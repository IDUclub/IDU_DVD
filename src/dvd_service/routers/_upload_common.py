"""Shared multipart-upload/background-job helpers used by both `/documents` and
`/user-documents`."""

from __future__ import annotations

import json
import mimetypes
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import structlog
from fastapi import Form, HTTPException, Response, UploadFile
from fastapi.concurrency import run_in_threadpool

from src.common.config import Settings
from src.common.db.minio_client import DocumentStorage
from src.common.db.redis_client import DocumentRegistry
from src.dvd_service.modules.doc_parsers import DocumentParser

log = structlog.get_logger(__name__)


def queued_job(
    job_id: str, filename: str | None, operation: str, name: str | None = None
) -> dict:
    return {
        "job_id": job_id,
        "status": "queued",
        "filename": filename,
        "operation": operation,
        "name": name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        # The browser owns 0..10% while it transfers the multipart body. By the time
        # this server-side job exists, that part is complete and the pipeline owns 10..100%.
        "stage": "queued",
        "stage_index": 0,
        "stage_total": 7,
        "progress": 0,
        "progress_total": 1,
        "task_progress": 0,
        "overall_progress": 10,
    }


def parse_json_field(name: str, value: str | None) -> dict:
    if not value:
        return {}
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise HTTPException(422, f"Поле '{name}' должно быть JSON-объектом: {exc}")
    if not isinstance(data, dict):
        raise HTTPException(422, f"Поле '{name}' должно быть JSON-объектом")
    return data


async def document_meta(
    doc_type: str | None = Form(None),
    corpus: str | None = Form(None),
    lang: str | None = Form(None),
    title: str | None = Form(None),
    source_uri: str | None = Form(None),
    effective_date: str | None = Form(None),
    external_ids: str | None = Form(None),  # JSON object: {code, doi, isbn, url, …}
    metadata: str | None = Form(None),  # JSON object: free-form domain attributes
) -> dict:
    """Optional per-document metadata form fields, shared by upload/update/reload."""
    return {
        "doc_type": doc_type,
        "corpus": corpus,
        "lang": lang,
        "title": title,
        "source_uri": source_uri,
        "effective_date": effective_date,
        "external_ids": parse_json_field("external_ids", external_ids),
        "metadata": parse_json_field("metadata", metadata),
    }


async def receive_file(
    file: UploadFile, settings: Settings, parser: DocumentParser, job_id: str
) -> tuple[str, list[dict], str]:
    """Validate the extension, persist the upload and pre-parse it: (path, raw, hash)."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in settings.allowed_extensions:
        raise HTTPException(
            415,
            "Поддерживаются только: %s (получено '%s')"
            % (", ".join(settings.allowed_extensions), ext or "—"),
        )
    os.makedirs(settings.upload_dir, exist_ok=True)
    path = os.path.join(settings.upload_dir, f"{job_id}_{file.filename}")
    with open(path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    # Cheap text extraction BEFORE the heavy LLM pass — also powers the duplicate check.
    try:
        raw = await run_in_threadpool(parser.extract_raw, path)
    except Exception as exc:  # noqa: BLE001
        os.remove(path)
        raise HTTPException(422, f"Не удалось разобрать файл: {exc}")
    return path, raw, parser.content_hash(raw)


def reject_duplicate(
    registry: DocumentRegistry, qdrant, content_hash: str, path: str
) -> None:
    """Reject an exact text duplicate — unless the registry entry is a ghost.

    ``register()`` runs only after a successful upsert, so a registered document whose name
    has no points left in Qdrant means the two stores diverged (a replaced instance, a dropped
    or re-created collection). The registry is authoritative only while Qdrant backs it, so
    such an entry is dropped and the upload proceeds as a fresh one instead of being refused
    for a document nobody can see.
    """
    if not registry.has_hash(content_hash):
        return
    info = registry.hash_info(content_hash) or {}
    name = info.get("name")
    if name and not qdrant.points_by_name(name):
        removed = registry.remove_hashes(name)
        if doc_id := info.get("doc_id"):
            registry.unregister_document(doc_id)
        registry.unregister_name(name)
        log.warning(
            "stale_registry_entry_dropped",
            name=name,
            version=info.get("version"),
            hashes_removed=removed,
            reason="registered document has no points in Qdrant",
        )
        return
    os.remove(path)
    raise HTTPException(
        400,
        detail="Документ уже загружен — текст полностью совпадает (имя: %s, версия: %s)"
        % (info.get("name"), info.get("version")),
    )


def object_key(content_hash: str, suffix: str) -> str:
    """Content-addressed MinIO object key — identical content always reuses the same object."""
    return f"{content_hash}{suffix}"


async def save_source(storage: DocumentStorage, path: str, content_hash: str) -> str:
    """Upload the already-received temp file to MinIO; returns its object key.

    Raises on failure — this is the fail-closed gate: callers must not queue an ingestion job
    for a source file that wasn't durably saved.
    """
    key = object_key(content_hash, Path(path).suffix)
    data = await run_in_threadpool(Path(path).read_bytes)
    content_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    await run_in_threadpool(storage.upload, key, data, content_type)
    return key


def pick_source_point(points: list[dict], version: str | None) -> dict:
    """Select the point to serve a document's original file from.

    With an explicit ``version``, only points carrying it (in ``version`` or the multi-valued
    ``versions``) qualify. Without one, prefers each candidate's own ``version`` field so a
    fragment merely *shared* with a later version doesn't shadow that version's own origin point;
    ties broken by the lexicographically latest version string (mirrors ``find_node``).
    """
    if not points:
        raise HTTPException(404, "документ не найден")
    if version:
        candidates = [
            p
            for p in points
            if version == p.get("version") or version in (p.get("versions") or [])
        ]
        if not candidates:
            raise HTTPException(404, f"версия не найдена: {version}")
    else:
        candidates = points
    return max(candidates, key=lambda p: p.get("version", ""))


def download_response(data: bytes, content_type: str | None, filename: str) -> Response:
    """A file download response with an RFC-5987-safe filename (names may be Cyrillic/spaced)."""
    ascii_fallback = (
        filename.encode("ascii", "replace").decode("ascii").replace('"', "_")
    )
    disposition = (
        f'attachment; filename="{ascii_fallback}"; '
        f"filename*=UTF-8''{quote(filename, safe='')}"
    )
    return Response(
        content=data,
        media_type=content_type or "application/octet-stream",
        headers={"Content-Disposition": disposition},
    )


def run_job(job_id: str, path: str, task) -> None:
    """Background wrapper: job status is maintained inside the service call itself."""
    try:
        task()
    except Exception:  # noqa: BLE001 — error status is already set by the service
        log.warning("background_job_error", job_id=job_id)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

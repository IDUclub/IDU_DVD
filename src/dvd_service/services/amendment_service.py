"""Amendments and clarifications saturate the document they change.

An amending act ("О внесении изменений в Правила …") is uploaded like any document and stays
searchable on its own. It is also *linked* to the document it amends — explicitly (``amends``
at upload, or later through the API) or by its title — and every link queues a consolidation
of that document:

1. the root edition is the newest edition that was uploaded, not built (its raw blocks are
   re-read from the original once and kept in object storage);
2. the linked acts are applied in order of their dates (``effective_date``, else the date in
   the act's heading): the LLM reads each act into operations once (cached on the link), and
   the operations are applied deterministically (``modules/amendments.py``);
3. the result is indexed as a new edition through the ordinary delta update — the fragments
   the acts changed carry ``amended_by`` — and every older edition becomes ``superseded``, so
   search answers from the current text unless asked for a version.

An act that arrives out of order simply triggers a rebuild from the root: the same cached
operations are replayed in date order. An act that changes no text (maps, boundary
descriptions) is recorded as such and builds nothing. An operation that could not be applied
is reported on its act and the edition is marked for review; the others still apply.

A clarification (``explains``) is only linked: it does not change the text, it is context for
the document's readers (NormGraph reads it next to the clauses).
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import structlog

from src.api_clients import ChatClient, create_llm
from src.api_clients.llm_client import OpenAICompatibleClient
from src.broker.events import DocumentUpdated
from src.common.config import Settings
from src.dvd_service.modules.amendments import (
    EXTRACTOR_VERSION,
    apply_operations,
    extract_operations,
    norm,
)
from src.dvd_service.modules.doc_parsers import DocumentParser

log = structlog.get_logger(__name__)

KINDS = ("amends", "explains")
RAW_PREFIX = "raw"
# A rebuild re-indexes the whole document; its lock outlives any sane run, not a dead worker.
LOCK_SECONDS = 6 * 3600
LOCK_POLL_SECONDS = 5

_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ),
        1,
    )
}
_DATE_WORDS = re.compile(r"\bот\s+\"?(\d{1,2})\"?\s+([а-я]+)\s+(\d{4})")
_DATE_DOTS = re.compile(r"\bот\s+(\d{1,2})\.(\d{1,2})\.(\d{4})")
_AMENDS = re.compile(r"о\s+внесении\s+изменени[йяе]\w*\s+в\s+(.{10,400})")
_WORD = re.compile(r"[^\W\d_]{4,}")


def _head(blocks: list[dict], size: int = 20) -> str:
    return norm(" ".join(b["text"] for b in blocks[:size]))


def act_date(blocks: list[dict]) -> str | None:
    """The date in the act's heading ("от 20 ноября 2023 года № 170") as YYYY-MM-DD."""
    head = _head(blocks)
    for m in _DATE_WORDS.finditer(head):
        month = _MONTHS.get(m.group(2))
        if month:
            return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(1)):02d}"
    m = _DATE_DOTS.search(head)
    if m:
        return f"{int(m.group(3)):04d}-{int(m.group(2)):02d}-{int(m.group(1)):02d}"
    return None


def amended_phrase(blocks: list[dict]) -> str | None:
    """What the act says it amends: the words after "о внесении изменений в" in its title.

    Only a heading counts — a block among the first ones that *starts* with the phrase: the
    rules themselves mention "о внесении изменений в правила" in their table of contents.
    A title wrapped over a few short blocks is joined back.
    """
    for i, block in enumerate(blocks[:8]):
        text = norm(block["text"])
        if not _AMENDS.match(text):
            continue
        for following in blocks[i + 1 : i + 3]:
            more = norm(following["text"])
            if len(more) > 200 or more.startswith(("в соответствии", "приказываю")):
                break
            text = f"{text} {more}"
        return _AMENDS.match(text).group(1)
    return None


def _stems(text: str) -> set[str]:
    return {w[:5] for w in _WORD.findall(norm(text))}


def _label(date: str | None) -> str:
    if date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        y, m, d = date.split("-")
        return f"{d}.{m}.{y}"
    return date or ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AmendmentService:
    """Links acts to the documents they change and builds the editions they produce."""

    def __init__(self, ingestion, queue, settings: Settings, client_factory=None):
        self.ingestion = ingestion
        self.registry = ingestion.registry
        self.qdrant = ingestion.qdrant
        self.storage = ingestion.storage
        self.parser = ingestion.parser
        self.jobs = ingestion.jobs
        self.queue = queue
        self.settings = settings
        self.client_factory = client_factory or self._llm

    def __repr__(self) -> str:
        return f"{type(self).__name__}(registry={self.registry!r})"

    def _llm(self) -> ChatClient:
        # Reading an act into operations needs more reasoning than tagging fragments: on
        # gpt-oss "low" loses sections and addresses that "medium" gets right.
        if self.settings.llm_provider == "openai":
            return OpenAICompatibleClient(
                reasoning_effort=self.settings.amendment_reasoning_effort
            )
        return create_llm()

    # --- raw blocks of stored documents ---

    def _raw(
        self, source_key: str, content_hash: str, filename: str | None
    ) -> list[dict]:
        """Raw blocks of a stored original, re-read once and then kept next to it.

        A scanned act takes minutes to recognize and the OCR cache lives on a scratch volume;
        the blocks themselves are what a rebuild needs, so they are stored with the original.
        """
        key = f"{RAW_PREFIX}/{content_hash}.json"
        if self.storage.exists(key):
            data, _ = self.storage.download(key)
            return json.loads(data)
        data, _ = self.storage.download(source_key)
        suffix = Path(filename or source_key).suffix or Path(source_key).suffix
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, f"source{suffix}")
            Path(path).write_bytes(data)
            raw = self.parser.extract_raw(path)
        self.store_raw(content_hash, raw)
        return raw

    def store_raw(self, content_hash: str, raw: list[dict]) -> None:
        self.storage.upload(
            f"{RAW_PREFIX}/{content_hash}.json",
            json.dumps(raw, ensure_ascii=False).encode("utf-8"),
            "application/json",
        )

    def _origin(self, name: str, version: str | None = None) -> dict:
        """The point that carries an edition's own original (the newest edition by default)."""
        points = [
            p for p in self.qdrant.points_by_name(name) if p.get("source_object_key")
        ]
        if version is not None:
            points = [p for p in points if p.get("version") == version]
        if not points:
            raise KeyError(
                f"нет сохранённого исходника: {name} {version or ''}".strip()
            )
        return max(points, key=lambda p: (p.get("uploaded_at") or "", p.get("version")))

    # --- links ---

    def find_target(self, phrase: str, exclude: str) -> str | None:
        """The stored document an act's heading names, when exactly one fits it well."""
        wanted = _stems(phrase)
        scores: dict[str, float] = {}
        for doc in self.registry.all_documents():
            name = doc.get("name")
            if not name or name == exclude or self.registry.amendment_target(name):
                continue
            for label in (name, doc.get("title") or ""):
                if _AMENDS.search(norm(label)):
                    continue  # another act about the same document
                stems = _stems(label)
                if len(stems) >= 2:
                    score = len(stems & wanted) / len(stems)
                    scores[name] = max(scores.get(name, 0.0), score)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])
        if not ranked or ranked[0][1] < 0.8:
            return None
        if len(ranked) > 1 and ranked[1][1] >= ranked[0][1]:
            log.info(
                "amendment_target_ambiguous", phrase=phrase[:120], candidates=ranked[:3]
            )
            return None
        return ranked[0][0]

    def after_ingest(
        self,
        result: dict,
        raw: list[dict],
        *,
        content_hash: str,
        amends: str | None = None,
        explains: str | None = None,
        effective_date: str | None = None,
    ) -> dict | None:
        """Link a freshly indexed document when it is an act about a stored one."""
        name = result["name"]
        if amends or explains:
            kind, target, detected = (
                ("amends", amends, False) if amends else ("explains", explains, False)
            )
        else:
            phrase = amended_phrase(raw)
            target = self.find_target(phrase, name) if phrase else None
            kind, detected = "amends", True
        if not target:
            return None
        if not self.registry.has_name(target):
            log.warning("amendment_target_missing", act=name, target=target)
            return None
        self.store_raw(content_hash, raw)
        return self.link(
            name,
            target,
            kind,
            content_hash=content_hash,
            effective_date=effective_date or act_date(raw),
            detected=detected,
        )

    def link(
        self,
        name: str,
        target: str,
        kind: str = "amends",
        *,
        content_hash: str | None = None,
        effective_date: str | None = None,
        detected: bool = False,
    ) -> dict:
        """Link act ``name`` to ``target``; an ``amends`` link queues a rebuild of it."""
        if kind not in KINDS:
            raise ValueError(f"неизвестный вид связи: {kind}")
        if name == target:
            raise ValueError("документ не может изменять сам себя")
        if not self.registry.has_name(target):
            raise KeyError(f"документ не найден: {target}")
        if self.registry.amendment_target(target) == name:
            raise ValueError("документы не могут изменять друг друга")
        origin = self._origin(name)
        content_hash = content_hash or origin.get("content_hash") or ""
        if effective_date is None:
            effective_date = origin.get("effective_date") or act_date(
                self._raw(
                    origin["source_object_key"], content_hash, origin.get("source")
                )
            )
        previous_target = self.registry.amendment_target(name)
        previous = (
            self.registry.amendment(previous_target, name) if previous_target else None
        ) or {}
        keep = previous.get("content_hash") == content_hash
        record = {
            "name": name,
            "target": target,
            "kind": kind,
            "detected": detected,
            "effective_date": effective_date,
            "content_hash": content_hash,
            "linked_at": _now(),
            # The operations depend only on the act: a re-link keeps what was read.
            "ops": previous.get("ops") if keep else None,
            "ops_version": previous.get("ops_version") if keep else None,
            "status": "pending" if kind == "amends" else "linked",
            "results": [],
        }
        self.registry.link_amendment(target, record)
        log.info(
            "amendment_linked", act=name, target=target, kind=kind, detected=detected
        )
        if (
            previous_target
            and previous_target != target
            and previous.get("kind") == "amends"
        ):
            self.enqueue(previous_target)
        if kind == "amends":
            record["job_id"] = self.enqueue(target)
        if kind == "explains" or previous.get("kind") == "explains":
            self._announce(name, origin.get("version"))
        return record

    def unlink(self, name: str) -> str | None:
        """Drop the link of act ``name``; the document it amended is rebuilt without it."""
        target = self.registry.amendment_target(name)
        if not target:
            return None
        record = self.registry.amendment(target, name) or {}
        self.registry.unlink_amendment(name)
        if record.get("kind") == "amends" and self.registry.has_name(target):
            self.enqueue(target)
        if record.get("kind") == "explains" and self.registry.has_name(name):
            self._announce(name)
        return target

    def _announce(self, name: str, version: str | None = None) -> None:
        """Tell consumers an explanation link of act ``name`` changed (its text did not).

        An explanation changes no text, so no edition is built: the act itself is announced
        as updated, and a consumer that reads its ``explains`` (NormGraph) relinks it. At
        upload the link is made after the act's own event, so this one follows it.
        """
        outbox = getattr(self.ingestion, "outbox", None)
        if outbox is None:
            return
        if version is None:
            try:
                version = self._origin(name).get("version")
            except KeyError:
                return
        outbox.enqueue(DocumentUpdated(document_name=name, version=version or ""))

    def enqueue(self, target: str, *, reextract: bool = False) -> str:
        # The routers package imports the dependency container, which imports this module.
        from src.dvd_service.routers._upload_common import queued_job

        # A rebuild that has not started yet will see this link too.
        for entry in self.queue.pending():
            if (
                entry.get("operation") == "consolidate"
                and entry.get("name") == target
                and (entry.get("reextract") or not reextract)
            ):
                return entry["job_id"]
        job_id = str(uuid.uuid4())
        self.jobs.set(job_id, queued_job(job_id, None, "consolidate", target))
        self.queue.enqueue(
            {
                "job_id": job_id,
                "operation": "consolidate",
                "name": target,
                "reextract": reextract,
                "content_hash": "",
            }
        )
        return job_id

    def overview(self, name: str) -> dict:
        """What amends or explains ``name``, what ``name`` itself amends, and its editions."""
        target = self.registry.amendment_target(name)
        own = self.registry.amendment(target, name) if target else None
        acts = sorted(
            self.registry.amendments(name),
            key=lambda r: (r.get("effective_date") or "9999", r.get("linked_at") or ""),
        )
        return {
            "name": name,
            "amendments": [_public(r) for r in acts],
            "amends": _public(own) if own else None,
            "editions": self.registry.editions(name),
        }

    # --- consolidation ---

    def consolidate(
        self,
        target: str,
        *,
        job_id: str | None = None,
        reextract: bool = False,
        on_identity=None,
    ) -> dict:
        """Rebuild ``target``'s current edition from its root edition and its linked acts.

        Acts uploaded together queue several rebuilds and workers run in parallel: one
        rebuild per document at a time; the next one finds the edition already built.
        """
        with self._locked(target):
            return self._consolidate(
                target, job_id=job_id, reextract=reextract, on_identity=on_identity
            )

    @contextmanager
    def _locked(self, target: str):
        """Hold the per-document rebuild lock, waiting for a running rebuild to finish."""
        key, token = f"{self.registry.prefix}:consolidating:{target}", uuid.uuid4().hex
        while not self.registry.r.set(key, token, nx=True, ex=LOCK_SECONDS):
            time.sleep(LOCK_POLL_SECONDS)
        try:
            yield
        finally:
            if self.registry.r.get(key) == token:
                self.registry.r.delete(key)

    def _consolidate(self, target, *, job_id, reextract, on_identity) -> dict:
        if not self.registry.has_name(target):
            raise KeyError(f"документ не найден: {target}")
        if job_id:
            self.jobs.update(
                job_id, status="processing", stage="amendments", error=None
            )
        editions = self.registry.editions(target)
        versions = self.registry.versions(target)
        roots = [v for v in versions if not (editions.get(v) or {}).get("consolidated")]
        if not roots:
            raise KeyError(f"нет исходной редакции: {target}")
        origin = max(
            (self._origin(target, v) for v in roots),
            key=lambda p: (p.get("uploaded_at") or "", p.get("version")),
        )
        root = origin["version"]
        root_date = origin.get("effective_date")
        blocks = self._raw(
            origin["source_object_key"],
            origin.get("content_hash") or "",
            origin.get("source"),
        )
        acts = sorted(
            (r for r in self.registry.amendments(target) if r.get("kind") == "amends"),
            key=lambda r: (r.get("effective_date") or "9999", r.get("linked_at") or ""),
        )
        client = None
        applied: list[dict] = []
        review = False
        try:
            for index, act in enumerate(acts, 1):
                if job_id:
                    self.jobs.update(
                        job_id,
                        status="processing",
                        stage="amendments",
                        phase=f"{index}/{len(acts)}: {act['name']}",
                        task_progress=int(100 * (index - 1) / max(1, len(acts))),
                    )
                if (
                    root_date
                    and act.get("effective_date")
                    and act["effective_date"] <= root_date
                ):
                    act.update(status="included", results=[])
                    self.registry.link_amendment(target, act)
                    continue
                try:
                    act_origin = self._origin(act["name"])
                except KeyError:
                    act.update(status="missing", results=[])
                    self.registry.link_amendment(target, act)
                    continue
                act_raw = self._raw(
                    act_origin["source_object_key"],
                    act.get("content_hash") or act_origin.get("content_hash") or "",
                    act_origin.get("source"),
                )
                if (
                    reextract
                    or act.get("ops") is None
                    or act.get("ops_version") != EXTRACTOR_VERSION
                ):
                    client = client or self.client_factory()
                    act["ops"] = extract_operations(act_raw, client)
                    act["ops_version"] = EXTRACTOR_VERSION
                if not act["ops"]:
                    act.update(status="no_text_changes", results=[])
                    self.registry.link_amendment(target, act)
                    continue
                outcome = apply_operations(blocks, act_raw, act["ops"], act["name"])
                blocks = outcome.blocks
                done = len(outcome.results) - len(outcome.failed)
                act["results"] = [r.as_dict() for r in outcome.results]
                act["status"] = (
                    "applied" if not outcome.failed else "partial" if done else "failed"
                )
                review = review or bool(outcome.failed)
                if done:
                    applied.append(act)
                self.registry.link_amendment(target, act)
        finally:
            if client is not None:
                client.close()

        if not applied:
            version = root
            outcome = {"name": target, "version": root, "built": False}
            if any(e.get("consolidated") for e in editions.values()):
                # The acts that built the current edition are gone or change nothing now.
                self._activate(target, root, editions)
        else:
            version, built = self._build(
                target, origin, blocks, applied, review, job_id, on_identity
            )
            outcome = {"name": target, "version": version, "built": built}
        outcome["acts"] = {a["name"]: a["status"] for a in acts}
        outcome["review_required"] = review
        if job_id:
            self.jobs.update(
                job_id,
                status="done",
                stage="done",
                overall_progress=100,
                task_progress=100,
                version=version,
            )
        log.info(
            "consolidation_done", **{k: v for k, v in outcome.items() if k != "acts"}
        )
        return outcome

    def _build(self, target, origin, blocks, applied, review, job_id, on_identity):
        """Index the consolidated blocks as an edition (or reuse an identical one)."""
        content_hash = DocumentParser.content_hash(blocks)
        editions = self.registry.editions(target)
        same = [
            v
            for v, e in editions.items()
            if e.get("content_hash") == content_hash
            and self.registry.version_exists(target, v)
        ]
        date = max((a.get("effective_date") or "" for a in applied), default="") or None
        if same:
            version, built = same[0], False
        else:
            label = _label(date) if date else applied[-1]["name"]
            result = self.ingestion.update(
                target,
                origin.get("source") or f"{target}.json",
                blocks,
                content_hash,
                version_override=f"{origin['version']} (ред. от {label})",
                job_id=job_id,
                on_identity=on_identity,
                doc_type=origin.get("doc_type"),
                corpus=origin.get("corpus"),
                lang=origin.get("lang"),
                title=origin.get("title"),
                source_uri=origin.get("source_uri"),
                external_ids=origin.get("external_ids"),
                metadata=origin.get("metadata"),
                effective_date=date,
                territory_id=(
                    origin.get("territory_id")
                    if origin.get("territory_source") == "manual"
                    else None
                ),
            )
            version, built = result["version"], True
        self.registry.set_edition(
            target,
            version,
            {
                "status": "active",
                "consolidated": True,
                "root": origin["version"],
                "amended_by": [a["name"] for a in applied],
                "review_required": review,
                "content_hash": content_hash,
                "effective_date": date,
                "built_at": _now(),
            },
        )
        self._activate(target, version, self.registry.editions(target))
        return version, built

    def _activate(self, target: str, version: str, editions: dict[str, dict]) -> None:
        """Make ``version`` the current edition: every other one is superseded by it."""
        for v in self.registry.versions(target):
            record = dict(editions.get(v) or {})
            if v == version:
                record.update(status="active", superseded_by=None)
            elif (
                record.get("status") != "superseded"
                or record.get("superseded_by") != version
            ):
                record.update(status="superseded", superseded_by=version)
            else:
                continue
            record.setdefault("consolidated", False)
            self.registry.set_edition(target, v, record)
        self.mark_points(target)

    def mark_points(self, target: str) -> None:
        """Fragments only superseded editions share are hidden from default search."""
        superseded = {
            v
            for v, e in self.registry.editions(target).items()
            if e.get("status") == "superseded"
        }
        groups: dict[str, list[str]] = {}
        for p in self.qdrant.points_by_name(target):
            tags = set(p.get("versions") or [p.get("version")])
            status = "superseded" if tags and tags <= superseded else "active"
            if (p.get("status") or "active") != status:
                groups.setdefault(status, []).append(p["id"])
        for status, ids in groups.items():
            self.qdrant.set_status(ids, status)


def _public(record: dict) -> dict:
    """A link record as the API shows it: the operations are summarized, not dumped."""
    out = {k: v for k, v in record.items() if k not in ("ops", "content_hash")}
    out["operations"] = len(record.get("ops") or [])
    return out

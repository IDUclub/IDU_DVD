"""Repair edition labels written by the old version-from-name heuristic.

Until the heuristic learned what a year looks like, any standalone four-digit group of a name
became the document's version: «СП 2.4.3648-20» was stored as edition «3648», «СП 2.4.2.4283-26»
as «4283», and the heuristic outranked the head pass, so the LLM's answer was never used.
Documents whose head pass failed outright were stored as edition «unknown». Consumers
(NormGraph provenance, gMART answers) then cite «ред. 3648» — an edition that does not exist.

The repair relabels those editions the way ingestion now would: a real year from the name,
else the head pass, else (for a number mistaken for a year) the name itself. It goes through the
document editor, so Qdrant payloads, the version registry and the document summary change
together, and it never overwrites an edition that already exists under the new label.
Idempotent: a repaired edition is no longer a candidate, so it runs once after every startup.
"""

from __future__ import annotations

import re
import threading

import structlog
from qdrant_client.models import Filter

from src.api_clients import ChatClient, create_llm
from src.common.db.qdrant_client import QdrantRepository, shared_only_condition
from src.dvd_service.modules.identity import extract_version_from_name
from src.dvd_service.modules.tagging import VersionDetector

log = structlog.get_logger(__name__)

UNKNOWN_VERSION = "unknown"
# The heuristic before the year check: the last standalone four-digit group.
_LEGACY_VERSION_DIGITS = re.compile(r"(?<!\d)(\d{4})(?!\d)")
# How many leading fragments feed the head pass — the same window the ingest pipeline uses.
_HEAD_FRAGMENTS = 14
_FIELDS = ("doc_id", "name", "version", "versions", "order", "text")


def _legacy_version_from_name(name: str) -> str | None:
    matches = _LEGACY_VERSION_DIGITS.findall(name or "")
    return matches[-1] if matches else None


def is_bogus_version(name: str, version: str) -> bool:
    """An edition label only the old heuristic (or a failed head pass) could have produced."""
    if version.strip().casefold() == UNKNOWN_VERSION:
        return True
    return version == _legacy_version_from_name(name) and (
        extract_version_from_name(name) != version
    )


class VersionRepairService:
    def __init__(
        self,
        qdrant: QdrantRepository,
        editor,
        version_detector: VersionDetector,
    ) -> None:
        self.qdrant = qdrant
        self.editor = editor
        self.version_detector = version_detector
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"{type(self).__name__}(qdrant={type(self.qdrant).__name__})"

    def candidates(self) -> list[dict]:
        """Shared-corpus documents with at least one bogus edition label.

        Each entry is ``{doc_id, name, versions, texts}``: ``versions`` are the bogus labels,
        ``texts`` the leading fragments for the head pass. User document indices are not
        touched — their registries are scoped per project and they are re-uploaded, not kept.
        """
        payloads = self.qdrant.scroll_payloads(
            Filter(must=[shared_only_condition()]), fields=_FIELDS
        )
        documents: dict[str, dict] = {}
        for payload in sorted(payloads, key=lambda p: p.get("order", 0) or 0):
            doc_id = payload.get("doc_id") or ""
            if not doc_id:
                continue
            entry = documents.setdefault(
                doc_id,
                {
                    "doc_id": doc_id,
                    "name": payload.get("name", ""),
                    "versions": set(),
                    "texts": [],
                },
            )
            tags = payload.get("versions") or (
                [payload["version"]] if payload.get("version") else []
            )
            entry["versions"].update(tags)
            if len(entry["texts"]) < _HEAD_FRAGMENTS and payload.get("text"):
                entry["texts"].append(payload["text"])
        result = []
        for entry in documents.values():
            bogus = sorted(v for v in entry["versions"] if is_bogus_version(entry["name"], v))
            if bogus:
                result.append({**entry, "versions": bogus})
        return result

    def _new_version(
        self, document: dict, old: str, client_factory
    ) -> str | None:
        """The label ingestion would give this edition today, or ``None`` to leave it."""
        name = document["name"]
        if year := extract_version_from_name(name):
            return year if year != old else None
        head = self.version_detector.detect_head(
            [{"text": text} for text in document["texts"]], client_factory()
        )
        detected = (head.version or "").strip()
        # The head pass answers the name itself (or "unknown") when it finds no edition.
        found = detected and detected.casefold() != UNKNOWN_VERSION and detected != name
        if found:
            return detected if detected != old else None
        if old.casefold() == UNKNOWN_VERSION:
            return None  # nothing better than "unknown" is known
        return name  # a document number is not an edition; the designation is honest

    def run(self, *, dry_run: bool = False) -> dict:
        """Relabel every bogus edition; returns counts and the per-edition outcome."""
        if not self._lock.acquire(blocking=False):
            return {"status": "already_running", "repaired": 0, "editions": []}
        client: ChatClient | None = None

        def client_factory() -> ChatClient:
            nonlocal client
            if client is None:
                client = create_llm()
            return client

        counts = {"repaired": 0, "unchanged": 0, "conflicts": 0, "failed": 0}
        editions: list[dict] = []
        try:
            for document in self.candidates():
                for old in document["versions"]:
                    outcome = {
                        "doc_id": document["doc_id"],
                        "name": document["name"],
                        "old_version": old,
                        "new_version": None,
                    }
                    try:
                        new = self._new_version(document, old, client_factory)
                        outcome["new_version"] = new
                        if new is None:
                            outcome["status"] = "unchanged"
                        elif dry_run:
                            outcome["status"] = "planned"
                        else:
                            self.editor.update_document(
                                document["doc_id"],
                                {"version": new, "current_version": old},
                                manual=False,
                            )
                            outcome["status"] = "repaired"
                    except ValueError as exc:
                        # Most often the new label is already another edition of this name.
                        outcome.update(status="conflict", error=str(exc))
                    except Exception as exc:  # noqa: BLE001 — one document must not stop the sweep
                        outcome.update(status="failed", error=str(exc))
                    key = {
                        "repaired": "repaired",
                        "planned": "repaired",
                        "unchanged": "unchanged",
                        "conflict": "conflicts",
                    }.get(outcome["status"], "failed")
                    counts[key] += 1
                    editions.append(outcome)
                    log.info("version_repair_edition", dry_run=dry_run, **outcome)
        finally:
            if client is not None:
                client.close()
            self._lock.release()
        log.info("version_repair_done", dry_run=dry_run, **counts)
        return {"status": "done", "dry_run": dry_run, **counts, "editions": editions}

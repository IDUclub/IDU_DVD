"""Reference extraction and linking stage.

``ReferenceExtractor`` pulls mentions of other documents (and the clause they point at) out of each
fragment with the LLM (windowed, strict JSON — same shape as the structure/tagging stages).

``ReferenceResolver`` then turns each raw mention into a :class:`DocumentRef`, resolving it against
the store:

  * **internal** — a reference to the current document's own clause: resolved against the freshly
    built ``{numbering -> node_id}`` index;
  * **external, target loaded** — resolved against the registry of document names and Qdrant; the
    exact clause becomes ``target_node_id`` (or, if only the document is found, a document-level link);
  * **external, target missing** — left unresolved and pushed to the pending registry so the link is
    completed by ``IngestionService`` once that document is ingested.

Extraction is LLM-first by design; the regex seed/learned patterns (``reference_patterns`` +
the Qdrant pattern collection) are the substrate for the optional self-improvement step gated by
``settings.ref_pattern_learning``.
"""

from __future__ import annotations

import structlog

from src.api_clients import OllamaClient, OllamaError
from src.common.config import Settings
from src.common.db.qdrant_client import QdrantRepository
from src.common.db.redis_client import DocumentRegistry
from src.dvd_service.dto import DocumentRef
from src.dvd_service.modules.reference_patterns import (
    normalize_designation,
    normalize_numbering,
)
from src.dvd_service.modules.windowing import make_windows

log = structlog.get_logger(__name__)

REF_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "references": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "raw": {"type": "string"},
                                "target_name": {"type": "string"},
                                "target_numbering": {"type": "string"},
                            },
                            "required": ["raw", "target_name"],
                        },
                    },
                },
                "required": ["id", "references"],
            },
        }
    },
    "required": ["items"],
}
REF_SYSTEM = (
    "Дан пронумерованный список фрагментов документа, каждый с id. Найди в каждом фрагменте ССЫЛКИ "
    "на ДРУГИЕ документы или на конкретные пункты документов. Для каждой ссылки верни:\n"
    'raw - текст ссылки ДОСЛОВНО, как во фрагменте ("в соответствии с СП 42.13330.2016, п. 7.5").\n'
    'target_name - обозначение документа, на который ссылаются ("СП 42.13330.2016", '
    '"ГОСТ 12.1.004-91", "Федеральный закон N 123-ФЗ"). Если ссылка на пункт ТЕКУЩЕГО документа '
    'без указания другого документа - верни "".\n'
    'target_numbering - номер пункта/раздела внутри документа, на который ссылаются ("7.5", "4.2.1"). '
    'Если ссылка на документ целиком - верни "".\n'
    "Не выдумывай ссылок: если во фрагменте ссылок нет - верни пустой массив references. "
    "Верни поле references по каждому id."
)


class ReferenceExtractor:
    """Stage: extract raw reference mentions from node texts via the LLM (windowed)."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(window_max_items={self.settings.window_max_items})"
        )

    def _llm_refs(self, client: OllamaClient, window_texts) -> dict[int, list[dict]]:
        user = "\n".join("[%d] %s" % (i, t[:800]) for i, t in enumerate(window_texts))
        data = client.chat(REF_SYSTEM, user, REF_SCHEMA)
        out: dict[int, list[dict]] = {}
        for it in data.get("items", []):
            refs = []
            for r in it.get("references", []):
                raw = str(r.get("raw", "")).strip()
                if not raw:
                    continue
                refs.append(
                    {
                        "raw": raw,
                        "target_name": str(r.get("target_name", "")).strip(),
                        "target_numbering": str(r.get("target_numbering", "")).strip(),
                    }
                )
            out[it["id"]] = refs
        return out

    def extract(
        self, nodes, client: OllamaClient, on_progress=None
    ) -> dict[str, list[dict]]:
        """Return ``{node_id -> [raw reference dicts]}`` for nodes that mention other documents."""
        result: dict[str, list[dict]] = {}
        windows = list(
            make_windows(nodes, overlap=0, max_items=self.settings.window_max_items)
        )
        for done, (s, e) in enumerate(windows, 1):
            window = nodes[s:e]
            try:
                local = self._llm_refs(client, [n["text"] for n in window])
            except (OllamaError, Exception) as exc:  # noqa: BLE001
                log.warning("reference_window_skipped", start=s, end=e, error=str(exc))
                if on_progress:
                    on_progress(done, len(windows))
                continue
            for pos, n in enumerate(window):
                refs = local.get(pos, [])
                if refs:
                    result[n["id"]] = refs
            if on_progress:
                on_progress(done, len(windows))
        log.info("reference_extraction_done", nodes_with_refs=len(result))
        return result


class ReferenceResolver:
    """Stage: resolve raw reference mentions into :class:`DocumentRef` and queue dangling links."""

    def __init__(
        self,
        qdrant: QdrantRepository,
        registry: DocumentRegistry,
        settings: Settings,
    ) -> None:
        self.qdrant = qdrant
        self.registry = registry
        self.settings = settings

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"

    def resolve(
        self,
        doc_id: str,
        current_name: str,
        node_refs: dict[str, list[dict]],
        local_index: dict[str, str],
    ) -> dict[str, list[DocumentRef]]:
        """Resolve every node's raw references; register dangling ones as pending.

        ``local_index`` maps a numbering of the *current* document to its node id (for internal
        and self references).
        """
        known = {normalize_designation(n): n for n in self.registry.names()}
        norm_cur = normalize_designation(current_name)
        result: dict[str, list[DocumentRef]] = {}
        for node_id, refs in node_refs.items():
            resolved = [
                self._resolve_one(doc_id, norm_cur, node_id, r, local_index, known)
                for r in refs
            ]
            if resolved:
                result[node_id] = resolved
        return result

    def _resolve_one(
        self,
        doc_id: str,
        norm_cur: str,
        node_id: str,
        raw_ref: dict,
        local_index: dict[str, str],
        known: dict[str, str],
    ) -> DocumentRef:
        tname = raw_ref.get("target_name", "").strip()
        tnum = normalize_numbering(raw_ref.get("target_numbering", ""))
        norm_t = normalize_designation(tname)
        is_internal = not tname or norm_t == norm_cur

        ref = DocumentRef(
            raw=raw_ref["raw"],
            target_name=tname,
            target_numbering=tnum,
            scope="internal" if is_internal else "external",
        )

        if is_internal:
            ref.target_doc_id = doc_id
            if tnum and tnum in local_index:
                ref.target_node_id = local_index[tnum]
                ref.resolved = True
            else:
                # whole-document self reference (no clause) is considered resolved
                ref.resolved = not tnum
            return ref

        original = known.get(norm_t)
        if original is None:
            # Target document is not loaded — queue the dangling link for back-fill.
            self.registry.add_pending(
                norm_t,
                {
                    "source_doc_id": doc_id,
                    "source_node_id": node_id,
                    "raw": ref.raw,
                    "target_numbering": tnum,
                },
            )
            return ref

        hit = self.qdrant.find_node(original, tnum) if tnum else None
        if hit is None:
            hit = self.qdrant.find_node(original)  # document-level fallback
        if hit:
            ref.target_doc_id = hit.get("doc_id")
            ref.target_version = hit.get("version")
            # pinpoint the clause only when the numbering actually matched
            if tnum and hit.get("numbering") == tnum:
                ref.target_node_id = hit.get("node_id")
            ref.resolved = True
        return ref

    # --- back-fill: complete dangling references pointing at a just-ingested document ---
    def backfill(
        self,
        name: str,
        doc_id: str,
        version: str,
        local_index: dict[str, str],
    ) -> int:
        """Resolve references that were waiting for document ``name`` and update their source nodes.

        Returns the number of references updated.
        """
        pending = self.registry.pop_pending(normalize_designation(name))
        if not pending:
            return 0
        # group by source node so each node is read/written once
        by_node: dict[str, list[dict]] = {}
        for entry in pending:
            by_node.setdefault(entry["source_node_id"], []).append(entry)

        updated = 0
        for source_node_id, entries in by_node.items():
            stored = self.qdrant.retrieve([source_node_id])
            payload = stored.get(source_node_id)
            if not payload:
                continue  # source node gone (e.g. re-ingested) — drop silently
            refs = payload.get("references", []) or []
            wanted = {e["raw"]: e for e in entries}
            for ref in refs:
                entry = wanted.get(ref.get("raw"))
                if entry is None or ref.get("resolved"):
                    continue
                tnum = entry.get("target_numbering", "")
                ref["target_doc_id"] = doc_id
                ref["target_version"] = version
                if tnum and tnum in local_index:
                    ref["target_node_id"] = local_index[tnum]
                ref["resolved"] = True
                updated += 1
            self.qdrant.update_references(source_node_id, refs)
        log.info("reference_backfill_done", document=name, refs_updated=updated)
        return updated

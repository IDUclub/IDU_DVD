"""Scoped structure/name retrieval with complete trees and snapshot pagination."""

from __future__ import annotations

import hashlib
import json
import math

from qdrant_client.models import FieldCondition, Filter, MatchAny

from src.api_clients.embeddings_client import create_embedder
from src.common.db.qdrant_client import shared_only_condition, user_scope_conditions
from src.dvd_service.dto.fragment_search import (
    FragmentMatch,
    FragmentSearchRequest,
    FragmentSearchResponse,
    NameBackfillRequest,
)
from src.dvd_service.modules.fragment_structure import (
    NAME_FIELDS,
    StructurePattern,
    annotate_fragments,
    document_key,
    name_score,
)


def _snapshot(nodes: list[dict], parameters: dict) -> str:
    # Derived names are intentionally omitted: a backfill must not invalidate its
    # own cursor. Source/hierarchy changes *do* require a new preview/search.
    state = [
        (
            n["id"],
            n.get("text"),
            n.get("parent_id"),
            n.get("numbering"),
            n.get("name"),
            n.get("version"),
            n.get("versions"),
            n.get("order"),
            n.get("prev_id"),
            n.get("next_id"),
        )
        for n in nodes
    ]
    return hashlib.sha256(
        json.dumps([parameters, state], sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:24]


def _offset(cursor: str | None, snapshot: str) -> int:
    if not cursor:
        return 0
    try:
        key, value = cursor.split(":")
        offset = int(value)
        if key != snapshot or offset < 0:
            raise ValueError
        return offset
    except (ValueError, AttributeError):
        raise ValueError(
            "cursor is invalid or documents changed; restart the search/preview"
        ) from None


def _cosine(a, b):
    denominator = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return (
        sum(x * y for x, y in zip(a, b)) / denominator
        if denominator and len(a) == len(b)
        else 0.0
    )


def _scoped_context(node: dict, by_id: dict[str, dict], height: int) -> str:
    """Expand only within the already-authorized document/edition snapshot."""
    identity_fields = ("doc_id", "user_id", "project_id", "scenario_id", "version_id")
    identity = tuple(node.get(k) for k in identity_fields)
    texts, seen = [node.get("text", "")], {node["id"]}
    for direction in ("prev_id", "next_id"):
        current = node
        for _ in range(height):
            neighbour = by_id.get(current.get(direction))
            if (
                not neighbour
                or neighbour["id"] in seen
                or tuple(neighbour.get(k) for k in identity_fields) != identity
            ):
                break
            seen.add(neighbour["id"])
            if direction == "prev_id":
                texts.insert(0, neighbour.get("text", ""))
            else:
                texts.append(neighbour.get("text", ""))
            current = neighbour
    return " ".join(t for t in texts if t)


class FragmentSearchService:
    def __init__(self, search):
        self.search_service = search
        self.qdrant = search.qdrant

    def search(self, req: FragmentSearchRequest) -> FragmentSearchResponse:
        pattern = StructurePattern(req.pattern) if req.pattern else None
        # Security, territory, document and edition conditions use the same policy
        # as semantic search. Root-only selectors must not remove its ancestors or children.
        scope = req.model_copy(
            update={
                "name": None,
                "document_names": None,
                "types": None,
                "parent_id": None,
                "tags": None,
            }
        )
        query_filter = self.search_service._build_filter(scope, None)
        if req.name or req.document_names:
            single = document_key(req.name) if req.name else None
            any_names = {document_key(n) for n in req.document_names or []}

            def in_documents(n):
                keys = {
                    document_key(str(v))
                    for v in [
                        n.get("name", ""),
                        *(n.get("aliases") or []),
                        *(n.get("external_ids") or {}).values(),
                    ]
                }
                return (not single or single in keys) and (
                    not any_names or bool(keys & any_names)
                )

            identities = self.qdrant.iter_points(
                query_filter, ["doc_id", "name", "aliases", "external_ids"]
            )
            doc_ids = sorted(
                {n["doc_id"] for n in identities if n.get("doc_id") and in_documents(n)}
            )
            if not doc_ids:
                if req.cursor:
                    raise ValueError("documents changed; restart the search")
                return FragmentSearchResponse(
                    count=0, total=0, match_count=0, hits=[], complete=True
                )
            query_filter = Filter(
                must=[
                    query_filter,
                    FieldCondition(key="doc_id", match=MatchAny(any=doc_ids)),
                ]
            )
        nodes = self.qdrant.scan_points(query_filter)
        if req.name or req.document_names:
            nodes = [n for n in nodes if in_documents(n)]
        nodes.sort(key=lambda n: (str(n.get("doc_id", "")), n.get("order", 0), n["id"]))
        nodes = annotate_fragments(nodes)
        by_id = {n["id"]: n for n in nodes}
        eligible = [
            n
            for n in nodes
            if (not pattern or pattern.matches(n, by_id))
            and (not req.parent_id or n.get("parent_id") == req.parent_id)
            and (not req.types or n.get("type") in req.types)
            and (not req.tags or set(req.tags) & set(n.get("tags", [])))
        ]
        scores = {n["id"]: (1.0, "structure") for n in eligible}
        if req.name_query:
            names_for = lambda n: (
                n["fragment_name_path"]
                if req.name_scope == "path"
                else [n.get("fragment_name") or ""]
            )
            scores = {
                n["id"]: name_score(
                    req.name_query, names_for(n), req.name_mode == "expanded"
                )
                for n in eligible
            }
            if req.name_mode == "expanded":
                names = sorted(
                    {name for n in eligible for name in names_for(n) if name}
                )
                if len(names) > 5000:
                    raise ValueError(
                        "expanded name search exceeds 5000 names; narrow document/structure scope"
                    )
                if names:
                    with create_embedder() as embedder:
                        query_vector = embedder.embed_query(req.name_query)
                        vectors = []
                        for start in range(0, len(names), 32):
                            vectors.extend(
                                embedder.embed_documents(names[start : start + 32])
                            )
                    if len(vectors) != len(names):
                        raise ValueError("name embedding response is incomplete")
                    similarities = {
                        name: _cosine(query_vector, v)
                        for name, v in zip(names, vectors)
                    }
                    for n in eligible:
                        similarity = max(
                            (similarities.get(v, 0) for v in names_for(n)), default=0
                        )
                        # All lexical matches outrank semantic-only matches.
                        if scores[n["id"]][0] == 0 and similarity >= 0.5:
                            scores[n["id"]] = (min(0.59, similarity * 0.59), "semantic")
        roots = [n for n in eligible if scores[n["id"]][0] > 0]
        root_ids = {n["id"] for n in roots}
        selected = [
            n
            for n in nodes
            if n["id"] in root_ids
            or (req.include_children and root_ids.intersection(n["ancestor_ids"]))
        ]
        if req.name_query:
            # Rank disjoint trees, never promote a matching child above its parent.
            def anchor(n):
                if req.include_children:
                    for ancestor in n["ancestor_ids"]:
                        if ancestor in root_ids:
                            return ancestor
                return n["id"]

            tree_scores = {}
            for root in roots:
                key = anchor(root)
                tree_scores[key] = max(tree_scores.get(key, 0), scores[root["id"]][0])

            selected.sort(
                key=lambda n: (
                    -tree_scores[anchor(n)],
                    str(n.get("doc_id", "")),
                    by_id[anchor(n)].get("order", 0),
                    anchor(n),
                    n.get("order", 0),
                    n["id"],
                )
            )
        parameters = req.model_dump(exclude={"cursor", "limit"})
        snapshot = _snapshot(nodes, parameters)
        # Match-set changes (e.g. a manual name update or model change) also invalidate search cursors.
        snapshot = hashlib.sha256(
            (
                snapshot
                + json.dumps(
                    [
                        (n["id"], n["fragment_name"], n["fragment_name_path"])
                        for n in selected
                    ],
                    ensure_ascii=False,
                )
            ).encode()
        ).hexdigest()[:24]
        offset = _offset(req.cursor, snapshot)
        if offset > len(selected):
            raise ValueError("cursor offset exceeds the result set")
        page = selected[offset : offset + req.limit]
        next_offset = offset + len(page)
        hits = []
        for n in page:
            ancestors = [i for i in n["ancestor_ids"] if i in root_ids]
            score, kind = (
                scores[n["id"]]
                if n["id"] in root_ids
                else (max(scores[i][0] for i in ancestors), "descendant")
            )
            payload = {k: v for k, v in n.items() if k in FragmentMatch.model_fields}
            payload.update(
                id=n["id"],
                score=score,
                doc_id=n.get("doc_id", ""),
                name=n.get("name", ""),
                version=req.version or n.get("version", ""),
                kind=n.get("kind", "text"),
                type=n.get("type", ""),
                matched=n["id"] in root_ids,
                match_kind=kind,
                matched_ancestor_ids=ancestors,
                context=(
                    _scoped_context(
                        n,
                        by_id,
                        max(
                            0,
                            min(
                                req.context_height,
                                self.search_service.settings.max_context_height,
                            ),
                        ),
                    )
                    if req.context_height
                    else None
                ),
            )
            hits.append(FragmentMatch(**payload))
        candidates = [
            {
                k: n.get(k)
                for k in (
                    "id",
                    "doc_id",
                    "name",
                    "version",
                    "versions",
                    "numbering",
                    "fragment_name",
                    "structure_path",
                    "block",
                    "parent_id",
                    "type",
                )
            }
            for n in roots[:200]
        ]
        for candidate in candidates:
            root = by_id[candidate["id"]]
            address_nodes = [by_id[i] for i in root["ancestor_ids"] if i in by_id] + [
                root
            ]
            kind_labels = {"article": "статья", "chapter": "глава", "section": "раздел"}
            candidate["selection_path"] = (
                [
                    " ".join(
                        filter(
                            None, [kind_labels.get(n.get("type")), n.get("numbering")]
                        )
                    )
                    for n in address_nodes
                    if n.get("numbering")
                ]
                if root.get("numbering")
                else root.get("structure_path", [])
            )
            candidate["excerpt"] = " ".join(root.get("text", "").split())[:200]
            # Include descendants: identical root text alone does not prove that
            # two provisions (with exceptions or tables) have the same content.
            subtree = [
                n
                for n in nodes
                if n["id"] == root["id"] or root["id"] in n["ancestor_ids"]
            ]
            positions = {n["id"]: i for i, n in enumerate(subtree)}
            candidate["content_digest"] = hashlib.sha256(
                json.dumps(
                    [
                        (
                            n.get("type"),
                            n.get("numbering"),
                            n.get("text"),
                            n.get("table_html"),
                            n.get("block"),
                            positions.get(n.get("parent_id")),
                        )
                        for n in subtree
                    ],
                    ensure_ascii=False,
                ).encode()
            ).hexdigest()
        concrete = req.pattern and not any(c in req.pattern for c in "*?[–—-")
        return FragmentSearchResponse(
            count=len(hits),
            total=len(selected),
            match_count=len(roots),
            hits=hits,
            complete=next_offset == len(selected),
            next_cursor=(
                f"{snapshot}:{next_offset}" if next_offset < len(selected) else None
            ),
            ambiguous=bool(
                (concrete or (req.name_query and not req.pattern)) and len(roots) > 1
            ),
            candidates=candidates,
            candidates_complete=len(roots) <= 200,
        )

    def backfill(self, req: NameBackfillRequest) -> dict:
        from qdrant_client.models import FieldCondition, MatchValue

        conditions = (
            user_scope_conditions(req.user_id, [req.project_id])
            if req.user_id
            else [shared_only_condition()]
        )
        if req.doc_id:
            conditions.append(
                FieldCondition(key="doc_id", match=MatchValue(value=req.doc_id))
            )
        nodes = self.qdrant.scan_points(Filter(must=conditions))
        nodes.sort(key=lambda n: (str(n.get("doc_id", "")), n.get("order", 0), n["id"]))
        snapshot = _snapshot(
            nodes,
            {
                "doc_id": req.doc_id,
                "user_id": req.user_id,
                "project_id": req.project_id,
            },
        )
        offset = _offset(req.cursor, snapshot)
        if offset > len(nodes):
            raise ValueError("cursor offset exceeds the result set")
        annotated = annotate_fragments(nodes)
        changes = []
        skipped = updated = 0
        for old, new in zip(
            nodes[offset : offset + req.limit], annotated[offset : offset + req.limit]
        ):
            fields = {k: new[k] for k in NAME_FIELDS}
            changed = any(old.get(k) != v for k, v in fields.items())
            skipped += not bool(new["fragment_name"])
            if changed:
                if not req.dry_run:
                    self.qdrant.set_points_payload([old["id"]], fields)
                updated += 1
            changes.append(
                {"id": old["id"], "name": new["fragment_name"], "changed": changed}
            )
        end = min(len(nodes), offset + req.limit)
        return {
            "dry_run": req.dry_run,
            "processed": end - offset,
            "would_update" if req.dry_run else "updated": updated,
            "without_name": skipped,
            "total": len(nodes),
            "changes": changes,
            "next_cursor": f"{snapshot}:{end}" if end < len(nodes) else None,
            "complete": end == len(nodes),
        }

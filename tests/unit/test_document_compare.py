"""Hermetic document comparisons, authorization, and transport wiring."""

from time import perf_counter
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastmcp.exceptions import ToolError
from pydantic import ValidationError

from src.common.auth import get_effective_user_id, require_authenticated
from src.dependencies import Dependencies
from src.dvd_service.dto.compare import CompareRequest
from src.dvd_service.routers.library import router
from src.dvd_service.services import document_compare as module
from src.dvd_service.services.document_compare import DocumentCompareService
from tests.conftest import FakeQdrantRepo


class CompareRepo(FakeQdrantRepo):
    @staticmethod
    def _matches(payload, cond):
        if hasattr(cond, "has_id"):
            return payload.get("_point_id") in cond.has_id
        if hasattr(cond, "must"):
            return (
                all(CompareRepo._matches(payload, c) for c in cond.must or [])
                and not any(
                    CompareRepo._matches(payload, c) for c in cond.must_not or []
                )
                and (
                    not cond.should
                    or any(CompareRepo._matches(payload, c) for c in cond.should)
                )
            )
        return FakeQdrantRepo._matches(payload, cond)

    def search(self, vector, query_filter, limit):
        out = []
        for point_id, (_, payload) in self.points.items():
            if self._matches(dict(payload, _point_id=point_id), query_filter):
                out.append(SimpleNamespace(id=point_id, score=1.0, payload=payload))
        return out[:limit]

    def comparison_points(self, query_filter):
        return [
            dict(p, id=i, _vector=v)
            for i, (v, p) in self.points.items()
            if self._matches(p, query_filter)
        ]


def node(i, doc, text, num="", name="Regulation", vector=None, **extra):
    return i, (
        vector or [1.0, 0.0],
        dict(
            doc_id=doc,
            name=name,
            text=text,
            numbering=num,
            order=int(i.strip("lr") or 0),
            structure_path=["Parking", num],
            version="v1",
            **extra,
        ),
    )


def service(*nodes):
    repo = CompareRepo()
    repo.points.update(nodes)
    registry = MagicMock()
    registry.get_document.return_value = None
    registry.edition.return_value = None
    return DocumentCompareService(repo, registry)


def compare(s, **kwargs):
    return s.compare(CompareRequest(left_doc_id="a", right_doc_id="b", **kwargs))


def test_figures_are_compared_as_text_fragments():
    s = service(
        node("l1", "a", "Рисунок 1 Схема: высота 10 м", kind="figure"),
        node("r1", "b", "Рисунок 1 Схема: высота 12 м", kind="figure"),
    )
    result = compare(s)
    assert result.stats.left_compared == result.stats.right_compared == 1
    assert result.changes[0].kind == "modified"
    assert "10" in result.changes[0].changed_numbers.left


def test_editions_numbering_numeric_changes_and_sections():
    s = service(
        node("l1", "a", "Provide at least 10 spaces at 5 m.", "1"),
        node(
            "r1", "b", "Provide at least 20 spaces at 6 m.", "1", amended_by=["Act 1"]
        ),
    )
    r = compare(s)
    assert r.mode == "editions"
    assert r.stats.counts["modified"] == 1
    c = r.changes[0]
    assert c.left.numbering == "1"
    # «spaces» is not a measurement unit: the count stays bare, «m» keeps its unit.
    assert c.changed_numbers.left == "10; 5 m"
    assert c.changed_numbers.right == "20; 6 m"
    assert c.word_diff
    assert c.right.amended_by == ["Act 1"]
    assert r.sections[0].counts == {"modified": 1}


def test_renumbered_added_removed_unchanged_and_containers():
    s = service(
        node("l1", "a", "Keep clear.", "1"),
        node("r1", "b", "Keep clear.", "2"),
        node("l2", "a", "Unaltered", "3"),
        node("r2", "b", "Unaltered", "3"),
        node("l3", "a", "Delete zebra", "4"),
        node("r3", "b", "New obligation", "5"),
        node("l4", "a", "Container", "6", is_container=True),
        node("r4", "b", " ", "7"),
    )
    r = compare(s)
    assert r.stats.unchanged == 1
    assert r.stats.left_compared == r.stats.right_compared == 3
    assert {c.kind for c in r.changes} == {"renumbered", "added", "removed"}
    assert r.stats.total_changes == 3


def test_semantic_mutual_matches_and_unmatched():
    s = service(
        node("l1", "a", "Parking minimum 10 spaces", name="A", vector=[1, 0]),
        node("l2", "a", "Pedestrian access", name="A", vector=[0, 1]),
        node("r1", "b", "Provide ten car places", name="B", vector=[0.99, 0.01]),
        node("r2", "b", "Unrelated restriction", name="B", vector=[-1, 0]),
    )
    r = compare(s)
    assert r.mode == "documents"
    assert r.stats.pairs_compared == 1
    assert {c.kind for c in r.changes} == {"different", "only_left", "only_right"}
    assert r.changes[0].left.id == "l1"
    assert r.changes[0].right.id == "r1"
    assert r.changes[0].similarity > 0.99


def test_mutual_best_does_not_force_ambiguous_second_match():
    left = [{"text": "a", "_vector": [1, 0]}, {"text": "b", "_vector": [0.99, 0.01]}]
    right = [{"text": "c", "_vector": [1, 0]}]
    pairs, only_l, only_r = DocumentCompareService._align(left, right, False)
    assert pairs == [(0, 0, 1.0)]
    assert only_l == [1] and only_r == []


def test_topic_embeds_query_only_and_scopes_each_search(monkeypatch):
    s = service(
        node("l1", "a", "Parking 10", "1"),
        node("l2", "a", "Road 10", "2"),
        node("r1", "b", "Parking 20", "1"),
        node("r2", "b", "Road 20", "2"),
    )
    embedder = MagicMock()
    embedder.__enter__.return_value = embedder
    embedder.embed_query.return_value = [1, 0]
    monkeypatch.setattr(module, "create_embedder", lambda: embedder)
    original = s.qdrant.search
    calls = []

    def search(vector, flt, limit):
        calls.append((flt, limit))
        return [
            h
            for h in original(vector, flt, limit)
            if h.payload["text"].startswith("Parking")
        ]

    monkeypatch.setattr(s.qdrant, "search", search)
    r = compare(s, query="parking")
    assert r.stats.left_compared == r.stats.right_compared == 1
    assert r.topic_top_k == 60
    assert len(calls) == 2 and all(k == 60 for _, k in calls)
    embedder.embed_query.assert_called_once_with("parking")
    embedder.embed_documents.assert_not_called()
    assert r.changes[0].left.id == "l1"


@pytest.mark.parametrize("user", [None, "intruder"])
def test_private_document_denied_before_registry_read(user):
    s = service(
        node("l1", "a", "Secret", user_id="owner", project_id="project"),
        node("r1", "b", "Shared"),
    )
    with pytest.raises(KeyError, match="document not found"):
        s.compare(CompareRequest(left_doc_id="a", right_doc_id="b"), user)
    s.registry.get_document.assert_not_called()


def test_owner_can_compare_private_and_shared():
    s = service(
        node("l1", "a", "Secret", user_id="owner", project_id="project"),
        node("r1", "b", "Shared"),
    )
    r = s.compare(CompareRequest(left_doc_id="a", right_doc_id="b"), "owner")
    assert r.left.owner == "user" and r.right.owner == "shared"


def test_limit_text_and_diff_bounds_counts_stay_complete():
    s = service(
        node("l1", "a", "word " * 200 + "10%", "1"),
        node("r1", "b", "other " * 200 + "20%", "1"),
        node("l2", "a", "Unchanged", "2"),
        node("r2", "b", "Unchanged", "2"),
        node("r3", "b", "New clause", "3"),
    )
    r = compare(s, limit=1, max_chars=100)
    assert r.truncated and len(r.changes) == 1
    assert r.stats.total_changes == 2 and r.stats.unchanged == 1
    assert r.changes[0].kind == "modified"
    assert r.changes[0].left.text_truncated and r.changes[0].diff_truncated
    assert len(r.changes[0].left.text) == 100
    assert r.changes[0].changed_numbers.left == "10%"
    assert all(
        len(d.left) <= 100 and len(d.right) <= 100 for d in r.changes[0].word_diff
    )


def test_versions_sharing_id():
    s = service(node("l1", "a", "10 spaces", "1"), node("r1", "a", "20 spaces", "1"))
    s.qdrant.points["r1"][1]["version"] = "v2"
    with pytest.raises(ValueError, match="multiple editions"):
        compare(s)
    r = s.compare(
        CompareRequest(
            left_doc_id="a", right_doc_id="a", left_version="v1", right_version="v2"
        )
    )
    assert r.mode == "editions" and r.left.version == "v1" and r.right.version == "v2"
    assert r.stats.counts["modified"] == 1


def test_path_alignment_without_number():
    s = service(node("l1", "a", "Before"), node("r1", "b", "After"))
    assert compare(s).changes[0].kind == "modified"


def test_request_bounds():
    for field, value in [
        ("limit", 501),
        ("limit", 0),
        ("max_chars", 99),
        ("max_chars", 10001),
    ]:
        with pytest.raises(ValidationError):
            CompareRequest(left_doc_id="a", right_doc_id="b", **{field: value})


def test_rest_auth_forwarding_and_not_found():
    s = service(node("l1", "a", "10 spaces", "1"), node("r1", "b", "20 spaces", "1"))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[Dependencies.get_document_compare] = lambda: s
    app.dependency_overrides[require_authenticated] = lambda: "owner"
    app.dependency_overrides[get_effective_user_id] = lambda: "owner"
    with TestClient(app) as client:
        assert (
            client.post(
                "/library/compare", json={"left_doc_id": "a", "right_doc_id": "b"}
            ).json()["mode"]
            == "editions"
        )
        assert (
            client.post(
                "/library/compare", json={"left_doc_id": "missing", "right_doc_id": "b"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/library/compare",
                json={"left_doc_id": "a", "right_doc_id": "b", "limit": 501},
            ).status_code
            == 422
        )
    route = next(r for r in router.routes if r.path == "/library/compare")
    assert any(d.dependency is require_authenticated for d in route.dependencies)


def test_mcp_and_dependencies(monkeypatch):
    from src.mcp_server import server

    s = service(
        node("l1", "a", "10 spaces", "1", user_id="owner"),
        node("r1", "b", "20 spaces", "1"),
    )
    monkeypatch.setattr(Dependencies, "get_qdrant", lambda: s.qdrant)
    monkeypatch.setattr(Dependencies, "get_registry", lambda: s.registry)
    assert (
        server.compare_documents("a", "b", user_id="owner").stats.counts["modified"]
        == 1
    )
    with pytest.raises(ToolError, match="document not found"):
        server.compare_documents("a", "b", user_id=None)


def test_repository_paginated_vectors():
    from qdrant_client.models import Filter

    from src.common.db.qdrant_client import QdrantRepository

    repo = object.__new__(QdrantRepository)
    repo.collection = "docs"
    repo.client = MagicMock()
    repo.client.scroll.side_effect = [
        ([SimpleNamespace(id="1", vector=[1, 0], payload={"text": "a"})], "next"),
        ([SimpleNamespace(id="2", vector=[0, 1], payload={"text": "b"})], None),
    ]
    result = repo.comparison_points(Filter())
    assert [p["id"] for p in result] == ["1", "2"]
    assert result[1]["_vector"] == [0, 1]
    assert repo.client.scroll.call_args.kwargs["offset"] == "next"


def test_same_identity_uses_normalization_and_registry():
    s = service(
        node("l1", "a", "One", name="  REGULATION: "),
        node("r1", "b", "Two", name="regulation"),
    )
    assert compare(s).mode == "editions"
    s.qdrant.points["r1"][1]["name"] = "Other name"
    s.registry.edition.side_effect = lambda name, version: (
        {"doc_id": "b"} if name.strip() == "REGULATION:" else {}
    )
    assert compare(s).mode == "editions"


def test_section_moves_and_duplicate_text_prefer_same_location():
    s = service(
        node("l1", "a", "Identical", "1"),
        node("l2", "a", "Identical", "2"),
        node("r1", "b", "Identical", "2"),
        node("r2", "b", "Identical", "3"),
    )
    r = compare(s)
    assert r.stats.unchanged == 1 and r.stats.counts["renumbered"] == 1
    assert r.changes[0].left.id == "l1" and r.changes[0].right.id == "r2"


def test_numeric_changes_before_other_modified_and_additions():
    s = service(
        node("l1", "a", "Before", "1"),
        node("r1", "b", "After", "1"),
        node("l2", "a", "At least 10%", "2"),
        node("r2", "b", "At least 20%", "2"),
        node("r3", "b", "Added", "3"),
    )
    r = compare(s)
    assert [c.kind for c in r.changes] == ["modified", "modified", "added"]
    assert r.changes[0].left.numbering == "2"


def test_topic_does_not_let_superseded_or_blank_fragments_consume_top_k(monkeypatch):
    s = service(
        node("l1", "a", "Current", "1"),
        node("l2", "a", "Old", "1", status="superseded"),
        node("l3", "a", " ", "3"),
        node("l4", "a", "Container", "4", is_container=True),
        node("r1", "b", "Current", "1"),
    )
    embedder = MagicMock()
    embedder.__enter__.return_value = embedder
    embedder.embed_query.return_value = [1, 0]
    monkeypatch.setattr(module, "create_embedder", lambda: embedder)
    r = compare(s, query="parking")
    assert r.stats.left_compared == r.stats.right_compared == 1
    assert r.stats.unchanged == 1


def test_similar_pairs_are_reported_with_diff():
    s = service(
        node(
            "l1", "a", "A lengthy parking requirement with minimum 10 spaces", name="A"
        ),
        node(
            "r1", "b", "A lengthy parking requirement with minimum 20 spaces", name="B"
        ),
    )
    r = compare(s)
    assert r.changes[0].kind == "similar"
    assert r.changes[0].word_diff and r.changes[0].changed_numbers


def test_incompatible_vectors_fail_clearly():
    s = service(
        node("l1", "a", "A", name="A", vector=[1, 0]),
        node("r1", "b", "B", name="B", vector=[1, 0, 0]),
    )
    with pytest.raises(ValueError, match="incompatible stored vector dimensions"):
        compare(s)


def test_breadcrumb_section_uses_library_separator():
    assert module.section({"breadcrumb": "Parking / Requirements / 1"}) == "Parking"


def test_flat_document_section_groups_by_top_numbering():
    # Without a structure path the breadcrumb is the fragment's own heading.
    flat = {"numbering": "3.15", "breadcrumb": "3.15 помещение стоянки при доме"}
    assert module.section(flat) == "Раздел 3"
    assert module.section({"numbering": "27", "breadcrumb": "27"}) == "Без раздела"
    assert module.section({"breadcrumb": "Введение"}) == "Без раздела"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("высотой не более 20 м (далее – дома)", ["20 м"]),
        ("не менее 2,5 м2 на одного; не менее 3,5 м² на одного", ["2,5 м2", "3,5 м²"]),
        ("срок 10 лет, 15 мин, 300 лк, 25 %", ["10 лет", "15 мин", "300 лк", "25 %"]),
        ("норма 0,5 мест/чел", ["0,5 мест/чел"]),
        # Words after a number are not units unless they are measurement units.
        ("в отношении 36 академических часов", ["36"]),
        ("подпунктом 2 пункта 27, статьей 28", []),
        # Dates, act numbers, clause references and designations are identifiers.
        ("от 30.03.1999 N 52-ФЗ, № 14, ст. 1650", []),
        ("от 2 июня 2026 г. N 19", []),
        ("пунктами 3.4.1, 3.4.2 и СП 2.4.2.4283-26", []),
        ("within 10 m", ["10 m"]),
    ],
)
def test_quantities_keep_measured_values_and_skip_identifiers(text, expected):
    assert module.quantities(text) == expected


def test_changed_quantities_ignore_spelling_and_shared_values():
    # Only the spelling differs: trailing dot, spacing, Cyrillic «С» in «°С».
    assert module.changed_quantities(
        ["6 человек.", "-5°С", "2 года", "2 года"], ["6 человек", "-5 °C", "2 года"]
    ) == (["2 года"], [])
    assert module.changed_quantities(["30", "70%"], ["500 м", "1 км"]) == (
        ["30", "70%"],
        ["500 м", "1 км"],
    )


def test_reference_renumbering_does_not_outrank_measured_change():
    ref, area = [1.0, 0.0], [0.0, 1.0]
    s = service(
        node("l1", "a", "в соответствии с пунктами 3.4.1, 3.4.2 Правил", "", "A", ref),
        node("r1", "b", "в соответствии с подпунктом 1 пункта 27 Правил", "", "B", ref),
        node(
            "l2", "a", "Площадь не менее 2,5 м2 на одного обучающегося", "", "A", area
        ),
        node(
            "r2", "b", "Площадь не менее 3,5 м2 на одного обучающегося", "", "B", area
        ),
    )
    r = compare(s)
    first = r.changes[0]
    assert first.changed_numbers.left == "2,5 м2"
    assert first.changed_numbers.right == "3,5 м2"
    assert all(
        c.changed_numbers is None
        for c in r.changes
        if c.left and "Правил" in c.left.text
    )


def test_empty_documents_have_complete_zero_stats():
    s = service(node("l1", "a", " "), node("r1", "b", "Container", is_container=True))
    r = compare(s)
    assert not r.changes and not r.sections and not r.truncated
    assert r.stats.total_changes == r.stats.left_compared == r.stats.right_compared == 0


@pytest.mark.parametrize(
    "key", ["numbering", "fragment_name_path", "structure_path", "text"]
)
def test_editions_topic_adds_counterparts_before_alignment(monkeypatch, key):
    nodes = []
    for side, doc in (("l", "a"), ("r", "b")):
        for i in range(1, 4):
            point = node(f"{side}{i}", doc, f"{side} parking requirement {i}")
            payload = point[1][1]
            payload.update(numbering="", fragment_name_path=[], structure_path=[])
            if key == "numbering":
                payload[key] = f"clause {i}" if side == "l" else str(i)
            elif key == "text":
                payload[key] = f"Identical parking requirement {i}"
                payload["numbering"] = str(i + (10 if side == "r" else 0))
            else:
                payload[key] = ["Parking", str(i)]
            nodes.append(point)
    s = service(*nodes)
    embedder = MagicMock()
    embedder.__enter__.return_value = embedder
    embedder.embed_query.return_value = [1, 0]
    monkeypatch.setattr(module, "create_embedder", lambda: embedder)
    original = s.qdrant.search
    monkeypatch.setattr(
        s.qdrant,
        "search",
        lambda v, f, k: [hit for hit in original(v, f, k) if hit.id in {"l1", "r2"}],
    )
    r = compare(s, query="parking")
    assert r.stats.left_compared == r.stats.right_compared == 2
    assert r.stats.counts["added"] == r.stats.counts["removed"] == 0
    assert {c.left.id for c in r.changes} == {"l1", "l2"}
    assert {c.right.id for c in r.changes} == {"r1", "r2"}
    assert r.stats.counts["renumbered" if key == "text" else "modified"] == 2
    embedder.embed_query.assert_called_once_with("parking")


@pytest.mark.parametrize("editions", [True, False], ids=["editions", "documents"])
def test_large_document_comparison_is_fast(editions, record_property):
    import numpy as np

    size = 1500
    vectors = np.random.default_rng(151).normal(size=(size, 64)).tolist()
    nodes = []
    for side, doc in (("l", "a"), ("r", "b")):
        for i in range(size):
            spaces = 20 if side == "r" and i % 25 == 0 else 10
            nodes.append(
                node(
                    f"{side}{i}",
                    doc,
                    f"Clause {i}: provide at least {spaces} parking spaces near building {i}.",
                    str(i),
                    name="Regulation" if editions or side == "l" else "Other",
                    vector=vectors[i],
                )
            )
    s = service(*nodes)
    started = perf_counter()
    result = compare(s)
    elapsed = perf_counter() - started
    record_property("comparison_seconds", elapsed)
    print(f"{result.mode}: 1500 x 1500 fragments in {elapsed:.3f}s")
    assert result.stats.pairs_compared == size
    assert result.stats.unchanged == 1440
    assert result.stats.total_changes == 60
    assert elapsed < 3.0


def test_fuzzy_fallback_is_bounded_and_finds_unstructured_modifications(monkeypatch):
    left = [
        dict(text=f"Parking requirement for building {i}: ten spaces.")
        for i in range(300)
    ]
    right = [
        dict(text=f"Parking requirement for building {i}: twenty spaces.")
        for i in range(300)
    ]
    matcher = MagicMock(wraps=module.SequenceMatcher)
    monkeypatch.setattr(module, "SequenceMatcher", matcher)
    pairs, only_l, only_r = DocumentCompareService._align(left, right, True)
    assert pairs and all(i == j for i, j, _ in pairs)
    assert (
        matcher.call_count <= module.LEFTOVER_POOL_LIMIT * module.TEXT_CANDIDATE_LIMIT
    )
    assert all(
        i in only_l and i in only_r for i in range(module.LEFTOVER_POOL_LIMIT, 300)
    )


def test_indexed_alignment_normalizes_each_fragment_once(monkeypatch):
    left = [dict(text=f"Old requirement {i}", numbering=str(i)) for i in range(50)]
    right = [dict(text=f"New requirement {i}", numbering=str(i)) for i in range(50)]
    numbering = MagicMock(wraps=module.normalize_numbering)
    text = MagicMock(wraps=module.text_key)
    monkeypatch.setattr(module, "normalize_numbering", numbering)
    monkeypatch.setattr(module, "text_key", text)
    pairs, only_l, only_r = DocumentCompareService._align(left, right, True)
    assert len(pairs) == 50 and not only_l and not only_r
    assert numbering.call_count == text.call_count == 100


def test_numpy_alignment_ignores_missing_zero_vectors_and_breaks_ties_stably():
    left = [{"_vector": []}, {"_vector": [0, 0]}, {"_vector": [3, 0]}]
    right = [{"_vector": [2, 0]}, {"_vector": [2, 0]}]
    pairs, only_l, only_r = DocumentCompareService._align(left, right, False)
    assert pairs == [(2, 0, 1.0)]
    assert only_l == [0, 1] and only_r == [1]


def test_numpy_alignment_agrees_with_scalar_reference():
    import math

    import numpy as np

    rng = np.random.default_rng(151)
    left = [{"_vector": v.tolist()} for v in rng.random((8, 5))]
    right = [{"_vector": v.tolist()} for v in rng.random((9, 5))]

    def cosine(a, b):
        return sum(x * y for x, y in zip(a, b)) / math.sqrt(
            sum(x * x for x in a) * sum(y * y for y in b)
        )

    scores = [[cosine(a["_vector"], b["_vector"]) for b in right] for a in left]
    expected = []
    for i, row in enumerate(scores):
        j = max(range(len(right)), key=lambda j: row[j])
        if (
            max(range(len(left)), key=lambda i: scores[i][j]) == i
            and row[j] >= module.VECTOR_THRESHOLD
        ):
            expected.append((i, j, row[j]))
    pairs, _, _ = DocumentCompareService._align(left, right, False)
    assert [(i, j) for i, j, _ in pairs] == [(i, j) for i, j, _ in expected]
    assert [s for _, _, s in pairs] == pytest.approx([s for _, _, s in expected])


def test_mcp_flat_signature_and_request_validation(monkeypatch):
    from inspect import signature

    from src.mcp_server import server

    parameters = signature(server.compare_documents).parameters
    assert "request" not in parameters
    assert list(parameters)[:7] == [
        "left_doc_id",
        "right_doc_id",
        "left_version",
        "right_version",
        "query",
        "limit",
        "max_chars",
    ]
    fake = MagicMock()
    monkeypatch.setattr(Dependencies, "get_document_compare", lambda: fake)
    server.compare_documents("a", "b", "old", "new", "parking", 5, 100, user_id="owner")
    request, user = fake.compare.call_args.args
    assert request == CompareRequest(
        left_doc_id="a",
        right_doc_id="b",
        left_version="old",
        right_version="new",
        query="parking",
        limit=5,
        max_chars=100,
    )
    assert user == "owner"
    with pytest.raises(ToolError):
        server.compare_documents("a", "b", limit=501, user_id=None)

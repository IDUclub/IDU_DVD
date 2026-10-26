"""Fragment relations: candidate pairs, scorers, builder, and search expansion."""

from types import SimpleNamespace

import pytest

from src.dvd_service.dto import FragmentRelation, SearchHit
from src.dvd_service.modules.relations import (
    Candidate,
    CrossEncoderRelationScorer,
    HeuristicRelationScorer,
    LlmRelationScorer,
    RelationBuilder,
    RelationCandidates,
    continues,
    create_relation_scorer,
)
from src.dvd_service.services.dvd_service import SearchService


def node(nid, text, parent=None, children=(), numbering="", type_="paragraph", **kw):
    return {
        "id": nid,
        "text": text,
        "parent_id": parent,
        "child_ids": list(children),
        "numbering": numbering,
        "type": type_,
        "kind": kw.pop("kind", "text"),
        "breadcrumb": "",
        **kw,
    }


@pytest.fixture
def doc():
    return [
        node("root", "doc", children=["c1", "c2", "t"], type_="document"),
        node(
            "c1",
            "8.70 Выделяют следующие виды ОПТ:",
            "root",
            ["i1", "i2"],
            "8.70",
            "subclause",
        ),
        node(
            "i1", "& традиционные, движущиеся в общем потоке;", "c1", type_="list_item"
        ),
        node("i2", "& обособленные, на выделенных полосах.", "c1", type_="list_item"),
        node(
            "c2",
            "8.71 Ширину полос принимают по таблице 8.3.",
            "root",
            numbering="8.71",
            type_="subclause",
        ),
        node("t", "Таблица 8.3 – Ширина полос", "root", type_="table", kind="table"),
    ]


def vectors(nodes, close=()):
    """Orthogonal vectors except for the pairs in ``close``, which point the same way."""
    vecs = {
        n["id"]: [1.0 if i == j else 0.0 for j in range(len(nodes))]
        for i, n in enumerate(nodes)
    }
    for a, b in close:
        vecs[b] = vecs[a]
    return [vecs[n["id"]] for n in nodes]


def test_candidates_come_from_structure_references_and_neighbours(settings, doc):
    pairs = {
        (c.a, c.b): c.sources
        for c in RelationCandidates(settings).pairs(doc, vectors(doc, [("c1", "c2")]))
    }
    # The lead-in «…виды ОПТ:» breaks off right before its first item.
    assert pairs[("c1", "i1")] == {"parent_child", "continuation"}
    assert pairs[("i1", "i2")] == {"sibling"}
    assert "ref_table" in pairs[("c2", "t")]
    assert "knn" in pairs[("c1", "c2")]
    # The document root is never a partner; pairs keep reading order (a before b).
    assert not any("root" in key for key in pairs)
    assert ("i1", "c1") not in pairs


def test_bare_headings_are_not_paired(settings):
    nodes = [
        node("s", "7 Инженерная подготовка", children=["c"], type_="section"),
        node("c", "7.1 Отвод поверхностных вод следует предусматривать.", "s", numbering="7.1", type_="subclause"),
        node("a", "Статья 5 " + "Требования к размещению объектов. " * 6, type_="chapter"),
    ]  # fmt: skip
    pairs = {
        (c.a, c.b)
        for c in RelationCandidates(settings).pairs(nodes, vectors(nodes, [("c", "a")]))
    }
    assert not any("s" in key for key in pairs)
    assert ("c", "a") in pairs  # a heading with body text stays pairable


def test_large_sibling_groups_only_pair_a_window(settings):
    settings.relation_sibling_full = 3
    settings.relation_sibling_window = 1
    kids = [f"k{i}" for i in range(6)]
    nodes = [node("p", "Пункт", children=kids)] + [
        node(k, f"Текст {k}", "p") for k in kids
    ]
    pairs = {(c.a, c.b) for c in RelationCandidates(settings).pairs(nodes, [])}
    assert ("k0", "k1") in pairs and ("k0", "k2") not in pairs


def test_heuristic_directions(doc):
    by_id = {n["id"]: n for n in doc}
    lead = Candidate("c1", "i1", {"parent_child"}, 0.5)
    table = Candidate("c2", "t", {"ref_table"}, 0.2)
    (lead_ab, lead_ba), (tab_ab, tab_ba) = HeuristicRelationScorer().score(
        [lead, table], by_id
    )
    assert (
        lead_ab.weight == 1.0 and lead_ab.kind == "completes"
    )  # a lead-in needs its items
    assert lead_ba.kind == "completes"
    assert tab_ab.kind == "table_ref" and tab_ab.weight == 1.0
    assert tab_ba.weight < tab_ab.weight


def test_builder_keeps_both_directions_above_the_floor(settings, doc):
    settings.relation_min_store_weight = 0.5

    class Fixed:
        method = "fixed"

        def score(self, pairs, nodes):
            return [
                (SimpleNamespace(weight=0.9, kind="completes", confidence=None),
                 SimpleNamespace(weight=0.2, kind="same_topic", confidence=None))
                for _ in pairs
            ]  # fmt: skip

    rels = RelationBuilder(settings).build("d1", doc, [], Fixed())
    assert rels and all(r.weight == 0.9 and r.method == "fixed" for r in rels)
    assert all(r.doc_id == "d1" for r in rels)
    assert {(r.source_id, r.target_id) for r in rels} >= {("c1", "i1")}
    assert ("i1", "c1") not in {(r.source_id, r.target_id) for r in rels}


class GroupLLM:
    def __init__(self):
        self.calls = []

    def chat(self, system, user, schema):
        self.calls.append(user)
        count = schema["properties"]["pairs"]["maxItems"]
        return {
            "pairs": [
                {"id": i, "a_needs_b": {"weight": 3, "kind": "completes"},
                 "b_needs_a": {"weight": 0, "kind": "none"}}
                for i in range(count)
            ]
        }  # fmt: skip


def test_llm_scorer_groups_partners_of_one_anchor(settings, doc):
    settings.relation_llm_group = 8
    settings.llm_concurrency = 1
    by_id = {n["id"]: n for n in doc}
    pairs = [Candidate("c1", "i1"), Candidate("c1", "i2"), Candidate("c2", "t")]
    client = GroupLLM()
    scores = LlmRelationScorer(client, settings).score(pairs, by_id)
    assert len(client.calls) == 2  # one call for c1's two partners, one for c2
    assert [round(ab.weight, 2) for ab, _ in scores] == [1.0, 1.0, 1.0]
    assert all(ba.weight == 0.0 for _, ba in scores)


def test_llm_scorer_survives_a_failed_group(settings, doc):
    settings.llm_concurrency = 1

    class Broken:
        def chat(self, *a):
            raise RuntimeError("boom")

    by_id = {n["id"]: n for n in doc}
    scores = LlmRelationScorer(Broken(), settings).score([Candidate("c1", "i1")], by_id)
    assert scores[0][0].weight == 0.0


def test_cross_encoder_sends_directed_pairs_with_roles(settings, doc, monkeypatch):
    settings.relation_scorer_url = "http://scorer/v1"
    sent = []

    class Client:
        def __init__(self, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, url, json):
            sent.append((url, json))
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"scores": [0.9, 0.1] * (len(json["pairs"]) // 2)},
            )

    monkeypatch.setattr("src.dvd_service.modules.relations.httpx.Client", Client)
    by_id = {n["id"]: n for n in doc}
    ((ab, ba),) = CrossEncoderRelationScorer(settings).score(
        [Candidate("c1", "i1")], by_id
    )
    url, body = sent[0]
    assert url == "http://scorer/v1/relations/score"
    assert [p["role"] for p in body["pairs"]] == ["child", "parent"]
    assert body["pairs"][0]["x"]["numbering"] == "8.70"
    assert (ab.weight, ba.weight) == (0.9, 0.1)

    # A structural rule stronger than the model wins its direction; the model's
    # probability stays as the confidence.
    ((ab, ba),) = CrossEncoderRelationScorer(settings).score(
        [Candidate("c1", "i1", {"parent_child"}, 0.4)], by_id
    )
    assert (ab.weight, ab.kind, ab.confidence) == (1.0, "completes", 0.9)
    assert ba.weight == 1.0 and ba.confidence == 0.1


def test_scorer_factory(settings):
    assert settings.relation_scorer == "heuristic"
    assert isinstance(create_relation_scorer(settings, None), HeuristicRelationScorer)
    settings.relation_scorer = "cross_encoder"
    settings.relation_scorer_url = ""  # no service configured: the rules
    assert isinstance(create_relation_scorer(settings, None), HeuristicRelationScorer)
    settings.relation_scorer_url = "http://scorer/v1"
    assert isinstance(
        create_relation_scorer(settings, None), CrossEncoderRelationScorer
    )
    settings.relation_scorer = "llm"
    assert isinstance(create_relation_scorer(settings, GroupLLM()), LlmRelationScorer)


@pytest.mark.parametrize(
    "prev, nxt, expected",
    [
        # A norm table flattened by a PDF conversion, cut mid-row.
        (
            "полнокомплектной организации, мест5), 7): св.",
            "30 до 170 включительно",
            True,
        ),
        ("допускается определять по заданию на", "проектирование в соответствии", True),
        ("В городских населенных пунктах:", "& не более 500 м пешеходной", True),
        ("Жилые зоны формируют из следующих ЭПС:", "Квартал – основной элемент", False),
        ("введено в действие с 20 января 2023 г.", "Проектируемые предприятия", False),
        ("в жилой застройке — не менее 2,25 м;", "в сельских — 1,5 м", False),
        ("5 Население", "5.1 Численность населения", False),
        ("По заданию на проектирование", "3.3 специальные школы-интернаты", False),
    ],
)
def test_a_phrase_broken_between_fragments(prev, nxt, expected):
    assert continues(prev, nxt) is expected


def test_broken_neighbours_depend_on_each_other(settings):
    nodes = [
        node("a", "Приложение Л Нормы расчета объектов", type_="appendix"),
        node(
            "r", "2 Общеобразовательные организации При вместимости, мест: св.", order=1
        ),
        node("v", "30 до 170 включительно – 80 м2 на 1 место св.", order=2),
        node("w", "170 до 550 включительно – 35 м2 на 1 место.", order=3),
        node("x", "Отели По заданию на проектирование", order=4),
    ]
    pairs = RelationCandidates(settings).pairs(nodes, [])
    broken = [(c.a, c.b) for c in pairs if "continuation" in c.sources]
    assert broken == [("r", "v"), ("v", "w")]
    by_id = {n["id"]: n for n in nodes}
    (ab, ba), _ = HeuristicRelationScorer().score(
        [c for c in pairs if "continuation" in c.sources], by_id
    )
    assert (ab.weight, ab.kind) == (ba.weight, ba.kind) == (1.0, "completes")


class FakeRelations:
    def __init__(self, relations):
        self.relations = relations

    def outgoing(self, source_ids, min_weight=0.0):
        return sorted(
            (
                r
                for r in self.relations
                if r.source_id in source_ids and r.weight >= min_weight
            ),
            key=lambda r: -r.weight,
        )


def hit(pid, score=0.9):
    return SearchHit(
        id=pid,
        score=score,
        doc_id="d1",
        name="СП",
        version="1",
        kind="text",
        type="clause",
        text=pid,
    )


def test_search_adds_related_fragments_as_citable_hits(settings, fake_qdrant):
    from qdrant_client.models import PointStruct

    fake_qdrant.upsert(
        [PointStruct(id=i, vector=[0.0], payload={"doc_id": "d1", "name": "СП", "version": "1", "text": f"text {i}", "numbering": i})
         for i in ("a", "b", "c", "x")]  # fmt: skip
    )
    rels = FakeRelations(
        [
            FragmentRelation(
                source_id="a", target_id="b", doc_id="d1", weight=1.0, kind="completes"
            ),
            FragmentRelation(
                source_id="a", target_id="c", doc_id="d1", weight=0.3, kind="same_topic"
            ),
            FragmentRelation(
                source_id="a", target_id="gone", doc_id="d1", weight=0.9, kind="refines"
            ),
            FragmentRelation(
                source_id="a", target_id="x", doc_id="d1", weight=0.8, kind="refines"
            ),
        ]
    )
    settings.relation_context_min_weight = 0.6
    service = SearchService(fake_qdrant, settings, None, relations=rels)
    out = service.attach_related([hit("a"), hit("x", 0.5)])
    ids = [h.id for h in out]
    assert ids[:2] == ["a", "x"]  # matched hits first, untouched order
    assert ids[2:] == [
        "b"
    ]  # weak "c" dropped, orphan "gone" skipped, "x" not duplicated
    extra = out[2]
    assert extra.related_to == "a" and extra.relation_kind == "completes"
    assert extra.text == "text b" and extra.numbering == "b"
    assert [r.id for r in out[0].related] == ["b", "gone", "x"]


def test_search_without_relations_is_unchanged(settings, fake_qdrant):
    service = SearchService(fake_qdrant, settings, None)
    hits = [hit("a")]
    assert service.attach_related(hits) == hits

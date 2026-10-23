"""AmendmentService: an act linked to a stored document builds its current edition.

Runs the real pipeline over faked boundaries (the ``wired`` fixture): the base document and the
act are indexed, the act is linked, and consolidation goes through the ordinary delta update.
The act's operations come from a fake LLM.
"""

from __future__ import annotations

import pytest
from unit.test_services import SimpleNS, wired  # noqa: F401 — the fixture is reused

from src.dvd_service.dto import SearchRequest
from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.services.amendment_service import (
    AmendmentService,
    act_date,
    amended_phrase,
)

RULES = "Правила землепользования и застройки города Тест"

BASE = [
    {"text": RULES, "category": "Title", "html": None},
    {"text": "Статья 1. Общие положения", "category": "NarrativeText", "html": None},
    {
        "text": "1. Правила применяются на всей территории города.",
        "category": "NarrativeText",
        "html": None,
    },
    {
        "text": "2. Высота зданий не более 15 метров.",
        "category": "NarrativeText",
        "html": None,
    },
    {"text": "Статья 2. Зоны", "category": "NarrativeText", "html": None},
    {
        "text": "1. Устанавливаются жилые зоны.",
        "category": "NarrativeText",
        "html": None,
    },
]

ACT = [
    {"text": "ПРИКАЗ", "category": "Title", "html": None},
    {
        "text": "от 20 ноября 2023 года № 170",
        "category": "NarrativeText",
        "html": None,
    },
    {
        "text": f"О внесении изменений в {RULES} Тестового района",
        "category": "Title",
        "html": None,
    },
    {
        "text": "1. В пункте 2 статьи 1 слова «не более 15 метров» заменить словами "
        "«не более 20 метров».",
        "category": "NarrativeText",
        "html": None,
    },
]

OPERATION = {
    "item": "1",
    "item_block": 3,
    "scope": ["Статья 1"],
    "target": "item",
    "table": "",
    "section": "",
    "numbering": "2",
    "rows": [],
    "column": 0,
    "action": "replace_words",
    "find": "не более 15 метров",
    "text": "не более 20 метров",
    "content_blocks": [],
}


class FakeActReader:
    """Answers every act with the operations it was given."""

    def __init__(self, operations):
        self.operations = operations
        self.calls = 0

    def chat(self, system, user, schema, model=None):
        self.calls += 1
        return {"operations": self.operations}

    def close(self):
        pass


class FakeQueue:
    def __init__(self):
        self.entries = []

    def enqueue(self, entry):
        self.entries.append(entry)
        return entry

    def pending(self):
        return list(self.entries)


@pytest.fixture
def stack(wired):  # noqa: F811
    reader = FakeActReader([OPERATION])
    queue = FakeQueue()
    service = AmendmentService(
        wired.ingestion, queue, wired.ingestion.settings, client_factory=lambda: reader
    )

    def index(name, raw, version, key):
        content_hash = DocumentParser.content_hash(raw)
        wired.storage.upload(key, b"original")
        service.store_raw(content_hash, raw)
        return wired.ingestion.ingest(
            f"{name}.docx",
            raw,
            content_hash,
            name_override=name,
            version_override=version,
            source_object_key=key,
        )

    index(RULES, BASE, "2019", "rules.docx")
    act = index("Приказ № 170", ACT, "2023", "act.pdf")
    return SimpleNS(
        wired=wired,
        service=service,
        reader=reader,
        queue=queue,
        act=act,
        act_hash=DocumentParser.content_hash(ACT),
    )


def _texts(wired, **filters):
    request = SearchRequest(query="высота", limit=100, **filters)
    return [h.text for h in wired.search.search(request).hits]


def test_heading_gives_the_date_and_the_amended_document():
    assert act_date(ACT) == "2023-11-20"
    assert act_date([{"text": "Приказ от 13.01.2022 № 2"}]) == "2022-01-13"
    assert amended_phrase(ACT).startswith("правила землепользования")
    # A title wrapped over lines is joined back; the act's preamble is not.
    wrapped = [
        {"text": "О внесении изменений в Правила землепользования"},
        {"text": "и застройки города Тест"},
        {"text": "В соответствии со статьями 32 и 33 Градостроительного кодекса"},
    ]
    assert amended_phrase(wrapped) == "правила землепользования и застройки города тест"
    # The rules mention amending themselves in their contents: that is not a title.
    contents = BASE[:1] + [
        {"text": "Статья 13. О внесении изменений в правила землепользования"}
    ]
    assert amended_phrase(contents) is None


def test_an_uploaded_act_is_linked_by_its_title_and_queues_a_rebuild(stack):
    record = stack.service.after_ingest(stack.act, ACT, content_hash=stack.act_hash)
    assert record["target"] == RULES
    assert record["detected"] is True
    assert record["effective_date"] == "2023-11-20"
    assert stack.queue.entries[-1]["operation"] == "consolidate"
    assert stack.queue.entries[-1]["name"] == RULES
    # A second act before the rebuild starts joins the queued rebuild.
    assert stack.service.enqueue(RULES) == stack.queue.entries[-1]["job_id"]
    assert len(stack.queue.entries) == 1
    assert stack.service.enqueue(RULES, reextract=True) != record["job_id"]
    # A document that is not an act is left alone.
    assert stack.service.after_ingest({"name": RULES}, BASE, content_hash="h") is None


def test_consolidation_builds_the_current_edition(stack):
    wired, service = stack.wired, stack.service
    service.link("Приказ № 170", RULES)
    outcome = service.consolidate(RULES)
    assert outcome["built"] is True
    assert outcome["version"] == "2019 (ред. от 20.11.2023)"
    assert outcome["acts"] == {"Приказ № 170": "applied"}
    assert outcome["review_required"] is False

    editions = wired.registry.editions(RULES)
    assert editions["2019"]["status"] == "superseded"
    assert editions["2019"]["superseded_by"] == outcome["version"]
    current = editions[outcome["version"]]
    assert current["status"] == "active"
    assert current["amended_by"] == ["Приказ № 170"]
    assert current["root"] == "2019"

    # Default search reads the text in force; the replaced edition is reachable by version.
    hits = [t for t in _texts(wired, name=RULES) if "Высота" in t]
    assert any("20 метров" in t for t in hits)
    assert not any("15 метров" in t for t in hits)
    old = [t for t in _texts(wired, name=RULES, version="2019") if "Высота" in t]
    assert any("15 метров" in t for t in old)

    changed = [
        pl
        for _, pl in wired.qdrant.points.values()
        if pl["name"] == RULES and "20 метров" in pl["text"]
    ]
    assert changed and all(pl["amended_by"] == ["Приказ № 170"] for pl in changed)

    # The editions share the document id: a consumer rebuilding the document from the library
    # sees the text in force once.
    doc_id = changed[0]["doc_id"]
    current = wired.library.get_document(doc_id).text
    assert "20 метров" in current and "15 метров" not in current
    everything = wired.library.get_document(doc_id, include_superseded=True).text
    assert "20 метров" in everything and "15 метров" in everything

    act = wired.registry.amendment(RULES, "Приказ № 170")
    assert act["status"] == "applied"
    assert act["results"][0]["status"] == "applied"

    # Nothing changed: no new edition, and the act is not read again.
    again = service.consolidate(RULES)
    assert again == {**outcome, "built": False}
    assert stack.reader.calls == 1


def test_an_unapplicable_operation_marks_the_edition_for_review(stack):
    stack.reader.operations = [
        OPERATION,
        {**OPERATION, "item": "2", "scope": ["Статья 9"]},
    ]
    stack.service.link("Приказ № 170", RULES)
    outcome = stack.service.consolidate(RULES)
    assert outcome["acts"] == {"Приказ № 170": "partial"}
    assert outcome["review_required"] is True
    act = stack.wired.registry.amendment(RULES, "Приказ № 170")
    assert [r["status"] for r in act["results"]] == ["applied", "failed"]
    assert "не найдено место" in act["results"][1]["reason"]
    assert stack.wired.registry.editions(RULES)[outcome["version"]]["review_required"]


def test_an_act_without_text_changes_builds_nothing(stack):
    stack.reader.operations = []
    stack.service.link("Приказ № 170", RULES)
    outcome = stack.service.consolidate(RULES)
    assert outcome["built"] is False and outcome["version"] == "2019"
    assert outcome["acts"] == {"Приказ № 170": "no_text_changes"}
    assert stack.wired.registry.editions(RULES) == {}


def test_unlinking_the_act_makes_the_root_edition_current_again(stack):
    wired, service = stack.wired, stack.service
    service.link("Приказ № 170", RULES)
    built = service.consolidate(RULES)["version"]
    assert service.unlink("Приказ № 170") == RULES
    assert stack.queue.entries[-1]["name"] == RULES
    service.consolidate(RULES)
    editions = wired.registry.editions(RULES)
    assert editions["2019"]["status"] == "active"
    assert editions[built]["status"] == "superseded"
    hits = [t for t in _texts(wired, name=RULES) if "Высота" in t]
    assert any("15 метров" in t for t in hits)
    assert not any("20 метров" in t for t in hits)


def test_links_are_validated(stack):
    service = stack.service
    with pytest.raises(ValueError):
        service.link(RULES, RULES)
    with pytest.raises(KeyError):
        service.link("Приказ № 170", "нет такого")
    with pytest.raises(ValueError):
        service.link("Приказ № 170", RULES, "repeals")
    record = service.link("Приказ № 170", RULES, "explains")
    assert record["status"] == "linked"
    assert not stack.queue.entries  # a clarification changes no text
    overview = service.overview(RULES)
    assert overview["amendments"][0]["kind"] == "explains"
    assert service.overview("Приказ № 170")["amends"]["target"] == RULES


def _events(wired):
    entries = []
    while wired.outbox.size():
        entries.append(wired.outbox.peek())
        wired.outbox.commit()
    return [(e["model"], e["payload"]["document_name"]) for e in entries]


def test_an_explanation_link_is_served_and_announced(stack):
    wired, service = stack.wired, stack.service
    _events(wired)
    service.link("Приказ № 170", RULES, "explains")
    # the act's text did not change: it is announced so consumers relink it
    assert _events(wired) == [("DocumentUpdated", "Приказ № 170")]
    act = wired.library.get_document(stack.act["doc_id"])
    assert (act.explains, act.amends) == (RULES, None)
    rules = next(d for d in wired.library.list_documents().documents if d.name == RULES)
    assert wired.library.get_document(rules.doc_id).explains is None

    service.unlink("Приказ № 170")
    assert _events(wired) == [("DocumentUpdated", "Приказ № 170")]
    assert wired.library.get_document(stack.act["doc_id"]).explains is None


def test_an_amending_link_is_served_without_an_announcement(stack):
    wired, service = stack.wired, stack.service
    _events(wired)
    service.link("Приказ № 170", RULES)
    assert _events(wired) == []  # the rebuilt edition is announced by its own update
    act = wired.library.get_document(stack.act["doc_id"])
    assert (act.amends, act.explains) == (RULES, None)

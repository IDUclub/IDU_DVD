import pytest

from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.source_structure import SourceStructure
from tests.unit.test_services import wired


@pytest.mark.parametrize(
    "text",
    [
        "введена Федеральным законом от 27.12.2019 N 472-ФЗ)",
        "введен Федеральным законом от",
    ],
)
def test_editorial_fragments_without_opening_parenthesis_are_notes(text):
    assert SourceStructure.anchor(text) == {
        "type": "note",
        "numbering": "",
        "block": "amendment",
    }


def test_library_does_not_hide_indexed_documents_when_registry_is_missing(
    wired, sample_raw, monkeypatch
):
    result = wired.ingestion.ingest(
        "doc.docx", sample_raw, DocumentParser.content_hash(sample_raw)
    )
    monkeypatch.setattr(wired.registry, "all_documents", lambda: [])
    listing = wired.library.list_documents()
    assert {d.doc_id for d in listing.documents} == {result["doc_id"]}


def test_article_heading_from_dev_is_an_article():
    anchor = SourceStructure.anchor(
        "Статья\t52.\tОсуществление\tстроительства,\tреконструкции"
    )
    assert anchor["type"] == "article" and anchor["numbering"] == "52"

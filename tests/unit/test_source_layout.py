"""PDF-conversion layout: running headers and in-paragraph clause starts."""

import pytest

from src.dvd_service.modules.doc_parsers import DocumentParser
from src.dvd_service.modules.source_layout import SourceLayout, unspace
from src.dvd_service.modules.source_structure import SourceStructure


def pages(*bodies: str) -> str:
    """Join page bodies the way a PDF conversion does: a running header opens each page."""
    return "\n".join(
        f"СП 42.13330.2026 {n} {body}" for n, body in enumerate(bodies, 10)
    )


def raw(*texts):
    return [
        {"text": t, "category": "NarrativeText", "source_paragraph": True}
        for t in texts
    ]


def block_starts(text: str) -> set[int]:
    starts, pos = set(), 0
    for line in text.split("\n"):
        starts.add(pos)
        pos += len(line) + 1
    return starts


BODY = pages(
    "Содержание 1 Область применения 2 Нормативные ссылки 3 Термины",
    "1 Область применения 1.1 Настоящий свод правил распространяется на проекты.",
    "1.2 Требования следует учитывать. 2 Нормативные ссылки",
    "2.1 В настоящем своде правил использованы ссылки. 3 Организация территории",
    "3.1 Планировочная структура 3.1.1 Каркас формируют узлы: & один; & два.",
    "3.1.2 Размеры принимают согласно 3.1.1 и таблице 3.1. Жилая застройка 3.1.3 "
    "Застройку размещают по согласованию.",
)


def test_running_headers_are_found_and_stripped_from_text():
    layout = SourceLayout(BODY)
    assert len(layout.headers) == 6
    text = layout.strip_headers(0, len(BODY))
    assert "СП 42.13330.2026" not in text
    assert text.startswith("Содержание 1 Область применения")


def test_a_repeated_citation_is_not_a_running_header():
    text = "согласно СП 42.13330.2026 12 и СП 42.13330.2026 14 и далее"
    assert SourceLayout(text).headers == []


def test_address_run_follows_the_body_and_skips_the_toc_and_references():
    layout = SourceLayout(BODY)
    numbers = [n for _, n in layout.clause_addresses(block_starts(BODY))]
    assert numbers == [
        "1", "1.1", "1.2", "2", "2.1", "3", "3.1", "3.1.1", "3.1.2", "3.1.3",
    ]  # fmt: skip
    # «согласно 3.1.1» is a reference; «2 Нормативные ссылки» in the TOC is not a heading.
    offsets = dict((n, p) for p, n in layout.clause_addresses(block_starts(BODY)))
    assert BODY[offsets["3.1.3"] :].startswith("3.1.3 Застройку")
    assert BODY.index("Содержание") < offsets["1"]


def test_unspace_joins_letter_spaced_headings():
    assert unspace("Т а б л и ц а 6.8 – Виды") == "Таблица 6.8 – Виды"
    assert unspace("а в и с") == "авис"
    assert unspace("в и на") == "в и на"  # two letters are not a spaced word


@pytest.fixture
def parser(settings):
    settings.logical_partition_mode = "ranges"
    return DocumentParser(settings)


def test_units_are_cut_at_clauses_and_keep_exact_source(parser):
    blocks = raw(*BODY.split("\n"))
    source, units = parser.source_units(blocks)
    assert "".join(u["text"] for u in units) == source
    starts = [u["text"] for u in units if u.get("layout_number")]
    assert any(t.startswith("3.1.1 Каркас") for t in starts)
    assert any(t.startswith("1.1 Настоящий") for t in starts)
    # A header joins the unit before it: no unit starts with one, no clean text keeps one.
    assert not any(u["text"].startswith("СП 42.13330.2026") for u in units[1:])
    assert all("СП 42.13330" not in u.get("clean_text", "") for u in units)


def test_numbers_outside_the_run_lose_their_address():
    parts = [
        {"text": "5.2 Проектную численность определяют.", "_layout_number": "5.2"},
        {"text": "2 При определении показателей учитывают.", "_layout_number": None},
        {"text": "6 Организация территории", "_layout_number": "6"},
    ]
    SourceStructure.annotate(parts)
    anchors = [p["_source_anchor"] for p in parts]
    assert anchors[0]["numbering"] == "5.2"
    assert anchors[1] == {}
    assert anchors[2]["type"] == "section"
    assert anchors[2]["fragment_name"] == "Организация территории"


def test_documents_without_running_headers_are_cut_as_before(parser):
    blocks = raw("1 Общие положения", "1.1 Текст пункта. 1.2 Второй пункт.")
    _, units = parser.source_units(blocks)
    assert all("layout_number" not in u for u in units)
    assert all("clean_text" not in u for u in units)


HEAD = "Объекты1) Нормативная потребность1) Территориальная доступность2)"


def test_a_table_head_repeated_under_the_running_header_is_stripped():
    text = pages(
        "Приложение Л Таблица Л.1 " + HEAD + " 1 Дошкольные организации до",
        HEAD + " 100 мест – 44 м2. 2 Школы определять по заданию на",
        HEAD + "\nпроектирование в соответствии с СП 251.1325800",
        "Текст",
        "Текст",
        "Текст",
    )
    clean = SourceLayout(text).strip_headers(0, len(text))
    assert clean.count(HEAD) == 1  # the table's own head stays
    assert "до 100 мест" in clean and "на проектирование в соответствии" in clean


def test_an_appendix_heading_repeated_by_the_contents_is_kept():
    heading = "Приложение Е Расстояния по горизонтали между подземными сетями"
    text = pages(
        heading + " Приложение Ж Отходы", heading + " Т а б л и ц а Е.1", *["Текст"] * 4
    )
    assert SourceLayout(text).strip_headers(0, len(text)).count(heading) == 2


def test_a_range_abbreviation_does_not_end_a_sentence(parser):
    row = "При вместимости, мест: св. 30 до 170 – 80 м2 на 1 место св. 170 до 550 – 35 м2."
    assert parser._split_sentences(row) == [row]

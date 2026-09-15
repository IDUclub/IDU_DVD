"""Literal source headings must stay distinct from legal references and notes."""

import pytest

from src.dvd_service.modules.source_structure import SourceStructure


@pytest.mark.parametrize(
    "text",
    [
        "Статья 52 настоящего Кодекса.",
        "3.3 настоящей статьи. (в ред. Федеральных законов от",
        "3.3 (введена Федеральным законом от 01.01.2020 N 1-ФЗ)",
        "[ГОСТ 20400–2013, статья 3]",
        "Комментарий к статье 19",
        "18 июля 2011 года",
    ],
)
def test_reference_and_editorial_text_has_no_own_address(text):
    anchor = SourceStructure.anchor(text)
    assert not anchor.get("numbering")
    assert not anchor.get("source_heading_level")


@pytest.mark.parametrize(
    "text,number",
    [
        ("Статья 52. Осуществление строительства", "52"),
        ("Статья 19", "19"),
        ("Б.1 Общие требования", "Б.1"),
    ],
)
def test_literal_structural_address_is_retained(text, number):
    assert SourceStructure.anchor(text)["numbering"] == number


def test_multiline_and_unsubdivided_sections_are_headings():
    texts = [
        "1 Область применения",
        "Настоящий свод правил устанавливает требования.",
        "2 Нормативные ссылки",
        "ГОСТ 1.1 Общие положения",
        "11 Требования к изготовлению, возведению и эксплуатации\nжелезобетонных конструкций",
        "11.1 Конструкции следует изготовлять.",
    ]
    parts = SourceStructure.annotate([dict(text=t) for t in texts])
    for i in (0, 2, 4):
        assert parts[i]["_source_anchor"]["type"] == "section"


def test_toc_entries_cannot_create_normative_addresses():
    texts = [
        "Содержание",
        "1 Область применения ……………",
        "Приложение А Перечень …………… 28",
        "Введение",
        "1 Область применения",
        "Настоящий документ устанавливает требования.",
        "Приложение А Перечень",
    ]
    parts = SourceStructure.annotate([dict(text=t) for t in texts])
    for i in (1, 2):
        assert parts[i]["_source_anchor"]["type"] == "toc"
        assert not parts[i]["_source_anchor"]["numbering"]
    assert parts[4]["_source_anchor"]["numbering"] == "1"
    assert parts[6]["_source_anchor"]["numbering"] == "А"

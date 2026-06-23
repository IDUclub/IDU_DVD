"""Unit tests for src/dvd_service/modules/structure — Stage 2/3/3.5 StructureTagger.

Covers: type categorization + synonyms, numbering rank computation, leading-number stripping,
rank map extraction, and the full tag() pass driven by a fake LLM client.
"""

from __future__ import annotations

import pytest

from src.dvd_service.modules.structure import StructureTagger


@pytest.fixture
def tagger(settings):
    return StructureTagger(settings)


class TestCategorize:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("Глава", "chapter"),
            ("part", "chapter"),
            ("пункт", "clause"),
            ("list item", "list_item"),
            ("Title-Page", "title_page"),
            ("", "paragraph"),
            ("unknown_kind", "unknown_kind"),
        ],
    )
    def test_categorize_normalizes_and_maps_synonyms(self, raw, expected):
        assert StructureTagger.categorize(raw) == expected


class TestNumberingRank:
    @pytest.mark.parametrize(
        "num,rank",
        [
            ("1", 1),
            ("1.1", 2),
            ("4.2.", 2),
            ("1.2.3", 3),
        ],
    )
    def test_valid_numeric_ranks(self, num, rank):
        assert StructureTagger.numbering_rank(num) == rank

    @pytest.mark.parametrize("num", ["", "а)", "2099", "1.2.3.4.5.6.7", "ГОСТ"])
    def test_rejects_non_hierarchical_numbers(self, num):
        assert StructureTagger.numbering_rank(num) is None

    def test_numbering_ranks_filters_invalid(self, tagger):
        parts = [
            {"numbering": "1"},
            {"numbering": "1.1"},
            {"numbering": "а)"},
            {"numbering": ""},
        ]
        assert tagger.numbering_ranks(parts) == {"1": 1, "1.1": 2}


class TestStripLeadingNumbering:
    def test_strips_matching_prefix(self):
        assert (
            StructureTagger.strip_leading_numbering("1 Текст пункта", "1")
            == "Текст пункта"
        )
        assert (
            StructureTagger.strip_leading_numbering("а) перечисление", "а)")
            == "перечисление"
        )

    def test_no_numbering_returns_text_unchanged(self):
        assert StructureTagger.strip_leading_numbering("Текст", "") == "Текст"

    def test_does_not_strip_a_longer_number(self):
        # numbering "1" must not match inside "1.5 ..." (sub-number protection)
        assert (
            StructureTagger.strip_leading_numbering("1.5 Подпункт", "1")
            == "1.5 Подпункт"
        )

    def test_does_not_strip_when_only_content_remains_empty(self):
        # stripping would leave nothing -> keep original
        assert StructureTagger.strip_leading_numbering("1", "1") == "1"


class TestTagPass:
    def test_tag_assigns_structure_fields(self, tagger, fake_ollama):
        parts = [{"id": 0, "text": "Раздел"}, {"id": 1, "text": "Пункт"}]
        out = tagger.tag(parts, fake_ollama)
        for p in out:
            assert p["type"] == "paragraph"  # from fake handler
            assert {"raw_type", "numbering", "relation", "block"} <= p.keys()
        assert fake_ollama.chat_calls, "LLM should have been called for structure"

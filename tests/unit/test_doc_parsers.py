"""Unit tests for src/dvd_service/modules/doc_parsers — Stage 1 + 1.5 DocumentParser.

Covers: marker/heuristic detection, content hashing (dedup), block merging by boundaries,
heuristic-only logical splitting (no LLM), the refusal to structure a document when the LLM is
gone entirely, and __repr__. No network — `client=None` or a client that only raises.
"""

from __future__ import annotations

import pytest

from src.api_clients import LlmError
from src.dvd_service.modules.doc_parsers import (
    STRUCTURAL_GROUP_MAX_CHARS,
    DocumentParser,
    continues_designation,
    is_numbered_head,
    starts_new_marker,
)


class TestMarkerDetection:
    def test_numbered_list_marker_is_new(self):
        assert starts_new_marker("1. Текст пункта") is True
        assert starts_new_marker("2 Область применения") is True
        assert starts_new_marker("- буллет") is True

    def test_plain_text_is_not_a_marker(self):
        assert starts_new_marker("Настоящий документ устанавливает") is False

    def test_numbered_head_requires_separator_or_subnumber(self):
        assert is_numbered_head("1.1 Подпункт") is True
        assert is_numbered_head("1. Пункт") is True
        assert is_numbered_head("а) перечисление") is True
        assert is_numbered_head("1 без разделителя") is False
        assert is_numbered_head("обычный текст") is False

    @pytest.mark.parametrize(
        "text",
        [
            "17.13330 и СП 160.1325800.",
            "113.13330. Расчетную потребность мест определяют заданием.",
            "54.13330.2016 (пункт 5.8), остальных помещений",
            "2016 году введены изменения",
        ],
    )
    def test_designation_code_is_not_a_clause_number(self, text):
        assert starts_new_marker(text) is False
        assert is_numbered_head(text) is False

    def test_clause_numbers_keep_three_digits_per_level(self):
        assert starts_new_marker("6.1.11 Эксплуатируемые кровли") is True
        assert is_numbered_head("100.1 Пункт") is True

    @pytest.mark.parametrize(
        "prev, cur, expected",
        [
            ("согласно ГОСТ", "12.4.026 Знаки", True),
            ("в соответствии с ГОСТ Р", "21.1101", True),
            ("по СанПиН", "2.2.1/2.1.1.1200", True),
            ("требованиями\nСП", "113.13330.", True),
            ("указанных в п.", "5.8 настоящего", True),
            ("Требования к зданиям.", "6.1.2 Объемно", False),
            ("проектировать в СП", "Раздел 5", False),
            ("ТИСП", "1.1 Пункт", False),
        ],
    )
    def test_continues_designation(self, prev, cur, expected):
        assert continues_designation(prev, cur) is expected


class TestHeuristicBoundary:
    def setup_method(self):
        self.p = DocumentParser.__new__(DocumentParser)  # heuristics need no settings

    def test_table_forces_new(self):
        assert self.p._heuristic_boundary("a", "b", None, "Table") == "new"
        assert self.p._heuristic_boundary("a", "b", "Table", None) == "new"

    def test_marker_forces_new(self):
        assert (
            self.p._heuristic_boundary("Текст.", "1. Новый пункт", None, None) == "new"
        )

    def test_broken_sentence_is_continuation(self):
        # prev has no terminal punctuation, cur starts lowercase -> continuation
        assert (
            self.p._heuristic_boundary(
                "незаконченная строка", "продолжение", None, None
            )
            == "continuation"
        )

    def test_sentence_end_then_capital_is_new(self):
        assert (
            self.p._heuristic_boundary(
                "Конец предложения.", "Новое предложение", None, None
            )
            == "new"
        )

    def test_ambiguous_is_uncertain(self):
        # prev ends with terminal, cur starts lowercase and is not a marker -> uncertain (LLM decides)
        assert (
            self.p._heuristic_boundary("Конец.", "продолжение строчными", None, None)
            == "uncertain"
        )


class TestContentHash:
    def test_hash_is_deterministic(self, sample_raw):
        assert DocumentParser.content_hash(sample_raw) == DocumentParser.content_hash(
            sample_raw
        )

    def test_hash_changes_with_text(self, sample_raw):
        other = sample_raw[:-1]
        assert DocumentParser.content_hash(sample_raw) != DocumentParser.content_hash(
            other
        )

    def test_hash_is_sha256_hex(self, sample_raw):
        h = DocumentParser.content_hash(sample_raw)
        assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)


class TestMergeBlocks:
    def test_continuation_blocks_are_joined(self):
        blocks = [
            {"id": 0, "text": "A", "category": "x", "html": None},
            {"id": 1, "text": "B", "category": "x", "html": None},
            {"id": 2, "text": "C", "category": "x", "html": None},
        ]
        parts = DocumentParser._merge_blocks(blocks, ["new", "continuation", "new"])
        assert [p["text"] for p in parts] == ["A B", "C"]
        assert parts[0]["source_ids"] == [0, 1]


class TestLogicalSplitHeuristicOnly:
    def test_to_logical_parts_without_llm(self, settings, sample_raw):
        parser = DocumentParser(settings)
        parts = parser.to_logical_parts(sample_raw, client=None)
        assert parts, "expected at least one logical part"
        assert all({"id", "text", "source_ids"} <= p.keys() for p in parts)
        assert [p["id"] for p in parts] == list(range(len(parts)))  # ids are reindexed


class TestStructuralGrouping:
    @staticmethod
    def raw(texts):
        return [
            {"text": text, "category": "NarrativeText", "source_paragraph": True}
            for text in texts
        ]

    @pytest.mark.parametrize("size", [511, 512, 513])
    @pytest.mark.parametrize("markers", [("-", "-"), ("1.", "2."), ("а)", "б)")])
    def test_whole_list_threshold_and_source_ids(self, settings, size, markers):
        texts = ["Необходимо выполнить:", f"{markers[0]} проверку;", f"{markers[1]} "]
        texts[-1] += "я" * (size - len(" ".join(texts)))
        assert len(" ".join(texts)) == size
        parser = DocumentParser(settings)
        blocks = parser._split_into_segments(self.raw(texts))
        boundaries = parser._assemble_boundaries(blocks, client=None)
        short = size <= STRUCTURAL_GROUP_MAX_CHARS
        assert boundaries == (
            ["new", "continuation", "continuation"] if short else ["new"] * 3
        )
        parts = parser.to_logical_parts(self.raw(texts), client=None)
        assert [p["text"] for p in parts] == ([" ".join(texts)] if short else texts)
        assert [p["src_ids"] for p in parts] == (
            [[0, 1, 2]] if short else [[0], [1], [2]]
        )

    @pytest.mark.parametrize("long_list", [False, True])
    def test_llm_cannot_override_list_or_join_surrounding_text(
        self, settings, monkeypatch, long_list
    ):
        settings.semantic_merge_max_passes = 3
        parser = DocumentParser(settings)
        monkeypatch.setattr(
            parser,
            "_llm_boundaries",
            lambda client, texts: {i: "continuation" for i in range(len(texts))},
        )
        monkeypatch.setattr(
            parser,
            "_llm_semantic_merge",
            lambda client, texts: {i: "continuation" for i in range(len(texts))},
        )
        items = [
            "Требуется:",
            "- проверка;",
            "- " + ("я" * 512 if long_list else "расчёт."),
        ]
        texts = ["предисловие", *items, "пояснение", "и его продолжение"]
        parts = parser.to_logical_parts(self.raw(texts), client=object())
        expected_list = items if long_list else [" ".join(items)]
        assert [p["text"] for p in parts] == [
            "предисловие",
            *expected_list,
            "пояснение и его продолжение",
        ]
        # Repeated semantic passes must retain the forced boundaries.
        again = parser.semantic_merge(parts, client=object())
        assert [p["text"] for p in again] == [p["text"] for p in parts]

    def test_new_decisions_from_llm_do_not_split_short_list(
        self, settings, fake_ollama
    ):
        texts = ["Требуется:", "- проверка;", "- расчёт."]
        parts = DocumentParser(settings).to_logical_parts(self.raw(texts), fake_ollama)
        assert [p["text"] for p in parts] == [" ".join(texts)]

    @pytest.mark.parametrize("intro", ["Общие положения.", "Статья 1. Требования:"])
    def test_independent_numbered_provisions_remain_separate(self, settings, intro):
        texts = [intro, "1. Проверить объект.", "2. Выполнить расчёт."]
        parts = DocumentParser(settings).to_logical_parts(self.raw(texts), client=None)
        assert [p["text"] for p in parts] == texts

    @pytest.mark.parametrize(
        "boundary",
        [
            "Статья 2. Следующая статья",
            "(в ред. Федерального закона от 01.01.2026)",
            "14 сентября 2026 года",
            "обычное пояснение",
        ],
    )
    def test_list_stops_before_non_item(self, settings, boundary):
        texts = ["Требуется:", "- проверка;", "- расчёт.", boundary, "1. Другой пункт."]
        parts = DocumentParser(settings).to_logical_parts(self.raw(texts), client=None)
        assert [p["text"] for p in parts] == [" ".join(texts[:3]), *texts[3:]]

    def test_tables_end_lists_and_cannot_introduce_them(self, settings):
        raw = self.raw(
            ["Требуется:", "- проверка;", "1. Таблица:", "- отдельный пункт."]
        )
        raw[2].update(
            category="Table", html="<table><tr><td>Таблица:</td></tr></table>"
        )
        parts = DocumentParser(settings).to_logical_parts(raw, client=None)
        assert [p["text"] for p in parts] == [
            "Требуется: - проверка;",
            "1. Таблица:",
            "- отдельный пункт.",
        ]
        assert parts[1]["html"] == raw[2]["html"]

    @pytest.mark.parametrize("marker", ["-", "•", "1.", "а)", "IV."])
    def test_multiline_list_keeps_complete_items(self, settings, marker):
        settings.split_sentences = True
        settings.sent_min_len = 10
        texts = [
            "Требуется:",
            f"{marker} Проверить объект. Сохранить акт.",
            f"{marker} Выполнить расчёт.",
        ]
        raw = [{"text": "\n".join(texts), "category": "NarrativeText"}]
        parts = DocumentParser(settings).to_logical_parts(raw, client=None)
        assert [p["text"] for p in parts] == [" ".join(texts)]

    @pytest.mark.parametrize("size", [511, 512, 513])
    def test_numbered_parent_is_measured_with_all_children(self, settings, size):
        texts = ["1. Общие требования.", "1.1. Проверка.", "1.2. "]
        texts[-1] += "я" * (size - len(" ".join(texts)))
        following = "2. Другой пункт."
        parts = DocumentParser(settings).to_logical_parts(
            self.raw([*texts, following]), None
        )
        expected = [" ".join(texts)] if size <= 512 else texts
        assert [p["text"] for p in parts] == [*expected, following]

    def test_oversized_parent_descends_into_each_subtree(self, settings, fake_ollama):
        texts = [
            "1. " + "я" * 512,
            "1.1. Первая группа.",
            "1.1.1. Проверка.",
            "1.1.2. Расчёт.",
            "1.2. Вторая группа.",
            "1.2.1. Оформление акта.",
            "2. Следующий пункт.",
        ]
        parts = DocumentParser(settings).to_logical_parts(self.raw(texts), fake_ollama)
        assert [p["text"] for p in parts] == [
            texts[0],
            " ".join(texts[1:4]),
            " ".join(texts[4:6]),
            texts[6],
        ]
        assert [p["src_ids"] for p in parts] == [[0], [1, 2, 3], [4, 5], [6]]

    def test_oversized_subclause_descends_another_level(self, settings):
        texts = [
            "1. Требования.",
            "1.1. " + "я" * 512,
            "1.1.1. Проверка.",
            "1.1.1.1. Объект проверки.",
            "1.1.2. " + "ю" * 512,
            "1.2. Другой подпункт.",
        ]
        parts = DocumentParser(settings).to_logical_parts(self.raw(texts), None)
        assert [p["text"] for p in parts] == [
            texts[0],
            texts[1],
            " ".join(texts[2:4]),
            texts[4],
            texts[5],
        ]

    def test_numbered_point_and_introduced_list_share_one_budget(self, settings):
        texts = [
            "1. Требования.",
            "1.1. Выполнить:",
            "- проверку;",
            "- расчёт.",
            "1.2. Оформить акт.",
            "2. Другой пункт.",
        ]
        parts = DocumentParser(settings).to_logical_parts(self.raw(texts), None)
        assert [p["text"] for p in parts] == [" ".join(texts[:5]), texts[5]]

    def test_only_matching_number_prefixes_are_descendants(self, settings):
        texts = ["1. Пункт.", "10.1. Другой пункт.", "10.1.1. Его подпункт."]
        parts = DocumentParser(settings).to_logical_parts(self.raw(texts), None)
        assert [p["text"] for p in parts] == [texts[0], " ".join(texts[1:])]

    def test_article_parts_stay_siblings_but_nested_items_can_merge(self, settings):
        texts = [
            "Статья 52. Требования.",
            "3. Часть статьи.",
            "3.3. Другая часть.",
            "1) Условие.",
            "а) Проверка.",
            "б) Расчёт.",
            "2) Следующее условие.",
        ]
        parts = DocumentParser(settings).to_logical_parts(self.raw(texts), None)
        assert [p["text"] for p in parts] == [*texts[:2], " ".join(texts[2:])]

    def test_numbered_groups_cannot_cross_table_or_note(self, settings):
        for category, text in [
            ("Table", "Таблица"),
            ("NarrativeText", "(в ред. Федерального закона)"),
        ]:
            raw = self.raw(["1. Пункт.", "1.1. Подпункт.", text, "1.2. Ещё подпункт."])
            raw[2]["category"] = category
            parts = DocumentParser(settings).to_logical_parts(raw, None)
            assert [p["text"] for p in parts] == [
                "1. Пункт. 1.1. Подпункт.",
                text,
                "1.2. Ещё подпункт.",
            ]


class TestDesignationLineBreaks:
    def test_wrapped_code_is_not_split_from_its_line(self, settings):
        text = "6.1.11 Кровли проектируют с учетом ГОСТ\n12.4.026 и СП 17.13330.\n1.2 Пункт"
        assert DocumentParser(settings)._line_segments(text) == [
            "6.1.11 Кровли проектируют с учетом ГОСТ\n12.4.026 и СП 17.13330.",
            "1.2 Пункт",
        ]

    def test_wrapped_code_paragraph_continues_despite_structure(self, settings):
        raw = TestStructuralGrouping.raw(
            ["Место обозначают знаками согласно ГОСТ", "12.4.026 и оборудуют урнами."]
        )
        parser = DocumentParser(settings)
        blocks = parser._split_into_segments(raw)
        assert parser._assemble_boundaries(blocks, client=None) == [
            "new",
            "continuation",
        ]


class TestLlmOutage:
    """A dead LLM must fail the document, not quietly index it without structure.

    Every failed window is retried. Exhausted retries fail ingestion before indexing,
    leaving the corpus untouched. Explicit client=None retains the heuristic path.
    """

    class DeadLlm:
        """Every call fails the way an unreachable endpoint does."""

        def __init__(self):
            self.calls = 0

        def chat(self, system, user, schema, model=None):
            self.calls += 1
            raise ConnectionError("[Errno 111] Connection refused")

    def test_every_window_failing_raises(self, settings, sample_raw):
        parser = DocumentParser(settings)
        client = self.DeadLlm()
        with pytest.raises(LlmError, match="LLM недоступен"):
            parser.to_logical_parts(sample_raw, client)
        assert client.calls, "the LLM must actually have been attempted"

    def test_no_llm_at_all_is_still_the_heuristic_path(self, settings, sample_raw):
        """client=None is a deliberate choice, not an outage — it must keep working."""
        parts = DocumentParser(settings).to_logical_parts(sample_raw, client=None)
        assert parts


class TestRepr:
    def test_repr_mentions_pipeline_params(self, settings):
        r = repr(DocumentParser(settings))
        assert r.startswith("DocumentParser(") and "split_sentences=" in r

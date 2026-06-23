"""Unit tests for src/dvd_service/modules/doc_parsers — Stage 1 + 1.5 DocumentParser.

Covers: marker/heuristic detection, content hashing (dedup), block merging by boundaries,
heuristic-only logical splitting (no LLM), and __repr__. No network — `client=None` path.
"""

from __future__ import annotations

from src.dvd_service.modules.doc_parsers import (
    DocumentParser,
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


class TestRepr:
    def test_repr_mentions_pipeline_params(self, settings):
        r = repr(DocumentParser(settings))
        assert r.startswith("DocumentParser(") and "split_sentences=" in r

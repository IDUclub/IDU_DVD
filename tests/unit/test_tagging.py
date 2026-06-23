"""Unit tests for src/dvd_service/modules/tagging — Tagger and VersionDetector.

Covers: per-node tag extraction across windows, document name/version detection, graceful
fallback to "unknown" on LLM failure, and __repr__. LLM is faked.
"""

from __future__ import annotations

import pytest

from src.dvd_service.modules.tagging import Tagger, VersionDetector


class TestTagger:
    def test_tag_nodes_returns_tags_per_node(self, settings, fake_ollama):
        nodes = [
            {"id": "n0", "text": "первый фрагмент"},
            {"id": "n1", "text": "второй фрагмент"},
        ]
        result = Tagger(settings).tag_nodes(nodes, fake_ollama)
        assert set(result) == {"n0", "n1"}
        assert all(isinstance(tags, list) and tags for tags in result.values())

    def test_window_failure_is_skipped_not_fatal(self, settings):
        class Boom:
            def chat(self, *a, **k):
                raise RuntimeError("ollama down")

        result = Tagger(settings).tag_nodes([{"id": "n0", "text": "x"}], Boom())
        assert result == {}  # window skipped, no crash

    def test_repr(self, settings):
        assert "window_max_items=" in repr(Tagger(settings))


class TestVersionDetector:
    def test_detect_returns_name_and_version(self, fake_ollama):
        name, version = VersionDetector().detect([{"text": "СП ..."}], fake_ollama)
        assert name == "ТЕСТ 1"
        assert version == "ТЕСТ 1 ред. 1"

    def test_detect_falls_back_to_unknown_on_error(self):
        class Boom:
            def chat(self, *a, **k):
                raise RuntimeError("boom")

        assert VersionDetector().detect([{"text": "x"}], Boom()) == (
            "unknown",
            "unknown",
        )

    def test_repr(self):
        assert repr(VersionDetector()) == "VersionDetector()"

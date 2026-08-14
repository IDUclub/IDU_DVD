"""Unit tests for src/dvd_service/modules/tagging — VersionDetector.

Covers: the document head pass (name/version + administrative-scope hints), graceful fallback
to "unknown" on LLM failure, and __repr__. LLM is faked. (Fragment tagging now shares the
structure pass — see test_structure.)
"""

from __future__ import annotations

from src.dvd_service.modules.tagging import VersionDetector


class TestDocumentHead:
    def test_head_carries_identity_and_scope_hints(self, fake_ollama):
        head = VersionDetector().detect_head([{"text": "СП ..."}], fake_ollama)
        assert head.name == "ТЕСТ 1" and head.version == "ТЕСТ 1 ред. 1"
        assert head.level_hint == "federal"
        assert head.territory_hint == "" and head.region_hint == ""

    def test_identity_and_scope_share_one_llm_call(self, fake_ollama):
        """The scope hints ride the existing head pass — no second request per document."""
        VersionDetector().detect_head([{"text": "СП ..."}], fake_ollama)
        assert len(fake_ollama.chat_calls) == 1

    def test_head_falls_back_to_unknown_on_error(self):
        class Boom:
            def chat(self, *a, **k):
                raise RuntimeError("boom")

        head = VersionDetector().detect_head([{"text": "x"}], Boom())
        assert head.name == "unknown" and head.level_hint == "unknown"
        assert head.territory_hint == ""


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

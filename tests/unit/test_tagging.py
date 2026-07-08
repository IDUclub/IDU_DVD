"""Unit tests for src/dvd_service/modules/tagging — VersionDetector.

Covers: document name/version detection, graceful fallback to "unknown" on LLM failure, and
__repr__. LLM is faked. (Fragment tagging now shares the structure pass — see test_structure.)
"""

from __future__ import annotations

from src.dvd_service.modules.tagging import VersionDetector


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

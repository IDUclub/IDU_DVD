"""Unit tests for src/common/config — application configuration (Settings).

Covers: default values, environment-variable overrides (DVD_ prefix), and the concise __repr__.
"""

from __future__ import annotations

from src.common.config import Settings, settings


class TestDefaults:
    def test_core_defaults(self):
        s = Settings()
        assert s.qdrant_collection == "documents"
        assert s.vector_size == 1024  # must match bge-m3
        assert ".docx" in s.allowed_extensions  # OCR-free formats; PDF deferred
        assert ".pdf" not in s.allowed_extensions
        assert s.redis_job_ttl == 86400

    def test_module_singleton_is_settings_instance(self):
        assert isinstance(settings, Settings)


class TestEnvOverride:
    def test_dvd_prefixed_env_overrides_default(self, monkeypatch):
        monkeypatch.setenv("DVD_QDRANT_COLLECTION", "custom_coll")
        monkeypatch.setenv("DVD_VECTOR_SIZE", "512")
        s = Settings()
        assert s.qdrant_collection == "custom_coll"
        assert s.vector_size == 512

    def test_unknown_env_is_ignored(self, monkeypatch):
        monkeypatch.setenv("DVD_TOTALLY_UNKNOWN", "x")
        Settings()  # extra="ignore" — must not raise


class TestRepr:
    def test_repr_is_concise_and_mentions_key_endpoints(self):
        r = repr(Settings())
        assert r.startswith("Settings(")
        assert "ollama=" in r and "qdrant=" in r and "redis=" in r
        assert "vector_size=1024" in r

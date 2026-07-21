"""Unit tests for src/common/config — application configuration (Settings).

Covers: default values, environment-variable overrides (DVD_ prefix), and the concise __repr__.
"""

from __future__ import annotations

import pytest

from src.common.config import Settings, settings


class TestDefaults:
    def test_core_defaults(self):
        s = Settings()
        assert s.qdrant_collection == "documents"
        assert s.vector_size == 2048  # must match Giga-Embeddings-instruct
        assert s.embeddings_provider == "giga"
        assert s.embeddings_model == "ai-sage/Giga-Embeddings-instruct"
        assert ".docx" in s.allowed_extensions  # OCR-free formats; PDF deferred
        assert ".pdf" not in s.allowed_extensions
        assert s.redis_job_ttl == 86400

    def test_embedding_model_name_follows_provider(self):
        assert Settings().embedding_model_name == "ai-sage/Giga-Embeddings-instruct"
        s = Settings(embeddings_provider="ollama")
        assert s.embedding_model_name == s.ollama_embed_model


class TestCollectionNamespacing:
    def test_namespacing_on_by_default(self):
        assert Settings().collection_namespacing is True

    def test_effective_collection_encodes_model_and_dim(self):
        s = Settings()  # giga / 2048
        assert s.effective_collection == "documents__giga_embeddings_instruct_2048"

    def test_ollama_provider_gets_a_distinct_space(self):
        s = Settings(embeddings_provider="ollama", vector_size=1024)
        assert s.effective_collection == "documents__bge_m3_1024"

    def test_registry_prefix_scoped_to_collection(self):
        s = Settings()
        assert s.registry_prefix == "dvd:documents__giga_embeddings_instruct_2048"

    def test_fixed_mode_uses_verbatim_name_and_legacy_prefix(self):
        s = Settings(collection_namespacing=False, qdrant_collection="documents")
        assert s.effective_collection == "documents"
        assert s.registry_prefix == "dvd"

    def test_base_name_is_preserved_as_prefix(self):
        s = Settings(qdrant_collection="mycorp")
        assert s.effective_collection.startswith("mycorp__")

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


class TestMinioEndpoint:
    """The minio SDK rejects a scheme in the endpoint, so Settings strips it (and lets it
    pick the transport) instead of letting the app crash at startup."""

    def test_bare_host_port_is_untouched(self):
        s = Settings(minio_endpoint="10.32.1.42:9000")
        assert s.minio_endpoint == "10.32.1.42:9000"
        assert s.minio_secure is False

    def test_http_scheme_is_stripped(self):
        s = Settings(minio_endpoint="http://10.32.1.42:9000")
        assert s.minio_endpoint == "10.32.1.42:9000"
        assert s.minio_secure is False

    def test_https_scheme_implies_secure(self):
        s = Settings(minio_endpoint="https://minio.example.com")
        assert s.minio_endpoint == "minio.example.com"
        assert s.minio_secure is True

    def test_explicit_secure_wins_over_scheme(self):
        s = Settings(minio_endpoint="https://minio.example.com", minio_secure=False)
        assert s.minio_secure is False

    def test_trailing_slash_is_dropped(self):
        assert (
            Settings(minio_endpoint="http://minio:9000/").minio_endpoint == "minio:9000"
        )

    def test_env_value_with_scheme_is_normalized(self, monkeypatch):
        monkeypatch.setenv("DVD_MINIO_ENDPOINT", "https://minio.idu:9000")
        s = Settings()
        assert s.minio_endpoint == "minio.idu:9000"
        assert s.minio_secure is True

    def test_explicit_secure_env_wins_over_scheme(self, monkeypatch):
        monkeypatch.setenv("DVD_MINIO_ENDPOINT", "https://minio.idu:9000")
        monkeypatch.setenv("DVD_MINIO_SECURE", "false")
        assert Settings().minio_secure is False

    def test_path_is_rejected(self):
        with pytest.raises(ValueError, match="path in the endpoint is not allowed"):
            Settings(minio_endpoint="http://minio:9000/bucket")

    def test_unknown_scheme_is_rejected(self):
        with pytest.raises(ValueError, match="unsupported scheme"):
            Settings(minio_endpoint="s3://minio:9000")


class TestRepr:
    def test_repr_is_concise_and_mentions_key_endpoints(self):
        r = repr(Settings())
        assert r.startswith("Settings(")
        assert "ollama=" in r and "qdrant=" in r and "redis=" in r
        assert "embeddings=giga" in r
        assert "vector_size=2048" in r

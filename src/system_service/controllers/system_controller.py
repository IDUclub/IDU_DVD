"""System controller: read/render logs and read/write the ``DVD_`` runtime configuration.

The log file on disk is JSON lines (see ``src/common/logger``). This controller turns those
JSON lines into a human-readable stream, optionally filtered by **day** and/or **request_id**,
so the ``/system/logs`` endpoint can hand back a readable ``.log`` file.

It also exposes the application settings (the ``DVD_``-prefixed environment contract from
``app_config.Settings``): a masked read of the current effective values, and a write that
persists chosen variables to the ``.env`` file and applies the runtime-tunable ones to the
live settings object immediately (structural ones take effect on the next restart).
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any, Iterator

import structlog

from src.common.config import Settings
from src.common.logger import TIMESTAMP_KEY, log_file_path

log = structlog.get_logger(__name__)

# JSON keys rendered explicitly (in this order); everything else is appended as key=value.
_PRIMARY_KEYS = (TIMESTAMP_KEY, "level", "logger", "request_id", "event")

# Values never returned in clear text by the read endpoint (nor echoed back on write).
_SENSITIVE_FIELDS: frozenset[str] = frozenset({"qdrant_api_key"})

# Settings captured at startup (service wiring, Qdrant collection/dimension, logging sinks,
# the GPU semaphore). Persisting them updates ``.env`` but they only fully take effect after a
# restart, so the live settings object is intentionally *not* mutated for these — that would
# pretend a change applied when the running wiring still uses the old value.
_RESTART_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {
        "qdrant_url",
        "qdrant_api_key",
        "qdrant_collection",
        "vector_size",
        "collection_namespacing",
        "embeddings_provider",
        "redis_url",
        "redis_job_ttl",
        "kafka_bootstrap_servers",
        "kafka_schema_registry_url",
        "kafka_client_id",
        "kafka_outbox_key",
        "kafka_dead_letter_key",
        "log_dir",
        "log_file",
        "log_level",
        "ingest_concurrency",
    }
)


def _format_entry(entry: dict) -> str:
    """Render one parsed JSON log line as a readable text line."""
    timestamp = entry.get(TIMESTAMP_KEY, "")
    level = str(entry.get("level", "")).upper()
    logger_name = entry.get("logger", "")
    request_id = entry.get("request_id")
    event = entry.get("event", "")

    head = f"{timestamp} [{level:<8}]"
    if logger_name:
        head += f" {logger_name}:"
    if request_id:
        head += f" (request_id={request_id})"
    head += f" {event}"

    extras = " ".join(f"{k}={entry[k]!r}" for k in entry if k not in _PRIMARY_KEYS)
    return f"{head} {extras}".rstrip()


class SystemController:
    """Access to the application log file and the runtime settings for the system router."""

    def __init__(self, settings: Settings, env_path: str | Path | None = None) -> None:
        self._settings = settings
        self._log_path: Path = log_file_path(settings)
        # File that persists writes so they survive a restart. Matches the ``env_file`` that
        # ``Settings`` reads on boot (``.env`` in the working directory) unless overridden.
        self._env_path: Path = Path(env_path) if env_path is not None else Path(".env")

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def env_path(self) -> Path:
        return self._env_path

    # --- settings read/write (the ``DVD_`` environment contract) ---
    @staticmethod
    def _env_name(field: str) -> str:
        """``qdrant_url`` -> ``DVD_QDRANT_URL`` (the environment-variable form)."""
        return "DVD_" + field.upper()

    @staticmethod
    def _to_field(key: str) -> str:
        """Normalize an incoming key (``DVD_QDRANT_URL`` or ``qdrant_url``) to a field name."""
        k = key.strip()
        if k.upper().startswith("DVD_"):
            k = k[4:]
        return k.lower()

    @classmethod
    def _mask(cls, field: str, value: Any) -> Any:
        return "***" if (field in _SENSITIVE_FIELDS and value) else value

    def _item(self, field: str) -> dict:
        """One settings row: field name, env-var name, (masked) value, and flags."""
        return {
            "field": field,
            "env": self._env_name(field),
            "value": self._mask(field, getattr(self._settings, field)),
            "restart_required": field in _RESTART_REQUIRED_FIELDS,
            "sensitive": field in _SENSITIVE_FIELDS,
        }

    def settings_snapshot(self) -> dict:
        """Current effective configuration (secrets masked), plus the derived collection info.

        ``vector_size`` here is the value actually in use — including the dimension auto-detected
        from the vectorizer at startup — so this endpoint is the quickest way to confirm Qdrant
        is storing 2048-d vectors.
        """
        return {
            "effective_collection": self._settings.effective_collection,
            "registry_prefix": self._settings.registry_prefix,
            "vector_size": self._settings.vector_size,
            "embeddings_provider": self._settings.embeddings_provider,
            "env_file": str(self._env_path),
            "settings": [self._item(f) for f in Settings.model_fields],
        }

    def update_env(self, updates: dict[str, Any]) -> dict:
        """Persist ``DVD_`` variables to the ``.env`` file and apply the runtime-tunable ones.

        Keys may be given as env names (``DVD_SEARCH_LIMIT``) or field names (``search_limit``);
        unknown keys are rejected (``ValueError``) so this cannot inject arbitrary environment.
        Runtime-tunable settings are coerced and written onto the live settings object, so they
        take effect on the next request/ingest; structural ones (see ``_RESTART_REQUIRED_FIELDS``)
        are only persisted and need a restart.
        """
        if not updates:
            raise ValueError("Пустой набор изменений")

        normalized: dict[str, str] = {}
        unknown: list[str] = []
        for key, val in updates.items():
            field = self._to_field(key)
            if field not in Settings.model_fields:
                unknown.append(key)
            else:
                normalized[field] = str(val)
        if unknown:
            raise ValueError("Неизвестные переменные: " + ", ".join(sorted(unknown)))

        # Persist first so the change survives a restart even if live-apply is a no-op.
        self._write_env_file({self._env_name(f): v for f, v in normalized.items()})

        live_applied: list[str] = []
        restart_required: list[str] = []
        for field, raw in normalized.items():
            if field in _RESTART_REQUIRED_FIELDS:
                restart_required.append(field)
                continue
            try:
                # Validate/coerce just this field ("32" -> int, "true" -> bool, JSON -> list).
                coerced = Settings(**{field: raw})
                setattr(self._settings, field, getattr(coerced, field))
                live_applied.append(field)
            except Exception as exc:  # noqa: BLE001
                log.warning("setting_live_apply_failed", field=field, error=str(exc))
                restart_required.append(field)

        log.info(
            "settings_updated",
            live_applied=live_applied,
            restart_required=restart_required,
            env_file=str(self._env_path),
        )
        return {
            "updated": [self._item(f) for f in normalized],
            "live_applied": sorted(live_applied),
            "restart_required": sorted(restart_required),
            "restart_needed": bool(restart_required),
            "env_file": str(self._env_path),
        }

    def _write_env_file(self, pairs: dict[str, str]) -> None:
        """Upsert ``KEY=value`` lines into the ``.env`` file, preserving comments and order."""
        existing = (
            self._env_path.read_text(encoding="utf-8").splitlines()
            if self._env_path.exists()
            else []
        )
        remaining = dict(pairs)
        out: list[str] = []
        for line in existing:
            stripped = line.strip()
            if stripped and not stripped.startswith("#") and "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in remaining:
                    out.append(f"{key}={remaining.pop(key)}")
                    continue
            out.append(line)
        out.extend(f"{key}={val}" for key, val in remaining.items())
        self._env_path.parent.mkdir(parents=True, exist_ok=True)
        self._env_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    def log_file_exists(self) -> bool:
        return self._log_path.is_file()

    def build_filename(self, day: date | None, request_id: str | None) -> str:
        """Suggested download filename reflecting the active filters."""
        parts = ["logs"]
        if day is not None:
            parts.append(day.isoformat())
        if request_id:
            parts.append(request_id)
        return "_".join(parts) + ".log"

    def iter_formatted_logs(
        self, day: date | None = None, request_id: str | None = None
    ) -> Iterator[str]:
        """Yield readable log lines, filtered by day and/or request_id.

        Each JSON line is parsed and kept only if it matches every active filter:
        ``day`` compares against the date part of the ISO timestamp; ``request_id``
        matches exactly. Malformed lines are passed through verbatim when no filter is
        active and skipped otherwise.
        """
        day_prefix = day.isoformat() if day is not None else None
        filtering = day_prefix is not None or request_id is not None

        with self._log_path.open("r", encoding="utf-8") as fh:
            for raw_line in fh:
                line = raw_line.rstrip("\n")
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    if not filtering:
                        yield line + "\n"
                    continue

                if day_prefix is not None:
                    ts = str(entry.get(TIMESTAMP_KEY, ""))
                    if not ts.startswith(day_prefix):
                        continue
                if request_id is not None and entry.get("request_id") != request_id:
                    continue

                yield _format_entry(entry) + "\n"

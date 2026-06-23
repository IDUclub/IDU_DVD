"""System controller: read, filter and render the application log file.

The log file on disk is JSON lines (see ``src/common/logger``). This controller turns those
JSON lines into a human-readable stream, optionally filtered by **day** and/or **request_id**,
so the ``/system/logs`` endpoint can hand back a readable ``.log`` file.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Iterator

import structlog

from src.common.config import Settings
from src.common.logger import TIMESTAMP_KEY, log_file_path

log = structlog.get_logger(__name__)

# JSON keys rendered explicitly (in this order); everything else is appended as key=value.
_PRIMARY_KEYS = (TIMESTAMP_KEY, "level", "logger", "request_id", "event")


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
    """Access to the application log file for the system router."""

    def __init__(self, settings: Settings) -> None:
        self._log_path: Path = log_file_path(settings)

    @property
    def log_path(self) -> Path:
        return self._log_path

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

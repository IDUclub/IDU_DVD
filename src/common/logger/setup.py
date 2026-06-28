"""Central logging configuration for the application.

structlog is configured once (from ``init_dependencies``) so that every module's
``structlog.get_logger(__name__)`` shares the same pipeline. We route everything through the
stdlib ``logging`` so we can fan a single event out to two sinks with different renderers:

* a **file** sink — one JSON object per line (machine-readable, filterable by date / request_id
  via ``/system/logs``);
* a **stdout** sink — human-readable console lines for local runs and ``docker logs``.

``request_id`` is carried through ``structlog.contextvars`` (bound by the request-logging
middleware), so it lands in every event emitted while handling a request.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import structlog

from src.common.config import Settings

# Key under which the ISO timestamp is stored in each JSON log line (used by the
# /system/logs date filter). Keep in sync with SystemController.
TIMESTAMP_KEY = "timestamp"

# Processors shared by structlog-native events and stdlib ("foreign") records, run
# before the final per-sink renderer.
_SHARED_PROCESSORS: list = [
    structlog.contextvars.merge_contextvars,  # injects request_id & friends
    structlog.stdlib.add_logger_name,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True, key=TIMESTAMP_KEY),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
    structlog.processors.UnicodeDecoder(),
]


def log_file_path(settings: Settings) -> Path:
    """Absolute path of the JSON log file, creating its parent directory."""
    path = Path(settings.log_dir).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path / settings.log_file


def configure_logging(settings: Settings) -> None:
    """Configure structlog + stdlib logging. Idempotent — safe to call again on re-init."""
    log_path = log_file_path(settings)
    level = logging.getLevelName(settings.log_level.upper())
    if not isinstance(level, int):
        level = logging.INFO

    structlog.configure(
        processors=_SHARED_PROCESSORS
        + [
            # Hand the event dict to a stdlib formatter (file vs console) for rendering.
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    # File: compact JSON, one object per line.
    file_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.JSONRenderer(),
        ],
    )
    # Console: human-readable, key=value with aligned levels.
    console_formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty()),
        ],
    )

    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(file_formatter)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(console_formatter)

    root = logging.getLogger()
    # Replace handlers so repeated configuration doesn't duplicate output.
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    root.setLevel(level)

    # Route uvicorn's own loggers through our handlers instead of their defaults.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uv_logger = logging.getLogger(name)
        uv_logger.handlers.clear()
        uv_logger.propagate = True

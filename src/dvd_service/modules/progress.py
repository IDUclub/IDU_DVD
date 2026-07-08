"""Per-document ingestion progress, surfaced on the background job status.

The pipeline runs a fixed sequence of stages (see :mod:`dvd_service.services.dvd_service`).
:class:`Progress` writes the current stage, an optional sub-phase, and an in-stage request
counter into the :class:`~src.common.db.redis_client.JobStore`, so ``GET /documents/{job_id}``
can report "stage 1/8 structure-markup · boundaries · 3/7" while a document is being processed.

A *stage* is one entry of the fixed pipeline; a *phase* is a sub-step within a stage that runs
its own batch of LLM requests (e.g. ``structure-markup`` splits into boundary detection and one
or more semantic-merge passes). ``progress``/``progress_total`` count LLM requests within the
current stage — or within the current phase, when the stage has phases.

Progress reporting is best-effort: it never raises into the ingestion path, and it is a no-op
when there is no job to report against (e.g. unit tests that call the pipeline directly).
"""

from __future__ import annotations

from typing import Callable

import structlog

from src.common.db.redis_client import JobStore

log = structlog.get_logger(__name__)

# Callback handed to a chunked stage: (requests_done, requests_total, phase) within the current
# stage. ``phase`` is optional — pass it only for stages split into sub-phases.
ProgressFn = Callable[..., None]


class Progress:
    """Tracks the current pipeline stage and reports it into the job status."""

    def __init__(
        self,
        jobs: JobStore | None = None,
        job_id: str | None = None,
        total_stages: int = 0,
    ) -> None:
        self._jobs = jobs
        self._job_id = job_id
        self._total_stages = total_stages
        self._index = 0
        self._name: str | None = None
        self._phase: str | None = None

    def stage(self, name: str, items: int | None = None) -> None:
        """Advance to the next stage, clearing the phase and in-stage counter."""
        self._index += 1
        self._name = name
        self._phase = None
        self._emit(0, items)

    def advance(
        self, done: int, total: int | None = None, phase: str | None = None
    ) -> None:
        """Report request progress within the current stage (used as a :data:`ProgressFn`).

        ``phase`` names the sub-step for multi-phase stages; the counter resets per phase.
        """
        self._phase = phase
        self._emit(done, total)

    def _emit(self, done: int, total: int | None) -> None:
        if not (self._jobs and self._job_id):
            return
        try:
            self._jobs.update(
                self._job_id,
                stage=self._name,
                stage_index=self._index,
                stage_total=self._total_stages,
                phase=self._phase,
                progress=done,
                progress_total=total,
            )
        except Exception as exc:  # noqa: BLE001 — progress must never break ingestion
            log.warning("progress_update_failed", job_id=self._job_id, error=str(exc))

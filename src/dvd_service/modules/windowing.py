"""Windows over a list of parts and reconciliation of decisions across overlap (shared by all LLM stages)."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import TypeVar

import httpx
import structlog

from src.api_clients.base import ChatClient, LlmError
from src.common.config import settings

_T = TypeVar("_T")
_R = TypeVar("_R")


def map_concurrent(
    worker: Callable[[_T], _R], items: Iterable[_T], *, max_workers: int
) -> Iterator[_R]:
    """Run independent LLM windows concurrently while yielding results in input order."""
    values = list(items)
    workers = max(1, int(max_workers))
    if workers == 1:
        for item in values:
            yield worker(item)
        return
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dvd-llm") as pool:
        try:
            yield from pool.map(worker, values)
        except BaseException:
            pool.shutdown(wait=True, cancel_futures=True)
            raise


def make_windows(items, max_chars=None, overlap=None, max_items=10**9):
    """Windows [(start, end)] over item indices (each item has a 'text' field).

    ``max_items`` caps the number of parts per window — long arrays break structured output.
    """
    max_chars = settings.window_chars if max_chars is None else max_chars
    overlap = settings.overlap_blocks if overlap is None else overlap
    windows, start, n = [], 0, len(items)
    while start < n:
        size, end = 0, start
        while (
            end < n
            and (end - start) < max_items
            and (size + len(items[end]["text"]) <= max_chars or end == start)
        ):
            size += len(items[end]["text"]) + 1
            end += 1
        windows.append((start, end))
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return windows


def reconcile(windows_decisions):
    """Across overlapping windows, pick the decision where the item has more left context."""
    best: dict[int, tuple] = {}
    for start, dec in windows_decisions:
        for pos, val in dec.items():
            gi = start + pos
            prev = best.get(gi)
            if prev is None or pos > prev[1]:
                best[gi] = (val, pos)
    return {gi: v for gi, (v, _) in best.items()}


def chat_window(
    client: ChatClient, system: str, texts: list[str], schema: dict, key: str
) -> list[dict]:
    """Require one decision per input, retrying only the failed window up to three times."""
    if not texts:
        return []
    size = len(texts)
    bounded = deepcopy(schema)
    array = bounded["properties"][key]
    array.update(minItems=size, maxItems=size)
    array["items"]["properties"]["id"].update(minimum=0, maximum=size - 1)
    system += f"\nВерни ровно {size} элементов: каждый id от 0 до {size - 1} ровно один раз, включая последний."
    user = "\n".join(f"[{i}] {text}" for i, text in enumerate(texts))
    for attempt in range(1, 4):
        try:
            data = client.chat(system, user, bounded)
            rows = data.get(key) if isinstance(data, dict) else None
            if (
                not isinstance(rows, list)
                or len(rows) != size
                or any(
                    not isinstance(row, dict) or type(row.get("id")) is not int
                    for row in rows
                )
                or {row["id"] for row in rows} != set(range(size))
            ):
                raise LlmError(
                    f"Неполное окно {key}: ожидались уникальные id 0..{size - 1}"
                )
            return rows
        except Exception as exc:
            retryable = isinstance(
                exc, (LlmError, httpx.TransportError, ConnectionError, TimeoutError)
            )
            if isinstance(exc, httpx.HTTPStatusError):
                retryable = (
                    exc.response.status_code == 429 or exc.response.status_code >= 500
                )
            if not retryable or attempt == 3:
                raise LlmError(
                    f"LLM недоступен или вернул неполное окно {key} после {attempt} попыток: {exc}"
                ) from exc
            structlog.get_logger(__name__).warning(
                "llm_window_retry", stage=key, attempt=attempt, error=str(exc)
            )
            time.sleep(attempt)
    raise AssertionError("unreachable")

"""Windows over a list of parts and reconciliation of decisions across overlap (shared by all LLM stages)."""

from __future__ import annotations

from src.common.config import settings


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

"""Unit tests for src/dvd_service/modules/windowing — windowing + overlap reconciliation.

Covers: window sizing by char budget and item cap, overlap stepping, and picking the decision
with the most left context across overlapping windows.
"""

from __future__ import annotations

from src.dvd_service.modules.windowing import make_windows, reconcile


def _items(*lengths):
    return [{"text": "x" * n} for n in lengths]


class TestMakeWindows:
    def test_single_window_when_everything_fits(self):
        wins = make_windows(
            _items(10, 10, 10), max_chars=1000, overlap=0, max_items=100
        )
        assert wins == [(0, 3)]

    def test_splits_on_char_budget(self):
        wins = make_windows(_items(100, 100, 100), max_chars=150, overlap=0)
        # each window holds one item (second would exceed 150)
        assert wins == [(0, 1), (1, 2), (2, 3)]

    def test_max_items_caps_window_length(self):
        wins = make_windows(
            _items(1, 1, 1, 1, 1), max_chars=10_000, overlap=0, max_items=2
        )
        assert all(end - start <= 2 for start, end in wins)
        assert wins[0] == (0, 2)

    def test_overlap_revisits_last_item(self):
        # 4 items of 40 chars, budget 100 -> 2 items per window; overlap=1 re-includes the last
        wins = make_windows(_items(40, 40, 40, 40), max_chars=100, overlap=1)
        assert wins[0] == (0, 2)
        assert (
            wins[1][0] == wins[0][1] - 1
        )  # next window starts on the previous window's last item

    def test_oversized_single_item_still_emitted(self):
        wins = make_windows(_items(10_000), max_chars=100, overlap=0)
        assert wins == [(0, 1)]


class TestReconcile:
    def test_non_overlapping_passthrough(self):
        out = reconcile([(0, {0: "a", 1: "b"})])
        assert out == {0: "a", 1: "b"}

    def test_prefers_decision_with_more_left_context(self):
        # global index 2 appears in both windows; the second has larger local pos (more context)
        decisions = [(0, {2: "from_first"}), (2, {0: "from_second_low_ctx"})]
        out = reconcile(decisions)
        # window starting at 0 puts index 2 at pos 2; window starting at 2 puts it at pos 0
        assert out[2] == "from_first"

    def test_later_window_wins_when_it_has_more_context(self):
        decisions = [(2, {0: "low_ctx"}), (0, {2: "high_ctx"})]
        assert reconcile(decisions)[2] == "high_ctx"

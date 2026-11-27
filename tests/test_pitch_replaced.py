"""P3-h2 verdict (D64): pairing both pitches against the announced XI, and the rule."""

from __future__ import annotations

import pytest

from adatta.eval import pitch_replaced as pr


def _slots(ids: list[int]) -> list[dict[str, object]]:
    return [{"code": f"S{i}", "player_id": p, "line": "M"} for i, p in enumerate(ids)]


def test_a_replacement_that_starts_adds_one_to_the_overlap() -> None:
    last = _slots(list(range(1, 12)))
    rep = _slots(list(range(1, 11)) + [20])
    rep[-1]["replaced"] = 11
    started = set(range(1, 11)) | {20}
    codes = {p: f"S{i}" for i, p in enumerate(list(range(1, 11)) + [20])}
    p = pr.pair(1, 2, "england", last, rep, started, codes)
    assert (p.overlap_last, p.overlap_replaced, p.replacements, p.replaced_hits) == (10, 11, 1, 1)
    assert p.acc_last == pytest.approx(10 / 11) and p.acc_replaced == pytest.approx(1.0)
    q = pr.pair(1, 2, "england", last, rep, started, None)  # not positional: no accuracy
    assert q.acc_last is None and q.acc_replaced is None


def _pairs(n: int, gain_every: int) -> list[pr.Pair]:
    return [pr.Pair(i, 1, "spain" if i % 2 else "italy", 9, 9 + (i % gain_every == 0),
                    int(i % gain_every == 0), int(i % gain_every == 0), 0.8, 0.8)
            for i in range(n)]  # fmt: skip


def test_the_verdict_needs_enough_matches_and_an_interval_above_zero() -> None:
    v = pr.verdict(_pairs(pr.MIN_N, 5))
    assert v["passed"] and v["overlap"]["lo"] > 0 and v["replacement_hit_rate"] == 1.0
    assert not pr.verdict(_pairs(pr.MIN_N - 1, 5))["passed"]  # too few
    flat = [pr.Pair(i, 1, "spain", 9, 9, 0, 0, None, None) for i in range(pr.MIN_N)]
    assert not pr.verdict(flat)["passed"]  # no gain: the interval is not above 0
    worse = [
        pr.Pair(
            p.match_id,
            1,
            p.bundle,
            p.overlap_last,
            p.overlap_replaced,
            p.replacements,
            p.replaced_hits,
            0.8,
            0.78,
        )
        for p in _pairs(pr.MIN_N, 5)
    ]
    assert not pr.verdict(worse)["passed"]  # slot accuracy 0.02 worse
    assert "PASSES" in pr.report(v, pr.WINDOW_TO)

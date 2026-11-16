"""Stage B shootout winner rule (docs/BEYOND_LEAGUES_PLAN.md): complete kicks, the winner's
basis, and agreement with who advanced."""

from typing import Any

from adatta.adapters import api_football_stats as st
from adatta.features import shootouts as so

H, A = "home", "away"


def abab(home: str, away: str) -> list[so.Kick]:
    """Alternating kicks from two strings of 'x' (scored) and 'o' (missed), home first."""
    out: list[so.Kick] = []
    for i in range(max(len(home), len(away))):
        if i < len(home):
            out.append((H, home[i] == "x"))
        if i < len(away):
            out.append((A, away[i] == "x"))
    return out


def test_complete_kicks() -> None:
    assert so.kicks_score(abab("xxxxx", "xxxxo")) == (5, 4)  # decided at the last kick
    assert so.kicks_score(abab("xxx", "ooo")) == (3, 0)  # 3-0 after 3 each: over early
    assert so.kicks_score(abab("xxx", "ooo")[:5]) is None  # 3-0 after 3 v 2: not yet
    assert so.kicks_score(abab("xxxxxxo", "xxxxxxx")) == (6, 7)  # sudden death
    assert so.kicks_score(abab("xxxx", "xxxx")) is None  # not over yet
    assert so.kicks_score(abab("xxxx", "ooo")) is None  # a kick listed after the end
    assert so.kicks_score(abab("xxxxxx", "xxxxxx")) is None  # level in sudden death


def test_abba_order_counts_the_same() -> None:
    abba = [(H, True), (A, True), (A, True), (H, False), (H, True), (A, True), (A, True),
            (H, True), (H, True), (A, False)]  # fmt: skip
    assert so.kicks_score(abba) is None  # 4-4 after five each: not over
    abba_done = [(H, True), (A, False), (A, False), (H, True), (H, True), (A, False)]
    assert so.kicks_score(abba_done) == (3, 0)


def test_winner_basis_and_advancement() -> None:
    k = abab("xxxxx", "xxxxo")
    assert so.decide((5, 4), k) == so.Shootout(H, "score", (5, 4))
    assert so.decide((1, 0), abab("xxxx", "xxoo")[:8]).winner == H  # count differs: score wins
    assert so.decide((None, None), k) == so.Shootout(H, "kicks", (5, 4))
    assert so.decide((None, None), abab("xx", "xx")).basis == "unresolved"
    assert so.decide((4, 3), None, advanced=A).winner is None  # never guessed either way
    assert so.decide((4, 3), None, advanced=H).winner == H
    assert so.advanced_side(1, 2, {2, 9}) == A and so.advanced_side(1, 2, {9}) is None


def test_kicks_are_parsed_from_shootout_events_only() -> None:
    def ev(team: int, detail: str, comments: str | None) -> dict[str, Any]:
        return {"type": "Goal", "detail": detail, "comments": comments, "team": {"id": team},
                "time": {"elapsed": 120, "extra": 1}}  # fmt: skip

    fx: dict[str, Any] = {
        "teams": {"home": {"id": 10}, "away": {"id": 20}},
        "events": [ev(10, "Penalty", None),  # a penalty in extra time, not a kick
                   ev(10, "Penalty", st.SHOOTOUT), ev(20, "Missed Penalty", st.SHOOTOUT)],
    }  # fmt: skip
    assert st.shootout_kicks(fx) == [(H, True), (A, False)]
    assert st.shootout_kicks({"teams": fx["teams"], "events": fx["events"][:1]}) is None
    fx["events"].append(ev(99, "Penalty", st.SHOOTOUT))
    assert st.shootout_kicks(fx) is None

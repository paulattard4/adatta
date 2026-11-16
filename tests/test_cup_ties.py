"""Cup ties: legs and replays from the fixture list and the cup's format (Stage A, A0 (c))."""

from datetime import UTC, datetime, timedelta

import pytest

from adatta.features import cup_ties as ct

T0 = datetime(2022, 1, 8, 15, tzinfo=UTC)
FORMATS = {1: ct.LEG, 2: ct.REPLAY, 3: ct.NONE}


def m(mid: int, rnd: str, home: int, away: int, day: int, league: int = 1) -> ct.CupMatch:
    return ct.CupMatch(mid, league, 2021, rnd, home, away, T0 + timedelta(days=day))


def kinds(matches: list[ct.CupMatch]) -> dict[int, str]:
    ties, _ = ct.label(matches, FORMATS)
    return {k: t.kind for k, t in ties.items()}


def test_two_legs_where_the_format_has_legs() -> None:
    matches = [m(1, "Semi-finals", 1, 2, 21), m(2, "Semi-finals", 2, 1, 0),
               m(3, "Final", 1, 3, 60)]  # fmt: skip
    ties, irregular = ct.label(matches, FORMATS)
    assert irregular == [] and kinds(matches) == {1: ct.LEG2, 2: ct.LEG1, 3: ct.SINGLE}
    assert ties[1].tie == ties[2].tie == (1, 2021, "Semi-finals", 1, 2)


def test_replays_labelled_or_not_where_the_format_has_replays() -> None:
    matches = [
        m(1, "3rd Round", 5, 6, 0, 2), m(2, "3rd Round Replays", 6, 5, 10, 2),
        m(3, "3rd Round Qualifying", 7, 8, 0, 2), m(4, "3rd Round Qualifying", 8, 7, 3, 2),
        m(5, "3rd Round", 9, 10, 0, 2),
    ]  # fmt: skip
    ties, irregular = ct.label(matches, FORMATS)
    assert irregular == []
    assert kinds(matches) == {1: ct.SINGLE, 2: ct.REPLAY, 3: ct.SINGLE, 4: ct.REPLAY,
                              5: ct.SINGLE}  # fmt: skip
    assert ties[1].tie == ties[2].tie and ties[3].tie == ties[4].tie


def test_second_meetings_the_format_does_not_allow_are_irregular() -> None:
    matches = [m(1, "Round of 16", 1, 2, 0, 3), m(2, "Round of 16", 2, 1, 7, 3),
               m(3, "Round of 16", 1, 2, 0, 1), m(4, "Round of 16", 1, 2, 7, 1),
               m(5, "Round of 16", 3, 4, 0, 99), m(6, "Round of 16", 4, 3, 7, 99)]  # fmt: skip
    ties, irregular = ct.label(matches, FORMATS)
    assert irregular == [1, 2, 3, 4, 5, 6]  # no legs at 3, same home side at 1, unknown 99
    assert {t.kind for t in ties.values()} == {ct.SINGLE}


def test_round_labels_and_shipped_formats() -> None:
    assert ct.is_replay("1st Round Qualifying Replays") and ct.is_replay("4th Round Replay")
    assert not ct.is_replay("Quarter-finals")
    assert ct.base_round("Extra Preliminary Round Replays") == "Extra Preliminary Round"
    assert ct.load_formats() == {45: ct.REPLAY, 143: ct.LEG, 137: ct.LEG, 81: ct.NONE,
                                 66: ct.NONE}  # fmt: skip


def test_unknown_second_meeting_value_is_refused(tmp_path: object) -> None:
    from pathlib import Path

    p = Path(str(tmp_path)) / "f.yml"
    p.write_text("formats:\n  1: {name: X, second_meeting: twice}\n")
    with pytest.raises(ValueError):
        ct.load_formats(p)

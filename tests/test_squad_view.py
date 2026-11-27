"""The squad and availability panel (docs/DASHBOARD_TASKS.md): captured status spells as
they stood at min(now, kickoff), when the feed was last checked, and lineups as announced."""

from datetime import datetime, timedelta
from pathlib import Path

from test_dashboard_api import ctx
from test_serve import AS_OF, K, World

from adatta.adapters.normalize import SOURCE_ID
from adatta.api import views
from adatta.db import models

NOW = K - timedelta(hours=2)


def _world(tmp_path: Path) -> tuple[World, int]:
    w = World(tmp_path)
    w.refit(AS_OF)
    mid = w.fixture(K)
    for pid, name in ((1, "Ana"), (2, "Bea"), (3, "Cal"), (4, "Dee"), (5, "Eli")):
        w.s.add(models.Player(player_id=pid, source_id=SOURCE_ID, source_ref=str(pid), name=name))
    w.s.flush()
    return w, mid


def _spell(w: World, mid: int, pid: int, team: int, status: str, start: datetime,
           end: datetime | None = None, reason: str | None = "Knee Injury") -> None:  # fmt: skip
    w.s.add(models.PlayerStatusSnapshot(
        player_id=pid, team_id=team, match_id=mid, source_id=SOURCE_ID, status=status,
        reason=reason, observed_at=start, valid_from=start, valid_to=end, superseded_at=end,
    ))  # fmt: skip


def _checked(w: World, at: datetime, outcome: str = "ok") -> None:
    w.s.add(models.CaptureLog(job="status", scope="39", run_at=at, outcome=outcome))


def _lineup(w: World, mid: int, team: int, at: datetime) -> None:
    xi = [{"player_ref": str(i), "name": f"P{i}", "number": i, "pos": "D", "grid": None}
          for i in range(11)]  # fmt: skip
    w.s.add(models.LineupSnapshot(match_id=mid, team_id=team, source_id=SOURCE_ID,
                                  observed_at=at, formation="4-4-2", coach_name="Coach",
                                  starters=xi, substitutes=xi[:3]))  # fmt: skip


def test_availability_is_as_of_the_match(tmp_path: Path) -> None:
    w, mid = _world(tmp_path)
    day = timedelta(days=1)
    _spell(w, mid, 1, 1, "missing", K - 3 * day)  # open: shown
    _spell(w, mid, 2, 2, "questionable", K - 2 * day, reason=None)  # open: shown, doubtful
    _spell(w, mid, 3, 1, "missing", K - 3 * day, end=K - day)  # cleared before: never
    _spell(w, mid, 4, 1, "missing", K + timedelta(hours=1))  # first seen after kickoff: never
    _spell(w, mid, 5, 2, "missing", K - 3 * day, end=K + timedelta(hours=1))  # closed later
    _checked(w, K - 5 * day)
    _checked(w, K - day)
    _checked(w, K - timedelta(hours=1), outcome="error")
    w.s.commit()
    after = K + timedelta(hours=3)
    sq = views.match_view(w.s, ctx(w), mid, after).squad
    assert sq is not None and sq.as_of == K
    assert [(a.name, a.status) for a in sq.home.absences] == [("Ana", "out")]
    assert sorted((a.name, a.status) for a in sq.away.absences) == [
        ("Bea", "doubtful"),
        ("Eli", "out"),
    ]
    assert sq.status_checked_at == K - day  # the last successful capture by kickoff
    # before kickoff, Dee is not listed yet and the spell ending later is still open
    early = views.match_view(w.s, ctx(w), mid, K - 2 * day - timedelta(hours=1)).squad
    assert early is not None
    assert [a.name for a in early.home.absences] == ["Ana", "Cal"]
    assert [a.name for a in early.away.absences] == ["Eli"]


def test_no_capture_is_not_no_absences(tmp_path: Path) -> None:
    w, mid = _world(tmp_path)
    w.s.commit()
    sq = views.match_view(w.s, ctx(w), mid, NOW).squad
    assert sq is not None and sq.status_checked_at is None
    assert sq.home.absences == [] and sq.away.absences == []


def test_lineups_as_announced(tmp_path: Path) -> None:
    w, mid = _world(tmp_path)
    w.s.commit()
    sq = views.match_view(w.s, ctx(w), mid, NOW).squad
    assert sq is not None and sq.home.lineup is None
    assert sq.lineup_note is not None and "75 minutes" in sq.lineup_note
    seen = K - timedelta(minutes=50)
    _lineup(w, mid, 1, seen)
    _lineup(w, mid, 2, seen)
    w.s.commit()
    # not shown before it was seen
    assert views.match_view(w.s, ctx(w), mid, NOW).squad.home.lineup is None  # type: ignore[union-attr]
    sq = views.match_view(w.s, ctx(w), mid, K - timedelta(minutes=10)).squad
    assert sq is not None and sq.lineup_note is None
    lu = sq.home.lineup
    assert lu is not None and lu.observed_at == seen and lu.formation == "4-4-2"
    assert len(lu.starters) == 11 and len(lu.bench) == 3 and lu.starters[3].name == "P3"


def test_started_match_without_a_lineup_says_not_captured(tmp_path: Path) -> None:
    w, mid = _world(tmp_path)
    w.s.commit()
    sq = views.match_view(w.s, ctx(w), mid, K + timedelta(minutes=5)).squad
    assert sq is not None and sq.lineup_note is not None and "Not captured" in sq.lineup_note


def test_youth_levels_list_their_players_like_senior_ones(tmp_path: Path) -> None:
    """D60 (owner, 2026-11-27): no youth restriction."""
    w, mid = _world(tmp_path)
    for tid in (1, 2):
        t = w.s.get(models.Team, tid)
        assert t is not None
        t.team_level = "u19"
    _spell(w, mid, 1, 1, "missing", K - timedelta(days=2))
    _lineup(w, mid, 1, K - timedelta(minutes=50))
    w.s.commit()
    sq = views.match_view(w.s, ctx(w), mid, K - timedelta(minutes=10)).squad
    assert sq is not None and sq.hidden_reason is None
    assert sq.home.absences != [] and sq.home.lineup is not None


def test_squad_leaves_the_not_available_list(tmp_path: Path) -> None:
    w, mid = _world(tmp_path)
    w.s.commit()
    v = views.match_view(w.s, ctx(w), mid, NOW)
    panels = [u.panel for u in v.unavailable]
    assert "Squad and lineup" not in panels and "Player projections" in panels

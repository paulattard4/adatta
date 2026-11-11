"""Referee identity across the provider's name formats, and the as-of history."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

from adatta.features import referees as R


def test_parse_strips_country_and_splits_initials() -> None:
    assert R.parse("Davide Massa, Italy") == R.Name("Davide Massa, Italy", (), ("davide", "massa"))
    assert R.parse("J.L. Munuera") == R.Name("J.L. Munuera", ("j", "l"), ("munuera",))
    assert R.parse("F. Badstübner").tokens == ("badstubner",)


def test_compatible_on_real_formats() -> None:
    p = R.parse
    assert R.compatible(p("D. Massa"), p("Davide Massa, Italy"))
    assert R.compatible(p("M. Ortiz"), p("Miguel Angel Ortiz Arias, Spain"))
    assert R.compatible(p("Miguel Ortiz"), p("Miguel Angel Ortiz Arias, Spain"))
    assert R.compatible(p("César Soto"), p("Cesar Soto Grado, Spain"))
    assert not R.compatible(p("D. Massa"), p("Daniele Doveri, Italy"))
    assert not R.compatible(p("A. Taylor"), p("Paul Tierney"))
    # Two different Spanish referees share a surname token.
    assert not R.compatible(p("Juan Martinez Munuera"), p("Jose Maria Sanchez Martinez"))


def test_resolve_merges_formats_and_leaves_bridges_unmerged() -> None:
    names = [
        "D. Massa", "Davide Massa, Italy", "M. Guida", "Marco Guida, Italy",
        "Miguel Ortiz", "M. Ortiz", "Miguel Angel Ortiz Arias, Spain",
        "J. Martinez", "Juan Martinez Munuera, Spain", "José María Sánchez Martínez, Spain",
    ]  # fmt: skip
    r = R.resolve(names, "X")
    assert r.identity["D. Massa"] == r.identity["Davide Massa, Italy"] == "X|Davide Massa"
    assert r.identity["M. Guida"] != r.identity["D. Massa"]
    assert len({r.identity[n] for n in ("Miguel Ortiz", "M. Ortiz")}) == 1
    assert r.identity["M. Ortiz"] == "X|Miguel Angel Ortiz Arias"
    # "J. Martinez" fits both: it stays apart, and both full names keep their own identity.
    assert r.review == ["J. Martinez"]
    assert r.identity["J. Martinez"] == "X|J. Martinez"
    assert (
        r.identity["Juan Martinez Munuera, Spain"]
        != r.identity["José María Sánchez Martínez, Spain"]
    )


def test_overrides_merge_first_name_variants() -> None:
    names = ["Andrew Madley", "Andy Madley, England", "A. Madley"]
    assert len(set(R.resolve(names, "E").identity.values())) == 3  # A. Madley bridges
    over = {"Andy Madley, England": "Andrew Madley"}
    r = R.resolve(names, "E", over)
    assert set(r.identity.values()) == {"E|Andrew Madley"} and r.review == []


def test_history_uses_only_matches_final_by_as_of() -> None:
    t0 = datetime(2024, 1, 6, 15, tzinfo=UTC)
    rows = [
        R.Officiated("Italy", 1, t0, "D. Massa", 4, 20),
        R.Officiated("Italy", 1, t0 + timedelta(days=7), "Davide Massa, Italy", 6, 30),
        R.Officiated("Italy", 1, t0 + timedelta(days=7), None, 2, 10),  # no referee: pool only
        R.Officiated("Italy", 1, t0 + timedelta(days=14), "D. Massa", 100, 100),
    ]
    h = R.RefereeHistory(rows)
    rid = h.identify("Italy", "D. Massa")
    as_of = t0 + timedelta(days=14) + R.RESULT_LAG - timedelta(minutes=1)  # third not final
    assert h.referee(rid, as_of) == (2, 5.0, 25.0)
    assert h.pool(1, as_of) == (3, 4.0, 20.0)
    assert h.referee(rid, t0) == (0, None, None)
    # A name first seen now joins the identity the other formats formed.
    assert h.identify("Italy", "Davide Massa") == rid


def test_league_override_splits_one_short_name_between_two_referees(tmp_path: Path) -> None:
    cfg = tmp_path / "overrides.yml"
    cfg.write_text(
        'Spain:\n  "J. Sanchez": Jose Maria Sanchez Martinez\n'
        'by_league:\n  "141":\n    "J. Sanchez": Jose Antonio Sanchez Villalobos\n'
    )
    over, league = R.load_overrides(cfg), R.load_league_overrides(cfg)
    assert over == {"Spain": {"J. Sanchez": "Jose Maria Sanchez Martinez"}}
    t0 = datetime(2026, 2, 7, 15, tzinfo=UTC)
    rows = [
        R.Officiated("Spain", 1, t0, "Jose Maria Sanchez Martinez, Spain", 4, 20, "140"),
        R.Officiated("Spain", 1, t0 + timedelta(days=7), "J. Sanchez", 6, 30, "140"),
        R.Officiated("Spain", 2, t0 + timedelta(days=7), "J. Sanchez", 2, 10, "141"),
        R.Officiated("Spain", 2, t0, "Jose Antonio Sanchez Villalobos, Spain", 8, 40, "141"),
    ]
    h = R.RefereeHistory(rows, over, league)
    top = h.identify("Spain", "J. Sanchez", "140")
    second = h.identify("Spain", "J. Sanchez", "141")
    assert top == "Spain|Jose Maria Sanchez Martinez"
    assert second == "Spain|Jose Antonio Sanchez Villalobos"
    later = t0 + timedelta(days=30)
    assert h.referee(top, later) == (2, 5.0, 25.0)
    assert h.referee(second, later) == (2, 5.0, 25.0)

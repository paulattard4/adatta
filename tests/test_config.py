from adatta.config import load_competitions


def test_competitions_load_and_sorted() -> None:
    comps = load_competitions()
    assert len(comps) >= 10
    assert comps == sorted(comps, key=lambda c: (c.priority, c.league_id))
    assert len({c.league_id for c in comps}) == len(comps)


def test_national_competitions_are_senior_and_flagged() -> None:
    comps = load_competitions("config/national_competitions.yml")
    assert comps and all(c.national and c.level == "senior" for c in comps)
    assert len({c.league_id for c in comps}) == len(comps)
    clubs = {c.league_id for c in load_competitions()}
    assert not clubs & {c.league_id for c in comps}  # no league in both lists
    assert not any(c.national for c in load_competitions())


def test_ensure_competition_sets_is_national() -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from adatta.adapters import normalize
    from adatta.db.models import Base

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as s:
        normalize.ensure_source(s)

        def comp(league_id: int, national: bool = False) -> bool:
            return normalize.ensure_competition(
                s, league_id=league_id, name="x", country="World", level="senior",
                tier="national", is_national=national,
            ).is_national  # fmt: skip

        assert not comp(39)
        assert comp(10, national=True)
        # a row created before the flag existed is upgraded, never downgraded
        assert comp(39, national=True)
        assert comp(39)

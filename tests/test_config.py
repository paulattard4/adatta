from adatta.config import load_competitions


def test_competitions_load_and_sorted() -> None:
    comps = load_competitions()
    assert len(comps) >= 10
    assert comps == sorted(comps, key=lambda c: (c.priority, c.league_id))
    assert len({c.league_id for c in comps}) == len(comps)

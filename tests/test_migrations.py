import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


@pytest.fixture
def cfg(tmp_path: Path) -> Iterator[tuple[Config, str]]:
    url = f"sqlite:///{tmp_path}/m.sqlite"
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    from adatta.config import get_settings

    get_settings.cache_clear()
    yield Config("alembic.ini"), url
    if old is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old
    get_settings.cache_clear()


def test_upgrade_and_downgrade(cfg: tuple[Config, str]) -> None:
    c, url = cfg
    command.upgrade(c, "head")
    insp = inspect(create_engine(url))
    tables = set(insp.get_table_names())
    assert {"match", "match_result", "raw_payload", "competition_source_capability"} <= tables
    assert {"odds_capture", "fixture_snapshot", "player_status_snapshot", "lineup_snapshot",
            "capture_log", "match_detail", "competition_bundle", "player_registration",
            "override_parametric", "override_structural", "model_run", "projection_run",
            "market_projection", "team_match_stat", "match_coach", "venue",
            "weather_snapshot"} <= tables  # fmt: skip
    assert "venue_id" in {col["name"] for col in insp.get_columns("match")}
    assert "capability" not in tables
    cols = {col["name"] for col in insp.get_columns("match_result")}
    assert {"home_goals_nt", "home_goals_et", "home_pens"} <= cols and "home_goals_ft" not in cols
    cap_cols = {col["name"] for col in insp.get_columns("competition_source_capability")}
    assert {"season_id", "status", "valid_from", "valid_to", "last_audited_at"} <= cap_cols
    command.downgrade(c, "0001")
    assert "capability" in set(inspect(create_engine(url)).get_table_names())
    command.downgrade(c, "base")
    assert "match" not in set(inspect(create_engine(url)).get_table_names())


def test_0002_refuses_to_drop_nonempty_capability(cfg: tuple[Config, str]) -> None:
    c, url = cfg
    command.upgrade(c, "0001")
    with create_engine(url).begin() as conn:
        conn.execute(text("INSERT INTO source VALUES ('api_football', '')"))
        conn.execute(
            text(
                "INSERT INTO competition VALUES (1,'api_football','39','PL','E','senior',0,'1','f')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO capability (competition_id, season_year, source_id, capability,"
                " coverage_ratio) VALUES (1, 2024, 'api_football', 'lineups', 1.0)"
            )
        )
    with pytest.raises(RuntimeError, match="refusing to drop"):
        command.upgrade(c, "head")

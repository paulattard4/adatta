import os
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_upgrade_and_downgrade(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/m.sqlite"
    os.environ["DATABASE_URL"] = url
    from adatta.config import get_settings

    get_settings.cache_clear()
    cfg = Config("alembic.ini")
    command.upgrade(cfg, "head")
    tables = set(inspect(create_engine(url)).get_table_names())
    assert {"match", "match_result", "raw_payload", "capability", "odds_snapshot"} <= tables
    command.downgrade(cfg, "base")
    assert "match" not in set(inspect(create_engine(url)).get_table_names())
    get_settings.cache_clear()

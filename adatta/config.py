from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    api_football_key: str = ""
    api_football_base_url: str = "https://v3.football.api-sports.io"
    api_football_daily_limit: int = 7500
    api_football_per_minute_limit: int = 300

    database_url: str = "sqlite:///data/dev.sqlite"

    r2_endpoint_url: str = ""
    r2_access_key_id: str = ""
    r2_secret_access_key: str = ""
    r2_bucket: str = "adatta-raw"
    storage_backend: Literal["r2", "local"] = "local"

    alert_webhook_url: str = ""  # Slack-compatible incoming webhook; optional


@lru_cache
def get_settings() -> Settings:
    return Settings()


class Competition(BaseModel):
    name: str
    country: str
    league_id: int
    level: str
    tier: int | str
    priority: int
    seasons: list[int]
    # False: fixture calendar only (no post-match detail), for competitions loaded as context,
    # e.g. European and League Cup games for congestion (§6.2). Never modelled.
    detail: bool = True
    # National-team competition (spec §10.4, D35 Stage D): its teams are national sides and it
    # is fitted apart from the club model. Sets competition.is_national.
    national: bool = False


def load_competitions(path: Path | str = "config/competitions.yml") -> list[Competition]:
    raw = yaml.safe_load(Path(path).read_text())
    comps = [Competition(**row) for row in raw["competitions"]]
    return sorted(comps, key=lambda c: (c.priority, c.league_id))

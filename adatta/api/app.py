"""Dashboard API: read-only FastAPI over Postgres and the stored refit draws.

    uv run python -m adatta.api            # http://127.0.0.1:8000, local only

Local only for now (owner decision 2026-11-11): it binds to 127.0.0.1 and has no sign-in,
so it must not be exposed until auth (§17.1) is in front of it."""

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from adatta.api import schemas as sc
from adatta.api import views

HOST = "127.0.0.1"
PORT = 8000


def readonly_engine(url: str) -> Engine:
    """Every transaction is READ ONLY on Postgres: the dashboard cannot write."""
    opts = {"postgresql_readonly": True} if url.startswith("postgresql") else {}
    return create_engine(url, pool_pre_ping=True, execution_options=opts)


def create_app(sessions: Callable[[], Session], ctx: views.Context,
               clock: Callable[[], datetime] = lambda: datetime.now(UTC)) -> FastAPI:  # fmt: skip
    app = FastAPI(title="adatta dashboard API", docs_url=None, redoc_url=None)

    def session() -> Iterator[Session]:
        s = sessions()
        try:
            yield s
        finally:
            s.rollback()
            s.close()

    Db = Annotated[Session, Depends(session)]

    @app.get("/api/fixtures", response_model=sc.FixtureList)
    def fixtures(db: Db, start: datetime | None = None, end: datetime | None = None,
                 competition_id: int | None = None,
                 team_level: Annotated[str | None, Query(max_length=32)] = None,
                 ) -> sc.FixtureList:  # fmt: skip
        return views.fixture_list(db, ctx, clock(), start, end, competition_id, team_level)

    @app.get("/api/matches/{match_id}", response_model=sc.MatchView)
    def match(db: Db, match_id: int) -> sc.MatchView:
        try:
            return views.match_view(db, ctx, match_id, clock())
        except KeyError:
            raise HTTPException(404, "not a projected-bundle match") from None

    @app.get("/api/played", response_model=sc.PlayedList)
    def played(db: Db, start: datetime | None = None, end: datetime | None = None,
               competition_id: int | None = None) -> sc.PlayedList:  # fmt: skip
        return views.played(db, ctx, clock(), start, end, competition_id)

    return app


def production_app() -> FastAPI:
    from adatta.adapters.storage import make_store
    from adatta.config import get_settings
    from adatta.eval import harness
    from adatta.model.backtest import production_spec

    settings = get_settings()
    cfg = harness.load_config()
    ctx = views.Context(bundles=cfg.bundles, model_name=production_spec(cfg).model_name,
                        max_goals=cfg.max_goals, store=make_store(settings))  # fmt: skip
    factory = sessionmaker(readonly_engine(settings.database_url), expire_on_commit=False)
    return create_app(factory, ctx)

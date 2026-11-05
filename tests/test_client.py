import httpx
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from adatta.adapters.api_football import ApiFootballClient
from adatta.adapters.quota import QuotaPlanner
from adatta.adapters.storage import LocalStore
from adatta.config import Settings
from adatta.db import models
from adatta.db.models import Base


def test_fetch_on_empty_db_seeds_source_and_stores_payload(tmp_path) -> None:  # type: ignore[no-untyped-def]
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    http = httpx.Client(
        base_url="https://example.test",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(
                200, json={"response": {}}, headers={"x-ratelimit-requests-remaining": "7499"}
            )
        ),
    )
    client = ApiFootballClient(Settings(), LocalStore(tmp_path), QuotaPlanner(7500, 300), http=http)
    with Session(engine) as s:
        client.status(s)
        client.status(s)  # second identical fetch must not duplicate the index row
        s.commit()
        assert s.scalar(select(models.Source.source_id)) == "api_football"
        assert len(s.scalars(select(models.RawPayload)).all()) == 1

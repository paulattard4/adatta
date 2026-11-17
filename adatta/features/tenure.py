"""P3-c (b): Manager and tenure from `match_coach` (docs/PHASE3_TASKS.md, spec §7.3).

A tenure is a run of consecutive team matches under one coach, so a coach who leaves and
returns has two. Start and end are the first and last match in the run, exact to the match
(the provider's `/coachs` dates are month-only and incomplete, register 3c.5).

As-of: a tenure's end is not known while it runs. Features read the coach of the team's
last match final by the as-of time and the tenure's length up to then (adatta/features/
formation.py), never this table's end dates; the table is for reporting and serving.

CLI: `python -m adatta.features.tenure build [--dry-run]`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import polars as pl
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.db import models

ROW_SCHEMA: dict[str, Any] = {
    "match_id": pl.Int64, "team_id": pl.Int64, "coach_ref": pl.Utf8, "coach_name": pl.Utf8,
    "source_id": pl.Utf8, "observed_at": pl.Datetime("us", "UTC"),
    "kickoff": pl.Datetime("us", "UTC"),
}  # fmt: skip


def coach_rows(session: Session, now: datetime | None = None) -> pl.DataFrame:
    """Current `match_coach` rows of matches kicked off by `now`, with the kickoff."""
    K, M = models.MatchCoach, models.Match
    now = now or datetime.now(UTC)
    rows = session.execute(
        select(K.match_id, K.team_id, K.coach_ref, K.coach_name, K.source_id, K.observed_at,
               M.kickoff_time)
        .join(M, M.match_id == K.match_id)
        .where(K.superseded_at.is_(None), M.kickoff_time <= now)
    ).all()  # fmt: skip
    return pl.DataFrame(rows, schema=ROW_SCHEMA, orient="row")


def tenures(rows: pl.DataFrame) -> pl.DataFrame:
    """One row per run of consecutive team matches under one coach."""
    r = rows.sort("team_id", "kickoff", "match_id").with_columns(
        new=(pl.col("coach_ref") != pl.col("coach_ref").shift(1).over("team_id")).fill_null(True)
    )
    r = r.with_columns(run=pl.col("new").cum_sum().over("team_id"))
    t = r.group_by("team_id", "run").agg(
        coach_ref=pl.col("coach_ref").first(), source_id=pl.col("source_id").first(),
        first_match_id=pl.col("match_id").first(), last_match_id=pl.col("match_id").last(),
        start=pl.col("kickoff").first(), end=pl.col("kickoff").last(), n_matches=pl.len(),
        observed_at=pl.col("observed_at").max(),
    )  # fmt: skip
    last = t.group_by("team_id").agg(pl.col("run").max().alias("last_run"))
    return (
        t.join(last, on="team_id")
        .with_columns(open=pl.col("run") == pl.col("last_run"))
        .drop("last_run")
        .sort("team_id", "run")
    )


def check(rows: pl.DataFrame, ten: pl.DataFrame) -> dict[str, Any]:
    """Every coach row is in exactly one tenure; a team's tenures don't overlap."""
    covered = int(ten["n_matches"].sum()) == len(rows)
    nxt = ten.sort("team_id", "run").with_columns(
        next_start=pl.col("start").shift(-1).over("team_id")
    )
    overlap = int(nxt.filter(pl.col("next_start") < pl.col("end")).height)
    return {
        "coach_rows": len(rows), "tenures": len(ten), "managers": ten["coach_ref"].n_unique(),
        "every_row_once": covered, "overlaps": overlap, "passed": covered and overlap == 0,
    }  # fmt: skip


def apply(session: Session, rows: pl.DataFrame, ten: pl.DataFrame,
          now: datetime | None = None) -> dict[str, int]:  # fmt: skip
    """Upsert managers; insert new tenures and supersede ones that grew or closed."""
    now = now or datetime.now(UTC)
    Mg, T = models.Manager, models.ManagerTenure
    names = rows.sort("kickoff").group_by("coach_ref").agg(
        pl.col("coach_name").drop_nulls().last(), pl.col("source_id").first(),
        pl.col("observed_at").min(),
    )  # fmt: skip
    known = {r: i for r, i in session.execute(select(Mg.source_ref, Mg.manager_id))}
    added = 0
    for n in names.iter_rows(named=True):
        if n["coach_ref"] not in known:
            m = Mg(source_id=n["source_id"], source_ref=n["coach_ref"], name=n["coach_name"],
                   observed_at=n["observed_at"], valid_from=n["observed_at"])  # fmt: skip
            session.add(m)
            session.flush()
            known[n["coach_ref"]] = m.manager_id
            added += 1
    current = {
        (t.manager_id, t.team_id, t.first_match_id): t
        for t in session.scalars(select(T).where(T.superseded_at.is_(None)))
    }
    new = changed = same = 0
    for t in ten.iter_rows(named=True):
        mid = known[t["coach_ref"]]
        valid_to = None if t["open"] else t["end"]
        old = current.get((mid, t["team_id"], t["first_match_id"]))
        if old is not None:
            if (old.last_match_id, old.n_matches, old.valid_to) == (
                t["last_match_id"],
                t["n_matches"],
                valid_to,
            ):
                same += 1
                continue
            old.superseded_at = now
            changed += 1
        else:
            new += 1
        session.add(T(manager_id=mid, team_id=t["team_id"], source_id=t["source_id"],
                      first_match_id=t["first_match_id"], last_match_id=t["last_match_id"],
                      n_matches=t["n_matches"], observed_at=t["observed_at"],
                      valid_from=t["start"], valid_to=valid_to))  # fmt: skip
    session.flush()
    return {"managers_added": added, "tenures_new": new, "tenures_superseded": changed,
            "tenures_unchanged": same}  # fmt: skip


def main(argv: list[str] | None = None) -> None:
    import argparse
    import json

    from adatta.db.session import get_session

    p = argparse.ArgumentParser(prog="adatta.features.tenure")
    p.add_argument("cmd", choices=["build"])
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    with get_session() as session:
        rows = coach_rows(session)
        ten = tenures(rows)
        out: dict[str, Any] = check(rows, ten)
        if not args.dry_run and out["passed"]:
            out |= apply(session, rows, ten)
            session.commit()
    print(json.dumps(out))


if __name__ == "__main__":
    main()

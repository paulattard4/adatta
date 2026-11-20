"""Projected lineups: P3-b served for upcoming fixtures (docs/DASHBOARD_TASKS.md, "Projected
lineups"). P3-b as it passed on dev, refitted per bundle every 28 days on CI
(`model.selection serve-refit`), its draws stored here; a daily run projects every
regular-season bundle fixture of the next 7 days into `lineup_projection` (append-only).

P3-b uses neither odds nor the injury feed; the dashboard shows the feed beside the
projection, labelled as not used by the model. P3-f (docs/PHASE3_TASKS.md), the
status-informed overlay, is computed beside it in shadow and stored for its live verdict;
it is not shown before a pass. Numpy only (the fit is the model group's).
"""

from __future__ import annotations

import io
import json
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import polars as pl
from numpy.typing import NDArray
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters.normalize import SOURCE_ID
from adatta.adapters.storage import RawStore, try_get
from adatta.db import models
from adatta.features import selection as sel

Arr = NDArray[np.float64]
PREFIX = "serving/lineups"
AHEAD = timedelta(days=7)  # fixtures projected this far ahead
REFIT_DAYS = 28  # D14, as P3-b on dev
NO_MATCH = {"PST", "CANC", "ABD", "AWD", "WO", "TBD"}


def _utc(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=UTC)


# ----------------------------------------------------------------------------- refits


def refit_key(bundle: str, as_of: datetime) -> str:
    return f"{PREFIX}/{bundle}/{as_of:%Y%m%dT%H%M}.npz"


def latest_key(bundle: str) -> str:
    return f"{PREFIX}/{bundle}/latest.json"


def encode(post: dict[str, Arr], design: Any, nu: dict[str, tuple[float, float]],
           meta: dict[str, Any]) -> bytes:  # fmt: skip
    """A serving refit: the thinned posterior draws, the design (means, sds, leagues), the
    newcomer allowances and the refit's metadata."""
    buf = io.BytesIO()
    extra = json.dumps({"design": asdict(design), "nu": nu, "meta": meta}, default=str)
    np.savez_compressed(buf, **{f"post__{k}": v for k, v in post.items()},  # type: ignore[arg-type]
                        extra=np.array(extra))  # fmt: skip
    return buf.getvalue()


def decode(data: bytes) -> tuple[dict[str, Arr], Any, dict[str, tuple[float, float]],
                                 dict[str, Any]]:  # fmt: skip
    from adatta.model.selection import Design

    with np.load(io.BytesIO(data)) as z:
        post = {k[6:]: z[k] for k in z.files if k.startswith("post__")}
        extra = json.loads(str(z["extra"]))
    nu = {k: (float(v[0]), float(v[1])) for k, v in extra["nu"].items()}
    return post, Design(**extra["design"]), nu, extra["meta"]


def latest(store: RawStore, bundle: str) -> tuple[str, datetime] | None:
    """The latest passing serving refit of a bundle: (key, as_of)."""
    raw = try_get(store, latest_key(bundle))
    if raw is None:
        return None
    x = json.loads(raw)
    return x["key"], _utc(datetime.fromisoformat(x["as_of"]))


def due(store: RawStore, bundle: str, now: datetime) -> bool:
    got = latest(store, bundle)
    return got is None or now - got[1] >= timedelta(days=REFIT_DAYS)


# ------------------------------------------------------------------------- projection


def upcoming(session: Session, leagues: list[int], now: datetime) -> pl.DataFrame:
    """Regular-season fixtures of these leagues kicking off in (now, now + AHEAD], with the
    projection's as-of time: kickoff - 24 h (P3-b's horizon) or now, whichever is earlier."""
    M, S, C = models.Match, models.Season, models.Competition
    rows = session.execute(
        select(M.match_id, C.source_ref, S.year, M.kickoff_time, M.home_team_id,
               M.away_team_id, M.status, M.round)
        .join(S, S.season_id == M.season_id).join(C, C.competition_id == S.competition_id)
        .where(C.source_id == SOURCE_ID, C.source_ref.in_([str(x) for x in leagues]),
               M.kickoff_time > now, M.kickoff_time <= now + AHEAD)
    ).all()  # fmt: skip
    out = []
    for mid, lg, yr, k, h, a, st, rnd in rows:
        if st in NO_MATCH or not (rnd or "").startswith("Regular Season"):
            continue
        k = _utc(k)
        out.append({"match_id": mid, "league_ref": str(lg), "season": int(yr), "kickoff": k,
                    "home_team_id": h, "away_team_id": a,
                    "as_of": min(now, k - sel.HORIZON)})  # fmt: skip
    return pl.DataFrame(out, schema={
        "match_id": pl.Int64, "league_ref": pl.Utf8, "season": pl.Int32,
        "kickoff": pl.Datetime("us", "UTC"), "home_team_id": pl.Int64,
        "away_team_id": pl.Int64, "as_of": pl.Datetime("us", "UTC"),
    })  # fmt: skip


def project(post: dict[str, Arr], design: Any, nu: dict[str, tuple[float, float]],
            rows: pl.DataFrame, teams: pl.DataFrame) -> pl.DataFrame:  # fmt: skip
    """P3-b's served numbers for pool rows: the dev path's coherent probabilities
    (`model.selection.predict`) and role minutes (`role_minutes`), and expected minutes
    from the minutes pmf."""
    from adatta.eval import selection as ev
    from adatta.model import selection as ms

    p, pmf = ms.predict(design, post, rows, teams, nu)
    e_start, e_bench = ms.role_minutes(design, post, rows)
    return rows.select("match_id", "team_id", "player_id", "pos").with_columns(
        p_start=pl.Series(p[:, 0]), p_bench=pl.Series(p[:, 1]), p_out=pl.Series(p[:, 2]),
        emin_start=pl.Series(e_start), emin_bench=pl.Series(e_bench),
        emin=pl.Series(pmf @ ev.MINUTES),
    )  # fmt: skip


# ------------------------------------------------------------------- P3-f (shadow)

PRIORS = {"missing": (1.0, 19.0), "questionable": (5.0, 5.0)}  # Beta priors on rho (P3-f (b))


def status_as_of(spells: pl.DataFrame, keys: pl.DataFrame) -> pl.DataFrame:
    """The open status spell of each (match_id, player_id) at its row's `as_of`: observed by
    then, not superseded and not closed by then. `spells`: match_id, player_id, status,
    observed_at, valid_to, superseded_at. Returns match_id, player_id, status."""
    j = keys.select("match_id", "player_id", "as_of").unique().join(
        spells, on=["match_id", "player_id"]
    ).filter(
        (pl.col("observed_at") <= pl.col("as_of"))
        & (pl.col("valid_to").is_null() | (pl.col("valid_to") > pl.col("as_of")))
        & (pl.col("superseded_at").is_null() | (pl.col("superseded_at") > pl.col("as_of")))
    )  # fmt: skip
    return j.sort("observed_at").group_by("match_id", "player_id").agg(
        pl.col("status").last()
    )  # fmt: skip


def rho(spells: pl.DataFrame, squads: pl.DataFrame, kickoffs: pl.DataFrame,
        t: datetime) -> dict[str, float]:  # fmt: skip
    """P3-f (b): per status, the posterior mean share of listed players still in the
    matchday squad, over listed player-matches final by t. A player-match's status is the
    one open at its kickoff - 24 h. `squads`: match_id, player_id of matchday squads;
    `kickoffs`: match_id, kickoff."""
    from adatta.features.referees import RESULT_LAG

    known = squads.select("match_id").unique()  # a final match with its squads captured
    final = kickoffs.filter(pl.col("kickoff") + RESULT_LAG <= t).join(known, on="match_id")
    keys = spells.select("match_id", "player_id").unique().join(final, on="match_id")
    keys = keys.with_columns(as_of=pl.col("kickoff") - sel.HORIZON)
    st = status_as_of(spells, keys)
    hit = st.join(squads.select("match_id", "player_id").unique().with_columns(_in=pl.lit(True)),
                  on=["match_id", "player_id"], how="left")  # fmt: skip
    out = {}
    for s_, (a, b) in PRIORS.items():
        g = hit.filter(pl.col("status") == s_)
        k = int(g["_in"].fill_null(False).sum())
        out[s_] = (a + k) / (a + b + g.height)
    return out


def project_f(post: dict[str, Arr], design: Any, nu: dict[str, tuple[float, float]],
              rows: pl.DataFrame, teams: pl.DataFrame, mult: Arr) -> Arr:  # fmt: skip
    """P3-f (a): P3-b's raw (start, bench, out) per draw with a listed player's start and
    bench scaled by `mult` (rho; 1 if unlisted) and the rest moved to out, then P3-b's
    coherence step per draw, as `model.selection.predict`. (n, 3) posterior means."""
    from adatta.eval import selection as ev
    from adatta.model import selection as ms

    raw = ms.raw_probs(design, post, rows)
    raw[:, :, 0] *= mult[None, :]
    raw[:, :, 1] *= mult[None, :]
    raw[:, :, 2] = 1.0 - raw[:, :, 0] - raw[:, :, 1]
    coh = np.stack([ev.coherent_all(rows, raw[i], teams, nu) for i in range(len(raw))])
    return np.asarray(coh.mean(axis=0), dtype=np.float64)


def load_spells(session: Session, match_ids: list[int] | None = None) -> pl.DataFrame:
    P = models.PlayerStatusSnapshot
    q = select(P.match_id, P.player_id, P.status, P.observed_at, P.valid_to, P.superseded_at)
    if match_ids is not None:
        q = q.where(P.match_id.in_(match_ids))
    rows = session.execute(q.where(P.match_id.is_not(None))).all()
    return pl.DataFrame(rows, orient="row", schema={
        "match_id": pl.Int64, "player_id": pl.Int64, "status": pl.Utf8,
        "observed_at": pl.Datetime("us", "UTC"), "valid_to": pl.Datetime("us", "UTC"),
        "superseded_at": pl.Datetime("us", "UTC"),
    })  # fmt: skip


def _kickoffs(session: Session, match_ids: list[int]) -> pl.DataFrame:
    M = models.Match
    rows = session.execute(select(M.match_id, M.kickoff_time).where(M.match_id.in_(match_ids))
                           ).all()  # fmt: skip
    return pl.DataFrame(
        [(m, _utc(k)) for m, k in rows],
        orient="row",
        schema={"match_id": pl.Int64, "kickoff": pl.Datetime("us", "UTC")},
    )


def run(session: Session, store: RawStore, bundles: dict[str, list[int]],
        now: datetime | None = None) -> dict[str, Any]:  # fmt: skip
    """Project the next AHEAD of every bundle with a passing serving refit into
    `lineup_projection`; returns counts (no match-level data)."""
    from adatta.eval import selection as ev

    now = now or datetime.now(UTC)
    report: dict[str, Any] = {"fixtures": 0, "rows": 0, "no_refit": [], "context_missing": 0}
    leagues = [lg for ls in bundles.values() for lg in ls]
    tg = upcoming(session, leagues, now)
    if tg.is_empty():
        return report
    season = max(int(x) for x in tg["season"].to_list())
    inp = ev.load_inputs(session, store, [str(x) for x in leagues], season, live=True)
    # P3-f shadow inputs: every captured spell, and the kickoffs of the matches they name
    spells = load_spells(session)
    kicks = (
        _kickoffs(session, spells["match_id"].unique().to_list())
        if spells.height
        else (pl.DataFrame(schema={"match_id": pl.Int64, "kickoff": pl.Datetime("us", "UTC")}))
    )
    session.rollback()  # end the read transaction before the build (Neon's idle timeout)
    squads = inp.appearances.select("match_id", "player_id")
    players, teams = sel.build(sel.Inputs(inp.appearances, inp.coaches, inp.context,
                                          tg.drop("kickoff")))  # fmt: skip
    if players.is_empty():
        return report
    rows_out = []
    for b, ls in bundles.items():
        got = latest(store, b)
        mine = [str(x) for x in ls]
        block = players.filter(pl.col("league_ref").is_in(mine))
        if block.is_empty():
            continue
        if got is None:
            report["no_refit"].append(b)
            continue
        key, as_of = got
        post, design, nu, _ = decode(store.get(key))
        tm = teams.join(block.select("match_id", "team_id").unique(), on=["match_id", "team_id"])
        proj = project(post, design, nu, block, tm)
        # P3-f shadow: status as of each fixture's as_of, rho as of that time
        keys = block.select("match_id", "player_id").join(tg.select("match_id", "as_of"),
                                                           on="match_id")  # fmt: skip
        st = status_as_of(spells, keys)
        block_st = block.join(st, on=["match_id", "player_id"], how="left").join(
            tg.select("match_id", "as_of"), on="match_id")  # fmt: skip
        rhos = {t: rho(spells, squads, kicks, t) for t in block_st["as_of"].unique().to_list()}
        r_m = np.array([rhos[t]["missing"] for t in block_st["as_of"].to_list()])
        r_q = np.array([rhos[t]["questionable"] for t in block_st["as_of"].to_list()])
        stat = block_st["status"].to_list()
        mult = np.where(
            np.array([x == "missing" for x in stat]),
            r_m,
            np.where(np.array([x == "questionable" for x in stat]), r_q, 1.0),
        )
        pf = project_f(post, design, nu, block, tm, mult)
        proj = proj.with_columns(
            status=pl.Series(stat, dtype=pl.Utf8), rho_missing=pl.Series(r_m),
            rho_questionable=pl.Series(r_q), p_start_f=pl.Series(pf[:, 0]),
            p_bench_f=pl.Series(pf[:, 1]), p_out_f=pl.Series(pf[:, 2]),
        )  # fmt: skip
        report["listed"] = report.get("listed", 0) + int(block_st["status"].is_not_null().sum())
        report["rho"] = {k: round(v, 4) for k, v in rhos[max(rhos)].items()}
        rows_out.append(proj.with_columns(refit_key=pl.lit(key), refit_as_of=pl.lit(as_of)))
        report["context_missing"] += int(block["rest_days"].is_nan().sum())
    if not rows_out:
        return report
    out = pl.concat(rows_out).join(
        tg.select(pl.col("match_id"), "as_of"), on="match_id", how="left"
    )  # fmt: skip
    session.add_all([
        models.LineupProjection(
            match_id=r["match_id"], team_id=r["team_id"], player_id=r["player_id"],
            generated_at=now, as_of=r["as_of"], refit_key=r["refit_key"],
            refit_as_of=r["refit_as_of"], pos=r["pos"], p_start=r["p_start"],
            p_bench=r["p_bench"], p_out=r["p_out"], emin_start=r["emin_start"],
            emin_bench=r["emin_bench"], emin=r["emin"], status=r["status"],
            rho_missing=r["rho_missing"], rho_questionable=r["rho_questionable"],
            p_start_f=r["p_start_f"], p_bench_f=r["p_bench_f"], p_out_f=r["p_out_f"],
        ) for r in out.iter_rows(named=True)
    ])  # fmt: skip
    session.flush()
    report["fixtures"] = int(out["match_id"].n_unique())
    report["rows"] = out.height
    return report

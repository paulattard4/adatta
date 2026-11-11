"""Match context as of a prediction time (spec §6.2, Layer 3b-e; register 2.8-2.24).

One row per match: everything the Layer 3 terms need, each value observable at
`as_of = kickoff - horizon`. The builders are the per-topic modules (travel, climatology,
importance, league_rules); this module joins them and adds congestion, referee, weather
choice, VAR and local kickoff hour.

Acceptance test (stated before building, docs/FEASIBILITY_REGISTER.md progress):
  leakage   no value uses a fact first observable after as_of, except the labelled D29
            weather stand-in (realised_proxy) (tests/test_context.py seeds a
            later result, deduction, forecast and referee and checks none reach the row);
  coverage  bundle league matches 2019 on, by league-season: travel, congestion (both
            directions), weather and kickoff hour >= 99%; importance wherever a zone rule
            exists (play-off rounds excepted); referee as D26 allows;
  nulls     only for the documented reasons below; never imputed.

Columns and their as-of rule:
  travel_*, venue_*      venue and home bases from published fixture venues (2.8-2.14).
  {side}_days_since_last days since the side's previous match in any loaded competition
                         (league, domestic cups, super cups, UEFA, Club World Cup) that kicked
                         off at or before as_of; null when the data holds none (2.16).
  {side}_days_to_next    days to the side's next match after this one, from final fixture
                         dates: D25, an optimistic look-ahead in dev and holdout (2.17);
                         {side}_next_kind league | cup | continental, {side}_next_match_id
                         so a model can weight it by that match's importance.
  weather_source         forecast (newest Open-Meteo snapshot observed <= as_of for this
                         kickoff hour: live capture, or the previous-runs forecasts of
                         weather_history from 2024-02-01) | realised_proxy (D29: ERA5 weather
                         on the day, kickoffs 2019-07-01 to 2024-01-31 at horizons <= 24 h
                         only; an optimistic look-ahead admitted for backtests) |
                         climatology (ERA5 2016-2018 at the venue) | null (venue not
                         placed); temperature_c, precipitation_mm, wind_speed_kmh,
                         forecast_lead_hours (null unless forecast) (2.12, 2.13).
  {side}_importance      dead_rubber | must_win | live from the table as of as_of; null for
                         cups, play-off rounds and seasons without a zone rule (2.20).
  referee, referee_basis snapshot: what the newest fixture_snapshot observed <= as_of names;
                         d26: no snapshot observed by as_of (before capture began), so the
                         final name (season fixture list, else the newest snapshot) is used
                         only when as_of >= kickoff - 24 h (D26); pool_prior: not known
                         (2.18, 2.19).
  referee_id             the referee identity (adatta/features/referees.py: name formats
                         merged per country, config/referee_overrides.yml); referee_matches,
                         referee_cards_pm, referee_fouls_pm: that referee's finished matches
                         in any domestic competition before as_of (null with no referee).
  pool_*                 the same over the competition's matches in the 365 days before
                         as_of: the pool prior's data, for every match (2.18).
  has_var                league-season fact from config/league_rules.yml; null for cups.
  kickoff_hour_local     hour at the venue (its IANA timezone); null if the venue is unplaced.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import polars as pl
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.open_meteo import MatchWeather
from adatta.adapters.storage import RawStore
from adatta.db import models
from adatta.features import climatology, importance, referees, travel, weather_history
from adatta.features.league_rules import Rules

DEFAULT_HORIZON = timedelta(hours=1)
REFEREE_LEAD = timedelta(hours=24)  # D26
NOT_PLAYED = frozenset({"PST", "CANC", "Canc", "WO"})
CONTINENTAL = frozenset({"Europe", "World"})
WEATHER_KICKOFF_TOLERANCE = timedelta(hours=1)

CONTEXT_SCHEMA: dict[str, pl.DataType | type[pl.DataType]] = {
    "match_id": pl.Int64,
    "as_of": pl.Datetime("us", "UTC"),
    "home_days_since_last": pl.Float64,
    "away_days_since_last": pl.Float64,
    "home_days_to_next": pl.Float64,
    "away_days_to_next": pl.Float64,
    "home_next_kind": pl.Utf8,
    "away_next_kind": pl.Utf8,
    "home_next_match_id": pl.Int64,
    "away_next_match_id": pl.Int64,
    "weather_source": pl.Utf8,
    "temperature_c": pl.Float64,
    "precipitation_mm": pl.Float64,
    "wind_speed_kmh": pl.Float64,
    "forecast_lead_hours": pl.Float64,
    "referee": pl.Utf8,
    "referee_basis": pl.Utf8,
    "referee_id": pl.Utf8,
    "referee_matches": pl.Int64,
    "referee_cards_pm": pl.Float64,
    "referee_fouls_pm": pl.Float64,
    "pool_matches": pl.Int64,
    "pool_cards_pm": pl.Float64,
    "pool_fouls_pm": pl.Float64,
    "has_var": pl.Boolean,
    "kickoff_hour_local": pl.Int64,
}


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _days(a: datetime, b: datetime) -> float:
    return round((b - a).total_seconds() / 86400, 3)


def competition_kind(comp: models.Competition) -> str:
    if comp.country in CONTINENTAL:
        return "continental"
    return "cup" if comp.tier == "cup" else "league"


class Calendar:
    """Every loaded match of a set of teams, by team, in kickoff order."""

    def __init__(self, session: Session, team_ids: Sequence[int]) -> None:
        M, S, C = models.Match, models.Season, models.Competition
        self.by_team: dict[int, list[tuple[datetime, int, str]]] = {}
        for part in normalize.chunks(sorted(set(team_ids))):
            for mid, h, a, ko, status, comp in session.execute(
                select(M.match_id, M.home_team_id, M.away_team_id, M.kickoff_time, M.status, C)
                .join(S, S.season_id == M.season_id)
                .join(C, C.competition_id == S.competition_id)
                .where(or_(M.home_team_id.in_(part), M.away_team_id.in_(part)))
            ):
                if status in NOT_PLAYED:
                    continue
                entry = (_utc(ko), mid, competition_kind(comp))
                for t in {h, a} & set(part):
                    self.by_team.setdefault(t, []).append(entry)
        for v in self.by_team.values():
            v.sort()
        self._kos = {t: [e[0] for e in v] for t, v in self.by_team.items()}

    def previous(self, team: int, as_of: datetime, match_id: int) -> datetime | None:
        """Kickoff of the team's last match at or before as_of, other than this one."""
        entries, kos = self.by_team.get(team, []), self._kos.get(team, [])
        i = bisect_right(kos, as_of)
        while i > 0:
            i -= 1
            if entries[i][1] != match_id:
                return entries[i][0]
        return None

    def next_after(
        self, team: int, kickoff: datetime, match_id: int
    ) -> tuple[datetime, int, str] | None:
        entries, kos = self.by_team.get(team, []), self._kos.get(team, [])
        for e in entries[bisect_left(kos, kickoff) :]:
            if e[1] != match_id and e[0] > kickoff:
                return e
        return None


def forecasts_as_of(
    session: Session, matches: Sequence[models.Match], as_of: dict[int, datetime]
) -> dict[int, models.WeatherSnapshot]:
    """{match_id: newest snapshot observed <= as_of whose kickoff is this match's (within
    WEATHER_KICKOFF_TOLERANCE)}."""
    W = models.WeatherSnapshot
    ko = {m.match_id: _utc(m.kickoff_time) for m in matches}
    out: dict[int, models.WeatherSnapshot] = {}
    for part in normalize.chunks(sorted(ko)):
        for w in session.scalars(select(W).where(W.match_id.in_(part)).order_by(W.observed_at)):
            if _utc(w.observed_at) > as_of[w.match_id]:
                continue
            if abs(_utc(w.kickoff_time) - ko[w.match_id]) > WEATHER_KICKOFF_TOLERANCE:
                continue
            if w.temperature_c is None:  # match hours past the forecast's range
                continue
            out[w.match_id] = w  # ordered by observed_at: the newest usable wins
    return out


def referees_as_of(
    session: Session, store: RawStore, matches: Sequence[models.Match],
    as_of: dict[int, datetime],
) -> dict[int, tuple[str | None, str]]:  # fmt: skip
    """{match_id: (name, basis)}: snapshot, d26 or pool_prior (see module docstring)."""
    F = models.FixtureSnapshot
    seen: dict[int, str | None] = {}  # newest snapshot observed <= as_of: its referee
    final: dict[int, str] = {}  # newest name in any snapshot (the provider's final name)
    for part in normalize.chunks(sorted(as_of)):
        for match_id, observed, name in session.execute(
            select(F.match_id, F.observed_at, F.referee)
            .where(F.match_id.in_(part))
            .order_by(F.observed_at)
        ):
            if _utc(observed) <= as_of[match_id]:
                seen[match_id] = name or None
            if name:
                final[match_id] = name
    lists = referees.fixture_referees(
        session, store, {m.season_id for m in matches if m.match_id not in seen}
    )
    out: dict[int, tuple[str | None, str]] = {}
    for m in matches:
        if m.match_id in seen:  # capture was watching by as_of: what it saw then
            name = seen[m.match_id]
            out[m.match_id] = (name, "snapshot") if name else (None, "pool_prior")
            continue
        name = lists.get(m.source_ref) or final.get(m.match_id)
        if name and as_of[m.match_id] >= _utc(m.kickoff_time) - REFEREE_LEAD:
            out[m.match_id] = (name, "d26")
        else:
            out[m.match_id] = (None, "pool_prior")
    return out


def context_features(
    session: Session, store: RawStore, matches: Sequence[models.Match], rules: Rules,
    horizon: timedelta = DEFAULT_HORIZON,
) -> pl.DataFrame:  # fmt: skip
    """The match context table: one row per match (see the module docstring)."""
    S, C, V = models.Season, models.Competition, models.Venue
    as_of = {m.match_id: _utc(m.kickoff_time) - horizon for m in matches}
    trav = travel.travel_features(session, matches)
    venue_of: dict[int, int | None] = dict(
        zip(trav["match_id"].to_list(), trav["venue_id"].to_list(), strict=True)
    )
    venue_ids = sorted({v for v in venue_of.values() if v is not None})
    venues = {
        v.venue_id: v
        for part in normalize.chunks(venue_ids)
        for v in session.scalars(select(V).where(V.venue_id.in_(part)))
    }
    cells = {
        climatology.cell(v.latitude, v.longitude)
        for v in venues.values()
        if v.geocode_status in climatology.USABLE
        and v.latitude is not None
        and v.longitude is not None
    }
    clim = climatology.match_climatology(
        session, matches, venue_of, climatology.load_cells(session, store, cells)
    ).drop("venue_id")
    imp = importance.importance_features(session, matches, rules, horizon)
    cal = Calendar(session, [t for m in matches for t in (m.home_team_id, m.away_team_id)])
    forecasts = forecasts_as_of(session, matches, as_of)
    realised: dict[int, MatchWeather] = {}
    if horizon <= weather_history.PROXY_MAX_HORIZON:  # D29 stand-in, backtests only
        early = [
            m for m in matches
            if m.match_id not in forecasts
            and weather_history.PROXY_FROM <= _utc(m.kickoff_time) < weather_history.AS_ISSUED_FROM
        ]  # fmt: skip
        realised = weather_history.load_realised(
            session, store, weather_history.targets(early, venue_of, venues)
        )
    named = referees_as_of(session, store, matches, as_of)
    history = referees.RefereeHistory.load(session, store)
    meta = {
        sid: (comp, year)
        for sid, comp, year in session.execute(
            select(S.season_id, C, S.year)
            .join(C, C.competition_id == S.competition_id)
            .where(S.season_id.in_(sorted({m.season_id for m in matches})))
        )
    }
    clim_rows = {r["match_id"]: r for r in clim.iter_rows(named=True)}
    rows: list[dict[str, object]] = []
    for m in matches:
        ko, t = _utc(m.kickoff_time), as_of[m.match_id]
        comp, year = meta[m.season_id]
        row: dict[str, object] = {"match_id": m.match_id, "as_of": t}
        for side, team in (("home", m.home_team_id), ("away", m.away_team_id)):
            prev = cal.previous(team, t, m.match_id)
            row[f"{side}_days_since_last"] = _days(prev, ko) if prev else None
            nxt = cal.next_after(team, ko, m.match_id)
            row[f"{side}_days_to_next"] = _days(ko, nxt[0]) if nxt else None
            row[f"{side}_next_kind"] = nxt[2] if nxt else None
            row[f"{side}_next_match_id"] = nxt[1] if nxt else None
        w = forecasts.get(m.match_id)
        c = clim_rows.get(m.match_id, {})
        if w is not None:
            row |= {
                "weather_source": "forecast", "temperature_c": w.temperature_c,
                "precipitation_mm": w.precipitation_mm, "wind_speed_kmh": w.wind_speed_kmh,
                "forecast_lead_hours": round(_days(_utc(w.observed_at), ko) * 24, 2),
            }  # fmt: skip
        elif (r := realised.get(m.match_id)) is not None:
            row |= {
                "weather_source": "realised_proxy", "temperature_c": r.temperature_c,
                "precipitation_mm": r.precipitation_mm, "wind_speed_kmh": r.wind_speed_kmh,
            }  # fmt: skip
        elif c.get("clim_temperature_c") is not None:
            row |= {
                "weather_source": "climatology", "temperature_c": c["clim_temperature_c"],
                "precipitation_mm": c["clim_precipitation_mm"],
                "wind_speed_kmh": c["clim_wind_speed_kmh"],
            }  # fmt: skip
        row["referee"], row["referee_basis"] = named[m.match_id]
        if comp.country not in CONTINENTAL:
            if row["referee"] is not None:
                rid = history.identify(comp.country, str(row["referee"]), comp.source_ref)
                n, cards, fouls = history.referee(rid, t)
                row |= {"referee_id": rid, "referee_matches": n, "referee_cards_pm": cards,
                        "referee_fouls_pm": fouls}  # fmt: skip
            n, cards, fouls = history.pool(comp.competition_id, t)
            row |= {"pool_matches": n, "pool_cards_pm": cards, "pool_fouls_pm": fouls}
        league = rules.league(int(comp.source_ref)) if competition_kind(comp) == "league" else None
        row["has_var"] = league.has_var(year) if league is not None else None
        vid = venue_of.get(m.match_id)
        v = venues.get(vid) if vid is not None else None
        if v is not None and v.timezone and v.geocode_status in travel.USABLE:
            row["kickoff_hour_local"] = ko.astimezone(ZoneInfo(v.timezone)).hour
        rows.append(row)
    ctx = pl.DataFrame(rows, schema=CONTEXT_SCHEMA)
    return ctx.join(trav, on="match_id", how="left").join(imp, on="match_id", how="left")

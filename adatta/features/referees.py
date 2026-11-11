"""Referee identity across name formats (spec §6.2, §8.4; register 2.18).

The provider writes one referee three ways, and switched during 2025/26: "D. Massa",
"Davide Massa, Italy", and (La Liga) "César Soto" for César Soto Grado. Distinct names per
league-season roughly doubled at the switch. Resolution, per country (a country's leagues
and cups share one referee pool; continental matches are not resolved):

  parse      drop a trailing ", Country"; fold accents, case and hyphens; split leading
             initials ("J. L.") from the surname tokens.
  link       two names are the same referee when compatible:
               initials + surname vs full name: the surname tokens appear in order in the
                 full name after its first token, and the initials match its first tokens;
               full vs full: equal, or the shorter's tokens are an ordered subsequence of
                 the longer's with the same first token ("Miguel Ortiz" and "Miguel Angel
                 Ortiz Arias");
               initials vs initials: same surname, one set of initials a prefix of the other.
  conflict   a name linked to full names that are not compatible with each other (two
             different people behind one "J. Martinez") bridges two referees: it stays
             unmerged, its own identity, and is listed for review.
  overrides  config/referee_overrides.yml maps a raw name to a canonical one by hand, first.
             Its by_league section does so for one league's matches only, before anything
             else: one short name used for two referees, each in their own league.

The identity is "Country|canonical name": the group's longest full name, else its name.

Identity uses every name the provider has written, later seasons included: it only says
which names are one person. The numbers attached to it never come from after as_of.

RefereeHistory: every finished match in the country's leagues and cups with a named
referee and both teams' card and foul counts; per identity and per competition, the
matches that kicked off at or before as_of - RESULT_LAG (final by as_of). Cards are yellow
plus red as the provider counts them, both teams together; fouls likewise. Raw means: the
shrinkage towards the pool is the model's (§8.4).
"""

from __future__ import annotations

import gzip
import json
import re
import unicodedata
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from adatta.adapters import normalize
from adatta.adapters.storage import RawStore
from adatta.db import models

_COUNTRY_SUFFIX = re.compile(r",\s*[^,]+$")
_INITIAL = re.compile(r"^[a-z]\.$")


@dataclass(frozen=True)
class Name:
    raw: str
    initials: tuple[str, ...]  # leading initials, when the name is abbreviated
    tokens: tuple[str, ...]  # surname tokens (abbreviated) or every token (full)

    @property
    def abbreviated(self) -> bool:
        return bool(self.initials)


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[\s\-]+", " ", text.lower()).strip()


def parse(raw: str) -> Name:
    base = _COUNTRY_SUFFIX.sub("", raw.strip())
    # "J.L. Munuera" -> "J. L. Munuera"
    words = fold(re.sub(r"\.(?=\S)", ". ", base)).split()
    initials: list[str] = []
    while words and _INITIAL.match(words[0]) and len(words) > 1:
        initials.append(words.pop(0)[0])
    return Name(raw, tuple(initials), tuple(words))


def _ordered_in(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    it = iter(long)
    return all(any(t == x for x in it) for t in short)


def _contiguous_in(short: tuple[str, ...], long: tuple[str, ...]) -> bool:
    n = len(short)
    return any(long[i : i + n] == short for i in range(len(long) - n + 1))


def compatible(a: Name, b: Name) -> bool:
    if a.abbreviated and b.abbreviated:
        k = min(len(a.initials), len(b.initials))
        return a.tokens == b.tokens and a.initials[:k] == b.initials[:k]
    if a.abbreviated or b.abbreviated:
        ab, full = (a, b) if a.abbreviated else (b, a)
        if len(full.tokens) <= len(ab.tokens) or not _contiguous_in(ab.tokens, full.tokens[1:]):
            return False
        firsts = full.tokens[: len(full.tokens) - len(ab.tokens)]
        return all(i < len(firsts) and firsts[i][0] == c for i, c in enumerate(ab.initials[:1]))
    short, long = sorted((a.tokens, b.tokens), key=len)
    return bool(short) and short[0] == long[0] and _ordered_in(short, long)


@dataclass(frozen=True)
class Resolution:
    identity: dict[str, str]  # raw name -> identity
    review: list[str]  # raw names left unmerged because they fit two different referees


def resolve(
    names: Iterable[str], country: str, overrides: Mapping[str, str] | None = None
) -> Resolution:
    """Group one country's raw referee names into identities."""
    overrides = overrides or {}
    raw = sorted(set(names))
    parsed = {r: parse(overrides.get(r, r)) for r in raw}
    keys = sorted({parsed[r] for r in raw}, key=lambda n: n.raw)
    by_raw = {n.raw: n for n in keys}
    edges: dict[str, set[str]] = defaultdict(set)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if compatible(a, b):
                edges[a.raw].add(b.raw)
                edges[b.raw].add(a.raw)
    review: set[str] = set()
    # A name linking full names that are not one person bridges two referees.
    # Only names more specific than it count: two shortenings of one full name don't
    # make the full name a bridge.
    for n in keys:
        fulls = [
            f for f in (by_raw[x] for x in edges[n.raw])
            if not f.abbreviated and (n.abbreviated or len(f.tokens) > len(n.tokens))
        ]  # fmt: skip
        if any(not compatible(f, g) for i, f in enumerate(fulls) for g in fulls[i + 1 :]):
            review.add(n.raw)
    group: dict[str, str] = {}
    for n in keys:
        if n.raw in group:
            continue
        stack, members = [n.raw], []
        while stack:
            x = stack.pop()
            if x in group:
                continue
            group[x] = n.raw
            members.append(x)
            if x not in review:
                stack += [y for y in edges[x] if y not in review and y not in group]
        fulls = [by_raw[m] for m in members if not by_raw[m].abbreviated]
        canonical = max(fulls, key=lambda f: (len(f.tokens), f.raw)) if fulls else by_raw[n.raw]
        label = _COUNTRY_SUFFIX.sub("", canonical.raw.strip())
        for m in members:
            group[m] = f"{country}|{label}"
    identity = {r: group[parsed[r].raw] for r in raw}
    return Resolution(identity, sorted(r for r in raw if parsed[r].raw in review))


OVERRIDES = "config/referee_overrides.yml"
BY_LEAGUE = "by_league"


def _read_overrides(path: Path | str) -> dict[str, object]:
    p = Path(path)
    return (yaml.safe_load(p.read_text()) or {}) if p.exists() else {}


def _str_map(v: object) -> dict[str, str]:
    return {str(a): str(b) for a, b in (v or {}).items()} if isinstance(v, dict) else {}


def load_overrides(path: Path | str = OVERRIDES) -> dict[str, dict[str, str]]:
    """{country: {raw name: canonical raw name}}; empty when the file does not exist."""
    data = _read_overrides(path)
    return {str(k): _str_map(v) for k, v in data.items() if k != BY_LEAGUE}


def load_league_overrides(path: Path | str = OVERRIDES) -> dict[str, dict[str, str]]:
    """{league id: {raw name: raw name}}: the by_league section, applied to that league's
    matches before resolution."""
    section = _read_overrides(path).get(BY_LEAGUE)
    return {str(k): _str_map(v) for k, v in section.items()} if isinstance(section, dict) else {}


RESULT_LAG = timedelta(hours=3)  # as importance.RESULT_LAG: a result is final 3 h after kickoff
POOL_DAYS = 365
CONTINENTAL = frozenset({"Europe", "World"})


def fixture_referees(
    session: Session, store: RawStore, season_ids: Iterable[int]
) -> dict[str, str]:
    """{fixture source_ref: referee} from the newest stored /fixtures list of each
    league-season given."""
    S, C, R = models.Season, models.Competition, models.RawPayload
    keys = {
        (str(ref), int(year))
        for ref, year in session.execute(
            select(C.source_ref, S.year)
            .join(C, C.competition_id == S.competition_id)
            .where(S.season_id.in_(sorted(set(season_ids))))
        )
    }
    newest: dict[tuple[str, int], str] = {}
    for params, key in session.execute(
        select(R.params, R.storage_key).where(R.endpoint == "/fixtures").order_by(R.fetched_at)
    ):
        if set(params) == {"league", "season"}:
            k = (str(params["league"]), int(params["season"]))
            if k in keys:
                newest[k] = key
    out: dict[str, str] = {}
    for key in newest.values():
        for f in json.loads(gzip.decompress(store.get(key)))["response"]:
            if f["fixture"].get("referee"):
                out[str(f["fixture"]["id"])] = f["fixture"]["referee"]
    return out


@dataclass(frozen=True)
class Officiated:
    country: str
    competition_id: int
    kickoff: datetime
    referee: str | None  # raw provider name
    cards: float
    fouls: float
    league: str | None = None  # the competition's provider id, for by_league overrides


@dataclass
class _Series:
    kickoffs: list[datetime] = field(default_factory=list)
    cards: list[float] = field(default_factory=lambda: [0.0])  # prefix sums
    fouls: list[float] = field(default_factory=lambda: [0.0])

    def add(self, o: Officiated) -> None:
        self.kickoffs.append(o.kickoff)
        self.cards.append(self.cards[-1] + o.cards)
        self.fouls.append(self.fouls[-1] + o.fouls)

    def window(
        self, start: datetime | None, end: datetime
    ) -> tuple[int, float | None, float | None]:
        hi = bisect_right(self.kickoffs, end)
        lo = bisect_right(self.kickoffs, start) if start is not None else 0
        n = hi - lo
        if n <= 0:
            return 0, None, None
        return (
            n,
            round((self.cards[hi] - self.cards[lo]) / n, 3),
            round((self.fouls[hi] - self.fouls[lo]) / n, 3),
        )


class RefereeHistory:
    """As-of card and foul history per referee identity and per competition."""

    def __init__(
        self,
        rows: Iterable[Officiated],
        overrides: Mapping[str, Mapping[str, str]] | None = None,
        league_overrides: Mapping[str, Mapping[str, str]] | None = None,
    ) -> None:
        rows = sorted(rows, key=lambda o: o.kickoff)
        self._overrides = overrides or {}
        self._league = league_overrides or {}
        names: dict[str, set[str]] = defaultdict(set)
        for o in rows:
            if o.referee:
                names[o.country].add(self.in_league(o.referee, o.league))
        self._identity: dict[tuple[str, str], str] = {}
        self.review: dict[str, list[str]] = {}
        for country, ns in names.items():
            self.add_names(country, ns)
        self._by_ref: dict[str, _Series] = defaultdict(_Series)
        self._by_comp: dict[int, _Series] = defaultdict(_Series)
        for o in rows:
            if o.referee:
                self._by_ref[self.identify(o.country, o.referee, o.league)].add(o)
            self._by_comp[o.competition_id].add(o)

    def add_names(self, country: str, names: Iterable[str]) -> None:
        """Resolve a country's names together, so groups form across seasons."""
        res = resolve(names, country, self._overrides.get(country))
        self._identity |= {(country, r): i for r, i in res.identity.items()}
        self.review[country] = res.review

    def in_league(self, raw: str, league: str | None) -> str:
        """The name after the by_league override of the match's league, if any."""
        return self._league.get(league, {}).get(raw, raw) if league is not None else raw

    def identify(self, country: str, raw: str, league: str | None = None) -> str:
        """The identity of a name (in a match of `league`, the provider's competition id);
        a name not seen in the history joins the one identity its compatible names share,
        else stands alone. Existing identities never change."""
        raw = self.in_league(raw, league)
        if (country, raw) in self._identity:
            return self._identity[(country, raw)]
        name = parse(self._overrides.get(country, {}).get(raw, raw))
        fits = {
            i for (c, r), i in self._identity.items()
            if c == country and compatible(name, parse(self._overrides.get(c, {}).get(r, r)))
        }  # fmt: skip
        found = (
            fits.pop() if len(fits) == 1 else f"{country}|{_COUNTRY_SUFFIX.sub('', raw.strip())}"
        )
        self._identity[(country, raw)] = found
        return found

    def identities(self) -> set[str]:
        return set(self._identity.values())

    def referee(self, identity: str, as_of: datetime) -> tuple[int, float | None, float | None]:
        """(matches, cards per match, fouls per match) before as_of, all competitions."""
        return self._by_ref[identity].window(None, as_of - RESULT_LAG)

    def pool(self, competition_id: int, as_of: datetime) -> tuple[int, float | None, float | None]:
        """The same over the competition's matches in the POOL_DAYS before as_of."""
        end = as_of - RESULT_LAG
        return self._by_comp[competition_id].window(end - timedelta(days=POOL_DAYS), end)

    @classmethod
    def load(cls, session: Session, store: RawStore) -> RefereeHistory:
        """Every finished match of the domestic competitions with team card and foul counts
        for both sides; referee from the season fixture lists, else the newest snapshot."""
        M, S, C, T, F = (
            models.Match, models.Season, models.Competition, models.TeamMatchStat,
            models.FixtureSnapshot,
        )  # fmt: skip
        seasons = {
            sid: (country, cid, str(league))
            for sid, country, cid, league in session.execute(
                select(S.season_id, C.country, C.competition_id, C.source_ref).join(
                    C, C.competition_id == S.competition_id
                )
            )
            if country not in CONTINENTAL
        }
        names = fixture_referees(session, store, seasons)
        for match_id, name in session.execute(
            select(F.match_id, F.referee).where(F.referee.is_not(None)).order_by(F.observed_at)
        ):
            if name:
                names[f"snap:{match_id}"] = name
        totals: dict[int, list[float]] = {}
        counts: dict[int, int] = defaultdict(int)
        for match_id, yellow, red, fouls in session.execute(
            select(T.match_id, T.yellow_cards, T.red_cards, T.fouls).where(
                T.superseded_at.is_(None)
            )
        ):
            if fouls is None:
                continue
            t = totals.setdefault(match_id, [0.0, 0.0])
            t[0] += (yellow or 0) + (red or 0)
            t[1] += fouls
            counts[match_id] += 1
        rows: list[Officiated] = []
        for part in normalize.chunks(list(seasons)):
            for match_id, ref, sid, ko in session.execute(
                select(M.match_id, M.source_ref, M.season_id, M.kickoff_time).where(
                    M.season_id.in_(part), M.status.in_(normalize.FINISHED)
                )
            ):
                if counts.get(match_id) != 2:
                    continue
                country, cid, league = seasons[sid]
                name = names.get(ref) or names.get(f"snap:{match_id}")
                ko = ko if ko.tzinfo else ko.replace(tzinfo=UTC)
                n_cards, n_fouls = totals[match_id]
                rows.append(Officiated(country, cid, ko, name, n_cards, n_fouls, league))
        return cls(rows, load_overrides(), load_league_overrides())

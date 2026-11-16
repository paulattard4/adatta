# Beyond domestic leagues: cups, continental and international football

Status: **agreed 2026-11-12 (D35)**. Stage 0 in progress; nothing else built yet.
The end goal is forecasting domestic cups, continental club competitions and national-team
football as well as the five league bundles. This plan orders the work, names what each
stage needs (data, model, evaluation), and states the gate before the next stage. Every
stage still follows CLAUDE.md: acceptance test stated before building, the locked holdout
untouched, graceful degradation instead of imputed fiction (principle 8).

## Where the spec puts this (§19.3) and the proposed change

| Work | Spec phase | Proposed |
|---|---|---|
| International v0 (team-only, from results) | "soon after 1b" | as the spec: start after Phase 2 |
| Domestic cup ties between bundle teams, 90' forecasts | not separate (part of 3d) | **Stage A, after Phase 2** |
| Knockout resolution (format rules, ET, shootouts, two legs) | 3d, after Phase 3 | **Stage B, before Phase 3** for team-only forecasts; lineup-aware later |
| Continental (UEFA) with Q_league | 6 (scale-out) | **Stage C, after B**, starting with the five bundles' leagues |
| Internationals v1 (squad offset), v2 (tournament simulator) | after 3 / after 3d | as the spec |

**Decision (D35, agreed 2026-11-12):** moving Stages A to C ahead of Phase 3 (squad and
player). Reason for: these competitions are the end goal, and §10.1 itself says
continental and international data "are scraped and modelled early" because they
calibrate the cross-league scale. Reason against: cup forecasts without lineups miss
rotation, which is largest in exactly these matches (early EFL Cup and Coppa Italia
rounds, dead group games). The plan handles this by keeping cup outputs team-only and
labelled, with a rotation proxy tested in Stage A, until Phase 3 adds lineups.

## Stage 0: data, can start now (no model change, quota only)

Starts in parallel with Phase 2, since it only loads data:
- **National-team matches** (new): World Cup and Euro finals and qualifiers, Nations
  League, Copa América, AFCON, Asian Cup, Gold Cup, friendlies, 2014 onwards (the spec's
  8-12-year window for internationals). Roughly 30 competitions x 12 seasons of
  `/fixtures` calls plus detail at 20 per call: about 1,000-1,500 requests, well inside
  the daily quota. Feasibility register entries first (coverage of lineups, xG,
  neutral venue).
- **Continental club matches with detail**: the UEFA competitions are loaded for their
  calendar only (`config/calendar_competitions.yml`, 2019 on, `detail: false`). Load
  detail and extend to 2016 so the warm-up seasons match the leagues.
- **Neutral venue flag** (an open item in `SOURCE_FEASIBILITY.md`): cup semi-finals and
  finals, continental finals, international tournaments. Derived from venue vs both
  teams' home venues (venues are already linked to matches), reviewed by hand where
  unclear, the same way as `config/venue_overrides.yml`.
- **Third tiers** (League One, 3. Liga, Serie C, National, Primera RFEF), as already
  planned: most early-round domestic cup ties involve a tier-3+ side, and without them
  those ties can't be forecast.

Gate: feasibility register rows verified for each new field; capability table filled.

Progress:
- [x] 2026-11-12: Champions, Europa and Conference League moved from the calendar-only
      list to `config/competitions.yml`, so the capture tick covers the current season
      (daily calendar and injuries, odds milestones, lineups, results every tick, weather).
      Cost estimate: about 50 matches in a UEFA matchday week at roughly 12 requests each
      (odds milestones, lineup polls, results, detail), about 650 requests that week,
      against current use of about 480 a day and a quota of about 7,500. Not modelled
      until Stage C; the projector serves bundle leagues only.
- [ ] Check the first ticks: 2026/27 UEFA fixtures loaded, odds and lineups captured on
      the next matchday (24-27 Nov), requests as estimated.
- [x] 2026-11-13: national-team feasibility. `config/national_competitions.yml` (22 senior
      men's competitions, `national: true`, which sets `competition.is_national`), audited
      with `adatta audit --config config/national_competitions.yml` (78 seasons, 245
      requests). Rows in `docs/SOURCE_FEASIBILITY.md` section 7. Findings: history starts
      2015-17 for most competitions, not 2014 (friendlies 2017, European WC qualifiers Sept
      2016; the Euro 2016 qualifiers are absent); results everywhere; lineups about 100% in
      competitive matches but only 35-38% of friendlies in 2025-26; xG only from 2024 in the
      top competitions, so v0 stays results-only as planned.
- [x] 2026-11-13: national-team live capture, in time for the November window (Nations
      League, CONCACAF Nations League, friendlies to 17-19 Nov). The capture tick loads
      `config/national_competitions.yml` with the club list; a competition is captured from
      30 days before its current season's start to 14 days after its end, otherwise one
      `/leagues` request a day. Teams of these competitions are stored with `is_national`.
      Expected cost in a window week: about 130 matches at about 12 requests each.
- [ ] National teams: check the first window's capture (odds, lineups, requests), then the
      history load, then the host flag (venue country).
      - 2026-11-15, first window so far (13-15 Nov, 75 finished matches): odds and results
        on every competition (close odds 18/18 Nations League, 15/15 AFCON qualifiers, 10/11
        CONCACAF Nations League, 26/31 friendlies); lineups captured 18/18, 14/15, 9/11 and
        8/31 for friendlies, in line with the audit (35-38% of friendlies in 2025-26).
        Requests 1,183 and 1,220 on 13-14 Nov, against about 480-620 before. Weather errors
        were transient Open-Meteo TLS timeouts and one 429, retried on the next tick. Final
        check after the window closes (17-19 Nov).
      - Friendlies (league 10) include youth internationals: 546 of 942 stored friendlies
        have a U17-U21 side, and 308 of 533 `is_national` teams are youth sides, all stored
        with `team_level` senior; a few clubs that played national sides (LAFC, DC United,
        FC Urartu) are tagged `is_national` too. The owner keeps youth sides in the data
        (2026-11-15). Before any national-team model (Stage D): set `team_level` from the
        name (U15-U23) and keep youth sides and clubs out of the senior fit.
      - Team level and clubs, acceptance stated first (owner: yes, 2026-11-16). Scope: teams
        of national competitions (`is_national`); club sides in domestic leagues keep
        `senior` (a reserve side plays a senior league). (a) `team_level` from the name:
        a final `U15`-`U23` (or `U-20`) gives `u15`-`u23`, a `W` before it or at the end
        marks a women's side (`women`, `women_u17`), a final `B` gives `b`, anything else
        `senior`; set on new national sides at load and on stored ones by a command.
        (b) Clubs: a national side whose name (suffix removed) matches no provider country
        is checked with the provider's own flag, one `/teams?id=` request each through the
        client and quota planner (about 85, resumable, raw payloads kept); `national:
        false` clears `is_national` and sets the provider's country; `national: true` or
        no answer leaves the row as it is (never guessed). (c) A senior national fit
        selects `is_national` and `team_level == "senior"`; tests show a U21 side, a
        women's U17 side, a B side and a club drop out and a senior side stays. (d) The
        command prints counts by level and the clubs cleared, and writes only with
        `--write`.
        **Built and applied (2026-11-16):** `normalize.team_level_of` (also set at load on
        national sides), `adatta/features/team_levels.py`, `adatta teams-level [--fetch]
        [--write]`, `ApiFootballClient.team` / `parse_team`, `tests/test_team_levels.py`
        (16 tests). `host.Countries` now strips every suffix ("Canada W U17" -> Canada; it
        stripped only the last); host roles are not rewritten until `venues-host --write`
        runs again. Rule (b) amended after its first run, disclosed: the provider flags
        some nations `national: false` (Montserrat, Sao Tome and Principe, Papua New Guinea
        U20, Puerto Rico U19), so a side stays national when the provider's country is its
        own name, or when it is listed under `nations` in `config/national_countries.yml`
        (Sao Tome and Principe: provider country "São-Tomé-e-Príncipe"). The four were
        cleared by the first run and restored. Result: 83 `/teams` requests; 27 clubs
        cleared (MLS, USL, K League, Süper Lig and other clubs, PSG U17, Rangers U17,
        Philadelphia Union II, ABFF U19, and the Basque and Catalan selections, which are
        not FIFA nations); 634 national sides remain: senior 218, u16-u23 410, B 4 (Algeria,
        Egypt, Ghana, Morocco), women's U17 2. The 29 sides without a country match that
        the provider calls national stay (Afghanistan, Bahamas, North Korea, ...).
      - History load done 2026-11-15: `adatta backfill-run --config
        config/national_competitions.yml`, 78 jobs (none failed), 572 requests (below the
        1,000-1,500 estimate: detail goes 20 matches a call); 10,308 finished national
        matches, every one with detail.
      - Host flag, acceptance stated first (2026-11-15). (a) `venue.provider_country` (new,
        nullable; migration 0010): the venue's country as API-Football's `/venues` gives it,
        one request per country (`/venues?country=`), for the countries of the national teams
        in stored matches, through the quota planner and resumable (a country already
        fetched is skipped); a venue is matched by provider id, else by normalised name and
        city within that country; the existing `venue.country` (the competition's country,
        "England" for Cardiff City Stadium) is left as it is. (b) Host role per national
        match from `adatta/features/host.py`: a team's country is its name with any youth
        suffix (U15-U23, B, W) removed, matched to the provider's `/countries` names
        (`config/national_countries.yml` for the 16 names that differ); `home` when the
        venue's provider country is the home side's, `swapped` when it is the away side's
        only (each needs only that side's country), `neutral` when both sides' countries are
        known and the venue is in neither, `unknown` otherwise (no venue, no venue country,
        or a side without a country: a club, or a nation the provider does not list), never
        guessed (principle 8). (c) `adatta venues-host
        --write` sets `match.neutral` on national-competition matches from the role (neutral
        only), and `venues-neutral --write` no longer touches them. (d) Reported: share of
        finished national matches with a known role, by competition; spot checks: World Cup
        2018 matches with Russia home in Russia and the rest neutral; Nations League group
        matches home; Euro 2020 matches neutral except the hosts' at home. Tests with fakes
        cover (a) to (c).
        **Built 2026-11-15; (a)-(c) pass, (d) partly.** `adatta venues-host [--fetch]
        [--write]`, `tests/test_host.py` (5 tests), migration 0010 applied. Fetch: 169
        countries, 170 requests with `/countries`; provider names and cities are also
        matched before and inside a bracket ("Jassim Bin Hamad Stadium (Al-Sadd Stadium)",
        "ad-Dōha (Doha)"). Of 9,881 finished national matches, **4,912 (50%) have a role**
        (home, swapped or neutral); unknown: 4,001 with a venue the provider's country
        listings don't place (English names against local ones: "Luzhniki Stadium" against
        "Stadion Luzhniki"; DRV PNK Stadium is in no listing), 967 with no venue, 428 with a
        side without a country. Coverage is highest in the European competitions (Euro
        qualifiers 98%, European WC qualifiers 83%, Nations League 77%) and lowest at
        tournaments (World Cup 32%, Copa America 27%, AFCON 15%). Every role derived in the
        spot checks is right (World Cup 2018: Russia home in Russia, Spain v Russia swapped;
        Euro 2024: Germany home, Switzerland v Germany swapped), but most World Cup 2018 and
        Euro 2024 matches are unknown, so (d) is not met yet. `--write` set match.neutral on
        1,144 national matches. Next: a geocoding fallback (Open-Meteo, no API-Football
        quota) for venues the listings don't place.
      - Geocoding fallback, acceptance stated first (2026-11-15). (a) `venue.geo_country`
        (new, nullable; migration 0011): the country of the venue's city from an Open-Meteo
        place search (worldwide, raw payload stored), for venues of national matches that
        `/venues` does not place; kept apart from `provider_country` (its source), never
        written over it, and no other venue field changes. (b) The country is set only
        when the populated places of the city's name (exact name if any, else all) lie in one
        country, or the most populous is at least 10 times the largest elsewhere; otherwise
        none. Geocoder country names go through the same name matching and aliases as team
        names; a place in the United Kingdom takes its `admin1` (England, Scotland, Wales,
        Northern Ireland). (c) The host role reads `provider_country`, else `geo_country`.
        (d) Reported as before: share with a role by competition, and the same spot checks
        (World Cup 2018, Euro 2024) now mostly placed and right. Tests with fakes cover
        (a)-(c), including Cardiff (Wales), Arlington (two US places: USA) and a name in two
        countries (none).
        **Built 2026-11-15; (a)-(d) pass.** `adatta venues-host --geocode`, migration 0011
        applied, `tests/test_host.py` (7 tests). 948 Open-Meteo searches: 929 venues placed,
        502 not (a name in two countries of similar size, such as Santa Clara in the US and
        Cuba; transliterations the geocoder doesn't know, such as Moskva; a country with no
        provider name). **7,287 of 10,308 finished national matches (71%) now have a role**,
        up from 4,912: World Cup 267 of 296, Euro 403 of 415, Copa America 112 of 118, Euro
        qualifiers 239 of 239, Nations League 745 of 792, AFCON 260 of 385; friendlies 2,155
        of 3,832. Unknown: 1,472 venues not placed, 967 matches with no venue, 582 with a side
        that has no country where it matters. Spot checks right: World Cup 2018 Russia home
        or swapped (Uruguay v Russia in Samara, Spain v Russia in Moscow) and 55 neutral;
        World Cup 2026 Mexico, USA and Canada home (Czech Republic v Mexico swapped) and 82
        neutral; Euro 2024 Germany home (Spain v Germany swapped), 46 neutral, none unknown;
        a random 15 of the geocoded matches all right. `--write` changed match.neutral on 889
        more national matches.
- [x] UEFA detail backfill 2016-2025, 2026-11-15: the 19 calendar-only jobs (2019-2025) set
      back to the detail stage, then `adatta backfill-run`: 25 jobs, detail for 7,717
      matches, 401 requests; every finished Champions, Europa and Conference League match
      from 2016 (2021 for the Conference League) now has detail.
- [x] 2026-11-13: neutral venue flag for club matches, `adatta venues-neutral`
      (`adatta/features/neutral.py`, `config/neutral_venues.yml`): venue identity against
      each team's league grounds at kickoff, then round rules, then per-fixture overrides.
      Bundle league matches 99.1% home (the rest: 342 with no venue, 42 temporary grounds);
      all 250 rule-covered finals and semis decided. The 25 review cases were settled by hand
      the same day (23 overrides in `config/neutral_venues.yml`, a rule for the Championship
      play-off final). `--write` set `match.neutral` on 362 matches.
- [x] Third tiers. Acceptance stated first (2026-11-15): (a) league ids verified against
      `/leagues` (done: League One 41, 3. Liga 80, Ligue 3 63 (the Championnat National),
      Serie C 138 / 942 / 943 by group plus play-offs 975 / 976, Primera División RFEF 435-438
      and 692 by group plus play-offs 1006; Spain's tier 3 is absent before 2019, so 2016-2018
      stay a gap, never filled); (b) `adatta audit --config config/tier3_competitions.yml`
      fills the capability table, with feasibility rows in `docs/SOURCE_FEASIBILITY.md`;
      (c) `adatta backfill-run --config config/tier3_competitions.yml` loads every listed
      season with detail for finished matches, none failed, and the completeness report
      shows it; (d) the file is not read by the capture tick: live capture of tier 3 is a
      separate decision for the owner (it would add odds, lineup and weather requests for
      about 2,300 matches a season).
      Result (2026-11-15): 87 seasons queued and done, none failed, 1,585 requests; all
      29,502 finished matches have detail (`backfill-report`). What the report flags, none
      of it a load gap: (1) "no regular-season round labels" on 55 seasons is the report's
      rule, not the data: Serie C from 2018 labels rounds "Girone A - n", Primera RFEF
      "Group 1 - n" (2020 adds a "2nd Phase"), and play-offs have no regular season;
      (2) Serie C 138 in 2016 and 2017 holds all three groups in one league (1,140 and
      1,026 matches, "Regular Season - n"), so the group must come from the teams, not the
      id; 19 seasons have cancelled or postponed matches (2019-20 Covid stops, 36 cancelled
      in Serie C 2017; status comes as both "CANC" and "Canc"); (3) Ligue 3 2024 lists one fixture
      twice (matches 80630 and 80631, same sides and kickoff), the only duplicate in tier
      3, to drop before any tier-3 fit; (4) 4 finished matches without a normal-time score
      (League One 2017, Serie C 2018-2020, one each). Before a tier-3 model: a group key
      for Serie C and Primera RFEF, and the round rule widened.
- [x] Tier-3 cleanup. Acceptance stated first (2026-11-15): (a) one regular-round rule in
      the adapter layer (`Regular Season - n`, `Girone X - n`, `Group N - n`, each with an
      optional `2nd Phase`; UEFA's lettered `Group A - n` is not a league round), used by the
      completeness report, `harness.load_matches` and the live trigger rule; (b) exact
      duplicates (same sides and kickoff in one season) are flagged by the report and
      `load_matches` keeps the lowest match id; (c) a group key per team from the
      first-phase match graph (teams that play each other), so the round-robin count is
      the sum over groups: Serie C 2016 and 2017 under one id read as three groups;
      (d) play-off ids are marked `knockout` and are not counted as leagues; (e) for every
      league in `config/competitions.yml`, `load_matches` returns the same match ids before
      and after (checked on Neon), so no fitted bundle or cache changes; (f) the tier-3
      report then flags only real gaps.
      Result (2026-11-15): `normalize.is_regular_round` / `is_second_phase`,
      `backfill.round_robin_groups` and `duplicate_fixtures`, `knockout: true` on ids 975,
      976 and 1006; tests in `tests/test_tier3_rounds.py`. (e) holds: 39,755 matches, same
      content hash before and after. The report went from 59 flagged seasons to 9, each a
      real irregularity: the Ligue 3 2024 duplicate; 4 missing normal-time scores; Primera
      RFEF Group 1 2022, where one place is shared by Internacional de Madrid (5 matches)
      and Talavera (33); Serie C Girone B 2025, where Rimini stops after 15 matches; and
      Serie C 2016 and 2017, where the provider files Reggina's Girone C fixtures under
      Reggiana's id (880, named "Reggiana" in the raw payload), so the two clubs are one
      team and Girone B and C read as one group (Reggina has its own id, 1692, from 2018).
      Before a tier-3 fit, Serie C 2016-2017 needs that split (by opponent group) or stays
      out. No team in the tier 1-2 leagues plays more matches than its group allows.
- [ ] Tier-3 live capture (owner: yes, 2026-11-15). Quota before: live capture used about
      220-900 API-Football requests a day over 5-14 Nov (the 3,000+ days were backfills),
      against 7,500. Tier 3 adds about 80 league matches a week to roughly 90, so odds and
      lineups about double: an expected 500-1,800 a day, under 25%, far from the 70%
      upgrade trigger. Acceptance stated first: (a) the capture tick loads
      `config/tier3_competitions.yml` next to `competitions.yml` and
      `national_competitions.yml` (one list of capture files, in code); (b) tier 3 gets
      calendar, availability, odds, lineups, detail and weather like any captured league,
      and no forecasts (projection covers the bundle leagues only); (c) after a week the
      daily request count is checked against the estimate, and stays under 70%.

## Stage A: domestic cup ties, 90-minute forecasts

Scope: FA Cup, Copa del Rey, Coppa Italia, DFB-Pokal and Coupe de France ties where both
teams are in a fitted bundle (tiers 1-2, then tier 3 once loaded). Other ties are not
forecast (principle 8), and the dashboard says so.

Model: the bundle's 1b posterior as is, plus cup-specific terms tested one at a time like
Phase 2 groups: cup home advantage (smaller than league), neutral venue (no home term),
and a rotation proxy (round of the cup, the gap to the next league match, the gap in
tier). Cup matches as training data: tested separately (first as targets only, then
added to the likelihood with their own home term), because rotation makes them noisier
evidence of strength.

Acceptance (to be stated in full before building): on dev cup ties between bundle teams,
1X2 and 90' scores not worse than the 1b model applied unchanged, calibration (ECE) no
worse; each cup term judged like a Phase 2 group.

## Stage B: knockout resolution (§9)

Needed whenever a winner is required: domestic cups, continental knockouts and
tournaments.
- `CompetitionRound` format rules: legs, extra time, penalties, tiebreak method, away-goals
  rule with validity dates (UEFA dropped it from 2021/22).
- Extra-time sub-model: its own small pooled rate for teams that were level (§9.2), not
  90' rates scaled by 30/90.
- Shootout model: kick by kick, taker and goalkeeper rates pooled player -> team ->
  league; until Phase 3, a team-level model with a small home-side edge as a tested
  covariate (§9.3).
- Two-legged ties: before leg 1 an aggregate estimate; after leg 1 the actual score is a
  fixed input to leg 2 (§9.4). P(advance) as in §9.4.
- Outputs: P(advance) per tie, round-reach probabilities by bracket simulation with one
  posterior draw per simulated bracket (§9.5).

Data to verify first: ET and shootout scores (`split_score` already separates them),
shootout kick events, and how often the provider has them per competition.

Acceptance: P(advance) calibration and log loss on dev knockout ties against a baseline
of "90' model, level = coin flip", validated apart from the round-robin core (§19.3).

## Stage C: continental club competitions (§10.1)

The problem: teams from different leagues meet, so league strengths need a common scale.
- **C1, the five bundles first:** Q_league (league quality) estimated from bridging
  matches between teams of the five bundles (UEFA competitions, 2016 on), as a global
  layer above the league fits and refitted when bridging matches accumulate (§10.1).
  Forecast only continental matches between two bundle teams.
- **C2, more leagues:** each further league (Portugal, Netherlands, Belgium, Turkey,
  Scotland, ...) as its own bundle (Phase 6 scale-out), with a data audit and quota plan
  each. A league-reputation scale (e.g. UEFA coefficients) only as a cold-start prior for
  Q_league, never a feature in log lambda, with licensing reviewed (§18.2).
- Travel: the G2 retest on cross-nation matches (noted in `PHASE2_TASKS.md`) belongs here.

Acceptance: on dev continental matches between bundle teams, beats the "each league's
own scale, no Q_league" model and an Elo-style baseline on 1X2 log loss and RPS.

## Stage D: national teams (§10.4)

Its own fit, not inside the club model:
- **v0 (team only, from results):** one global strength scale with confederation effects,
  a calendar-time random walk (variance grows with elapsed time, 8-12-year window),
  competition-type effects (friendly / qualifier / finals) with friendlies kept but
  weighted, host flag and travel instead of a neutral binary, heavier-tailed goals
  (negative binomial) with a wider score matrix. Elo/FIFA ratings only as fading
  cold-start priors.
- **v1 (after Phase 3):** squad offset from the called-up players' club quality, through
  a `CallUp` entity snapshotted like player status.
- **v2 (after Stage B):** tournament simulator (groups, best third places, brackets).

Acceptance: beats the Elo baseline on the locked international holdout (to be defined at
the start of the stage), calibrated on qualifiers and nations-league matches; friendlies
and competitive matches reported apart.

## Cross-cutting

- **Serving and refits:** results-triggered refits (D33) already work from the fixture
  list, so cup and continental midweek results are used as soon as these matches are in
  the model.
- **Minors:** youth internationals stay out of scope without review (CLAUDE.md rule 10).
- **Quota:** each stage's data load goes through the quota planner; the upgrade trigger
  (about 70% sustained use) is checked before Stage 0 and again before C2.

## Suggested order

1. Stage 0 data loads, alongside Phase 2.
2. Stage A, then Stage B (they share the cup data).
3. Stage D v0 in parallel with A/B if time allows (separate data and fit).
4. Stage C1, then C2 league by league.
5. Phase 3 (squad and player), then lineup-aware cups, D v1 and D v2.

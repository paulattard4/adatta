# Feasibility register: Phase 2 and Phase 3c covariates

Spec: `docs/adatta_architecture_v2.pdf` §19.3 (gate after 1b: "every Phase 2/3c covariate has a
verified entry"), §4.4 and Appendix E (register columns), §6.1–6.2 (Phase 2 context layer),
§7.3 (Phase 3c formation, slot codes, Manager entity), §20.2 (the former Phase 3c advanced
signals), Appendix C (traceability). Audited 2026-11-09.

This register is the per-covariate gate check. The per-field source register,
`docs/SOURCE_FEASIBILITY.md` (2026-11-05), stays the reference for fields used by other phases;
the evidence below extends it to the 2019–2025 modelling window and closes its open items 3
and 4 for Phase 2 (venue coordinates, Open-Meteo, coaches).

## Scope

- **Competitions:** the five bundles, top flight and second tier: Premier League, Championship,
  La Liga, Segunda División, Serie A, Serie B, Bundesliga, 2. Bundesliga, Ligue 1, Ligue 2.
  Domestic cups are noted only where they affect a covariate (cups stay out of modelling, D6).
- **Seasons:** 2019/20 to 2025/26 (start years 2019–2025): 70 league-seasons, **26,969
  finished matches**. Ligue 1 and Ligue 2 2019/20 stop at 279 and 280 matches (season ended
  early in 2020).
- **What "3c" means here.** The spec uses the label two ways. §19.3 Phase 3 step (c) and
  Appendix C ("Formation, slot codes: 3c") are formation, slot-code roles and the Manager
  entity. The note under the §19.3 table says the advanced signals "formerly in Phase 3c"
  (WOWY, cohesion, workload risk, style-matchup, contract, kickoff time) are now the §20.2
  experiment backlog. Both groups are registered below, in separate sections. **Raised for the
  owner: confirm which group the gate means** (no work depends on the answer, since both are
  covered).

## Evidence base

| Evidence | What it is | Cost |
|---|---|---|
| Capability table | `competition_source_capability` and `reports/capability_audit/*.csv` (2026-11-05; 40 sampled fixtures per competition-season, seed 20261105) | none (existing) |
| Neon, normalised tables | `team_match_stat`, `match_coach`, `match`, `match_result` for all 26,969 matches | read-only queries |
| R2, fixture lists | the stored `/fixtures?league&season` payload for each of the 70 league-seasons: venue, referee, kickoff time | 70 R2 reads |
| R2, match detail | every stored `/fixtures?ids` detail payload for the 26,969 matches (1,374 objects): lineups, formation, grid, coach, events | 1,374 R2 reads |
| Capture snapshots | `fixture_snapshot` (2026-11-05 to 2026-11-09, 4,384 matches), `lineup_snapshot` (231 team-lineups): pre-match timing of referee, kickoff and lineups | read-only queries |
| API-Football probes | 8 requests through `QuotaPlanner` and `ApiFootballClient.fetch` (raw payloads stored in R2): `/standings?league=39&season=2024`, `/standings?league=136&season=2024`, `/teams?league=39&season=2024`, `/coachs?team=33`, `/venues?city=Manchester`, `/fixtures?league=2&season=2019`, `/fixtures?league=3&season=2023`, `/fixtures?league=848&season=2023` | 8 of 7,500 (account at 208 used for the day afterwards, poller included) |
| Open-Meteo probes | geocoding of all 244 distinct venue city strings; elevation for 5 stadium coordinates; archive, historical-forecast, previous-runs and forecast APIs for Old Trafford on sample dates | about 260 requests, free, no key |

Nothing touches minors' data: every covariate here is senior team, venue, referee or weather data.

## Keys

**Status:** `verified` = measured from real payloads with usable coverage in every bundle
league-season · `partial` = exists, but with gaps in coverage, history, precision or as-of
timing (the entry says which) · `absent` = verified missing from every source we use.

**Temporal availability** (principle 2) uses these terms:
- *Post-match fact:* known once a match has finished. A covariate built from it (a rolling
  average, say) may use only matches whose `kickoff + result lag` is before the as-of time,
  as `team_match_stat` already enforces for xG in 1b.
- *Static:* a property of a venue or place (coordinates, elevation). Reconstructable at any
  date, provided a change (a new stadium) is versioned with `valid_from`.
- *Pre-match, published:* announced before kickoff (referee, kickoff time, lineups). History
  holds only the final value, not when it was published, so the publication lead time can be
  measured only from live capture.

---

## 1. Phase 2: style (Layer 3a, §6.1)

| # | Covariate | Source, endpoint, field | Coverage, bundles 2019–2025 | Verified by | Temporal availability | Status | Degrades to |
|---|---|---|---|---|---|---|---|
| 2.1 | Shot volume: total, on target, inside box | API-Football `/fixtures?ids` `statistics`: `Total Shots`, `Shots on Goal`, `Shots insidebox` (`team_match_stat`) | 26,955 / 26,969 matches (99.95%); every league-season ≥ 99.3%. Cups 8–100% (not modelled) | Neon `team_match_stat`; capability table `has_shot_stats` | Post-match fact | **verified** | Style terms dropped from the design matrix for a match whose window has no stats (§4.4), never zero-filled |
| 2.2 | Possession | `statistics` `Ball Possession` | 99.95% of matches, the same rows as 2.1 (Ligue 2 2024: 612 of 616 team rows) | Neon `team_match_stat.possession` | Post-match fact | **verified** | As 2.1 |
| 2.3 | Passing volume and accuracy | `statistics` `Total passes`, `Passes accurate`, `Passes %` | 99.95% of matches (the same rows) | Neon `team_match_stat.stats` | Post-match fact | **verified** | As 2.1 |
| 2.4 | Other exposed style stats: fouls, corners, offsides, blocked shots, saves | `statistics` (18 types) | 99.95% of matches (counts sent as null are stored as 0 where the block exists, verified in 1b) | Neon `team_match_stat` | Post-match fact | **verified** | As 2.1 |
| 2.5 | Style effect on variance (§6.1 second bullet) | Same inputs as 2.1–2.4 | As 2.1 | as above | Post-match fact | **verified** | Mean-only style terms |
| 2.6 | PPDA | none: no defensive-action counts by pitch zone, no event coordinates | 0% (no field in any of 26,969 detail payloads or 18 statistic types) | R2 detail scan; capability table `has_event_coordinates` absent everywhere | n/a | **absent** | Not in the model (§6.1, App. C "not planned") |
| 2.7 | Directness | none: pass counts exist, but not pass length or direction | 0% | as 2.6 | n/a | **absent** | Not in the model |

## 2. Phase 2: venue, weather, travel (Layer 3b, §6.2)

| # | Covariate | Source, endpoint, field | Coverage, bundles 2019–2025 | Verified by | Temporal availability | Status | Degrades to |
|---|---|---|---|---|---|---|---|
| 2.8 | Venue identity | API-Football `/fixtures` `fixture.venue{id,name,city}` | Venue name and city on every match. Venue id: 2019–2023 ≥ 95% in every league-season (lowest Ligue 2 2020, 365 of 384); 2024 85–100% (Serie A 323 of 380, Segunda 429 of 468, Ligue 1 291 of 308); **2025/26 54–100%** (Serie A 206 of 380, Championship 302 of 557, Segunda 284 of 468; PL 380 of 380). Stade Louis II appears under two ids (659 and 20470). 324 distinct ids, 244 city strings | R2 fixture lists (70 seasons) | Pre-match, published with the fixture; changes captured in `fixture_snapshot.venue_ref` since 2026-11-05 | **partial** (2024–2025 ids) | Match venue by name and city into a canonical venue table; until then, venue = home team's modal venue (2.14) |
| 2.9 | Venue coordinates (lat, lon) | Not in API-Football (`/venues` and `/teams` give address, city, capacity, surface; no lat/lon: probe `/venues?city=Manchester`, 8 venues). Open-Meteo geocoding by city | Geocoding of all 244 city strings: 240 resolve to a place in a bundle country (GB, ES, IT, DE, FR, MC, AD), 4 do not (`Caerdydd`, `Iruñea`, `La Coruña`, `Napoli`, all local-language names), 13 have a non-bundle top hit (Roma → Romania, Como → Comoros), and at least one in-country hit is wrong (`Roma` → a 692 m village, not Rome). City-level precision only; a stadium name search sometimes hits (Old Trafford, 0.6 km off) | Open-Meteo geocoding API, 244 requests | Static | **partial** | Build a reviewed venue table once (country-filtered lookup, manual review queue as in §4.6, about 330 rows); until then 2.10, 2.12–2.15 cannot run |
| 2.10 | Altitude and altitude differential | Open-Meteo elevation API (90 m DEM) from 2.9's coordinates; the differential uses the away team's home venue (2.14) | Elevation returned for 5 of 5 sampled stadiums (Old Trafford 18 m, Bernabéu area 716 m, Munich 491 m). City elevations span sea level to Soria 1,064 m and Encamp (Andorra) 1,257 m; Madrid, Getafe, Leganés, Alcorcón, Fuenlabrada, Valladolid, Granada, Albacete and Burgos are all 620–860 m, so Spanish matches have real differentials | Open-Meteo elevation and geocoding APIs | Static (look up once per venue, cache) | **partial** (needs 2.9) | Term absent until the venue table exists |
| 2.11 | Surface; pitch dimensions | API-Football `/venues`, `/teams` `venue.surface`. No pitch-dimension field anywhere | Surface: current value only, no history (PL 2024: 20 of 20 `grass`). Dimensions: 0% | Probes `/teams?league=39&season=2024`, `/venues?city=Manchester` | Static, but only today's value can be read; a past change of surface is not reconstructable | Surface **partial**; dimensions **absent** | Both terms absent; for these bundles the surface is near-constant, so little is lost |
| 2.12 | Weather at kickoff as forecast before the match: temperature, wind, precipitation | Open-Meteo: forecast API (16 days ahead, live); **previous-runs API** (forecasts as issued 1–7 days earlier); historical-forecast API (stitched short-lead runs) | Live forecast: verified (384 hourly values, 16 days). As-issued history: **from about Feb 2024 only** (previous-runs empty on 2023-12-15, present on 2024-02-15). Historical-forecast: differs from reanalysis on 2023-03-15, but on 2019-10-20 it returns exactly the reanalysis values, so before about 2022 it is not a forecast. **Every value needs 2.9's coordinates** | Open-Meteo probes | Forecast: pre-match, must be captured live or read from previous-runs (2024+) with the lead time stated. **Realised weather (archive or reanalysis) is not observable before kickoff** and must not be a training feature for a pre-match prediction | **partial** | Climatology (2.13) for training before 2024 and for matches more than 16 days out (§6.2); forecast terms only where an as-issued forecast exists |
| 2.13 | Weather climatology for venue and date | Open-Meteo archive API (ERA5 reanalysis, back to 1940) | Hourly values returned for 2019-10-20 at Old Trafford; full window covered | Open-Meteo probe | Reconstructable as of any date, if the climatology for a season uses only years before it | **partial** (needs 2.9; the data itself is verified) | Term absent until the venue table exists |
| 2.14 | Travel burden: great-circle distance and timezone shift from home venue | Derived: team home venue = modal venue of its home fixtures in a season (2.8) + coordinates (2.9) | Home venue: 1,349 team-seasons; 31 play under 90% of home games at their modal venue and 8 under 60% (ground-shares, rebuilds), so `home_venue` needs `valid_from/to`. Timezone shift: only the Canary Islands (Las Palmas, Tenerife: UTC+0 against UTC+1 for the rest of Spain) | R2 fixture lists; geocoding | Static plus the fixture list | **partial** (needs 2.9) | Term absent until the venue table exists |
| 2.15 | Consecutive away trips flag | Derived from the fixture calendar | As 2.16: needs every competition a team plays in | R2 fixture lists | Fixture dates are pre-match, published | **partial** (needs 2.16) | Flag off |

## 3. Phase 2: referee, congestion, importance, regime facts (Layer 3c–e, §6.2)

| # | Covariate | Source, endpoint, field | Coverage, bundles 2019–2025 | Verified by | Temporal availability | Status | Degrades to |
|---|---|---|---|---|---|---|---|
| 2.16 | Congestion, backward: days since the last match | Fixture calendar across **all** a team's competitions, API-Football `/fixtures?league&season` | League and domestic-cup fixtures: in the DB for every season. **European competitions are not loaded** (UCL 2, UEL 3, UECL 848 are not in `config/competitions.yml`), nor are the EFL Cup and the super cups, so midweek European and League Cup games are invisible and days-since-last-match is wrong for exactly the congested teams. The API has them: UCL 2019 210/210, UEL 2023 175/175, UECL 2023 417/417 finished fixtures, each with referee and venue | Neon `match`; 3 probes | Past match dates are facts; reconstructable | **partial** | Backfill the missing competitions' fixture lists first (about 3–5 competitions × 7 seasons, 20–35 requests); without them the term must not run for any team-season with European or League Cup fixtures |
| 2.17 | Congestion, forward: days to the next significant match | Fixture calendar as known at the as-of time | History holds only final kickoff times. Live capture shows schedules move: 130 of 4,384 captured matches had their kickoff changed in 4 days (2026-11-05 to 11-09). Also needs 2.16's competitions and 2.20's importance | `fixture_snapshot` | Pre-match, published, and revised: as-of reconstructable only from `fixture_snapshot` (from 2026-11-05) | **partial** | Dev and holdout use final dates (a small, stated look-ahead for postponements) or the term is tested on the live period only; decide before building |
| 2.18 | Referee identity and strictness history | API-Football `/fixtures` `fixture.referee`; strictness from cards and fouls (`team_match_stat`) | Name on 100% of finished league matches in all 70 league-seasons (one exception: Ligue 2 2025, 307 of 309). **The name format changed during 2025/26** ("D. Massa" became "Davide Massa, Italy"), so distinct names per season roughly double (Serie A 41 → 76, 2. Bundesliga 42 → 80); entity resolution is needed. Cards and fouls 99.95% | R2 fixture lists; Neon | Name: post-match fact for history | **partial** (resolution) | Resolve names once (normalise, initial + surname within competition, review queue); until then strictness from the pool prior (2.19) only |
| 2.19 | Referee assignment before kickoff (actual referee vs pool prior, §6.2) | `/fixtures` `fixture.referee` polled daily by capture (`fixture_snapshot`) | Captured 2026-11-05 to 11-09, matches played since capture began: referee present before kickoff in 10/10 PL, 9/9 Bundesliga, 9/9 2. Bundesliga, 10/10 Serie A, 10/10 Serie B, 9/9 Ligue 1, 9/9 Ligue 2 (all at least 24 h before), 12/12 Championship and 10/10 La Liga (all under 24 h before), **Segunda 2/9**, FA Cup 0/39 | `fixture_snapshot` | Pre-match, published; **when it was published is not reconstructable for 2019–2025** | **partial** | Pool prior by competition and tier until the referee appears (§6.2). For backtests, assume the referee is known at the as-of time only where capture shows ≥ 24 h lead (the eight leagues above); Championship, La Liga and Segunda use the pool prior at T-24h. Re-measure after a month of capture |
| 2.20 | Importance (dead rubber, must-win, final) | Derived: as-of league table computed from our results, plus each league-season's zone rules (promotion, play-off, relegation places). `/standings` gives zone labels | Results 100%. `/standings` returns only the final table (`update` = season end) and its labels are post-season outcomes (PL 2024 labels 17th place "Champions League"), so they are not usable as of a date. Zone rules and point deductions are not in the results | Probes `/standings` PL and Serie B 2024; Neon results | Results: post-match facts. Zone rules: static per season. Deductions: dated facts | **partial** | Manual table of zone rules and point deductions per league-season (70 rows) before the term is built; without it, importance off |
| 2.21 | VAR in use (`has_var`) | Events `type=Var` in `/fixtures?ids` | **No Var event in any 2019/20 match**, although VAR was in use that season in all five top flights. From 2020: top flights 16–43% of matches have a Var event; Serie B from 2021, Segunda and 2. Bundesliga from 2020; Championship 0–0.2% and Ligue 2 0–1.3% every season | R2 detail scan, 26,969 matches | Competition-season fact; static | **partial** | Manual competition-season table (VAR start dates are public), not inferred from events |
| 2.22 | Added time per half | Events `time.extra` (a goal, card or sub during added time) | A lower bound only: a second-half `extra` value appears in 15–74% of matches (mean of the per-match maximum 2.3–5.0 min); first half 3–31%. Announced added time is not provided. `fixture.periods` gives half start times only | R2 detail scan | Post-match fact | **partial** | Competition-season average from the lower bound, used only weakly (§6.2); stated as a known bias |
| 2.23 | Kickoff time of day | `/fixtures` `fixture.date` (UTC), local time from the venue's country | 100% of matches; spread across 10:00–21:00 UTC | R2 fixture lists | Pre-match, published; can change (2.17). History holds the final time only | **verified** | n/a (also §20.2 backlog) |
| 2.24 | Attendance (dynamic home advantage, §5.2) | none: `fixture.attendance` absent | 0 of 26,969 matches | R2 detail scan; capability table | n/a | **absent** | Static home advantage (§4.4); stays backlog (§20.2) |

## 4. Phase 3c: formation, slot codes, Manager entity (§7.3, §19.3 step c)

| # | Covariate | Source, endpoint, field | Coverage, bundles 2019–2025 | Verified by | Temporal availability | Status | Degrades to |
|---|---|---|---|---|---|---|---|
| 3c.1 | Formation history (team's recency-weighted tendency) | `/fixtures?ids` `lineups[].formation` | Both teams: 26,875 / 26,969 matches (99.65%); lowest Segunda 2019 437/468 (93.4%), Championship 2025 539/557 (96.8%) | R2 detail scan | Post-match fact for history. The current match's formation is known only at lineup release: in the 231 team-lineups captured so far it was first seen a median 22.7 min before kickoff (closest 7.7 min, an FA Cup match; none after kickoff; capture polls every 15 min from T-75 min) | **verified** | Matches without a lineup drop out of the formation fit |
| 3c.2 | Grid position → slot codes | `lineups[].startXI[].player.grid` | All 22 starters with a grid: same 99.65% as 3c.1 | R2 detail scan | As 3c.1 | **verified** | Formation-only roles where grid is missing |
| 3c.3 | Player position code per lineup (G/D/M/F, for role history) | `startXI[].player.pos` | All starters coded: 25,984 / 26,969 (96.35%); weakest Serie B 2025 (75%), Ligue 2 2025 (76%), Championship 2019 (80%) | R2 detail scan | Post-match fact | **partial** | Role history from grid-derived slot codes (3c.2), which cover these matches |
| 3c.4 | Manager per match (Manager entity, tenure, manager-level formation pooling) | `lineups[].coach.id/name` (`match_coach`) | 53,718 / 53,938 team-matches (99.59%); 87–166 distinct coaches per league | Neon `match_coach`; R2 detail scan | Known at lineup release, used as a post-match fact for history (as in 1b) | **verified** | Team-level formation tendency only, for the 0.4% of team-matches without a coach |
| 3c.5 | Manager appointment and departure dates | `/coachs?team` `career[].start/end` | Month precision only (every date is the 1st of a month), and incomplete: `/coachs?team=33` (Manchester United) returns 2 coaches, missing others who led the team 2019–2025 | Probe `/coachs?team=33` | Dated facts, but the source's precision is too coarse for as-of use | **partial** | Tenure from the first and last match a coach appears in a lineup (3c.4), which is exact to the match |
| 3c.6 | Opponent tier | Internal: from Layer 2 strength or Q_league | n/a (no external data) | n/a | As of the strength estimate used | **verified** | n/a |

## 5. Former Phase 3c signals, now the §20.2 experiment backlog

Registered so that a later experiment starts from a known data position. None has scheduled
phase time.

| # | Covariate | Source | Coverage, bundles 2019–2025 | Verified by | Temporal availability | Status | Degrades to |
|---|---|---|---|---|---|---|---|
| B.1 | WOWY marginal player impact | Lineups (3c.1) + player minutes (`players[].statistics.games.minutes`) + substitution events | Lineups 99.65%; player match stats and timed substitutions ≥ 97.5% in each of the 66 audited bundle league-seasons 2019–2025 (40-fixture samples; lowest Ligue 2 2020) | Capability table `has_player_match_stats`, `has_substitution_timestamps`; R2 detail scan | Post-match facts | **verified** | n/a |
| B.2 | Lineup cohesion | Lineups | As 3c.1 | R2 detail scan | Post-match facts | **verified** | n/a |
| B.3 | Workload-trend injury risk | needs distance and sprint data | 0% (`has_physical_load_data` absent in every audited season) | Capability table | n/a | **absent** | Dropped (§7.2) |
| B.4 | Style-matchup interaction | Style stats (2.1–2.4) | As 2.1 | as 2.1 | Post-match facts | **verified** | n/a |
| B.5 | Contract situation | Transfermarkt contract end dates (not in API-Football) | Not assessed against data: Transfermarkt has no official API, its terms have not been reviewed, and it shows only the current contract, so past contract states are not reconstructable as of a date | none (no request made: no new source until the licensing review) | Not reconstructable; would need live snapshots | **absent** | Stays backlog; needs a licensing review and, if approved, forward snapshots |
| B.6 | Kickoff time of day | as 2.23 | 100% | R2 fixture lists | as 2.23 | **verified** | n/a |
| B.7 | Dynamic home advantage via attendance | as 2.24 | 0% | as 2.24 | n/a | **absent** | Static home advantage |
| B.8 | Altitude and forward-congestion refinements | as 2.10, 2.17 | as 2.10, 2.17 | as 2.10, 2.17 | as 2.10, 2.17 | **partial** | as 2.10, 2.17 |
| B.9 | New-signing ramp (`matches_since_signing`) | Derived from lineups and player stats: a player's first appearance for a club | As B.1 | Capability table; R2 detail scan | Post-match facts | **verified** | n/a |

---

## Summary: what fails, and where it degrades

**Gate result: every covariate has an evidenced entry; Phase 2 is not yet buildable in full.**
40 entries (37 distinct covariates; B.6–B.8 repeat Phase 2 rows): 15 verified, 18 partial,
7 absent. The absent ones were expected and are already off the plan; the partial ones are what
stands between Phase 2 and modelling.

**Absent** (no source; the term stays out of the model everywhere, never imputed):

| Covariate | Effect (principle 8) |
|---|---|
| PPDA, directness (2.6, 2.7) | Style uses only the statistics the API exposes (2.1–2.4) |
| Pitch dimensions (2.11) | No dimension term |
| Attendance (2.24, B.7) | Static home advantage in every segment |
| Workload risk (B.3) | Dropped |
| Contract situation (B.5) | Stays backlog pending a Transfermarkt licensing review |

**Partial**, with the segments that fall back to the simpler layer:

| Covariate | Segments that degrade | Falls back to | Unblocked by |
|---|---|---|---|
| Venue coordinates (2.9), so altitude (2.10), weather climatology (2.13), travel (2.14) | **All segments** until the venue table exists | No venue, weather or travel terms: Layer 3b off | A reviewed venue table (about 330 venues; geocoding resolves 240/244 city strings, with known errors) |
| Weather forecast (2.12) | Every match before about Feb 2024: dev 2019–2023 and holdout 2024/25 up to Feb 2024 | Climatology (2.13) | Nothing: as-issued forecast history does not exist earlier. Live capture of forecasts from now on |
| Venue identity (2.8) | Matches without a venue id: up to 15% per league-season in 2024, up to 46% in 2025/26 | Home team's modal venue | Name-and-city matching into the venue table |
| Congestion backward (2.16), away-trips flag (2.15) | Every team-season with European or League Cup fixtures (most top-flight top halves) | Term off for those team-seasons | Backfilling UCL, UEL, UECL, EFL Cup and super-cup fixture lists (20–35 requests) |
| Congestion forward (2.17) | Dev and holdout (no as-of fixture calendar before 2026-11-05) | Final fixture dates (stated look-ahead) or live period only | A decision, then capture |
| Referee strictness (2.18) | 2025/26 names until resolved | Competition-tier pool prior | Name resolution |
| Referee assignment (2.19) | Segunda (2/9 before kickoff), Championship and La Liga (under 24 h), cups | Pool prior until the referee appears | A longer capture sample to confirm lead times |
| Importance (2.20) | All segments | Term off | Manual zone-rules and point-deductions table (70 league-seasons) |
| VAR (2.21), added time (2.22) | All segments | Manual VAR table; added time as a lower bound | Manual table |
| Surface (2.11) | All (current value only) | Term off; near-constant in these bundles anyway | n/a |
| Player position code (3c.3) | Serie B and Ligue 2 2025, Championship 2019 (75–80%) | Grid-derived slot codes | n/a |
| Manager dates (3c.5) | All | Tenure from lineups (exact to the match) | n/a |

**Phase 3c (formation, slot codes, Manager entity) passes**: formation and grid on 99.65% of
matches, coach on 99.59% of team-matches. Its two partial fields both have exact fallbacks
from lineups.

**Phase 2 style passes** (99.95%). **Phase 2 venue, weather, travel, importance and
backward congestion cannot be modelled yet**: each needs a one-off data task first (venue
table; missing competitions' fixtures; zone-rules table), none of them a modelling task.

## Spec disagreements raised (CLAUDE.md: raise, don't silently diverge)

1. **Appendix C, "Weather, altitude: Open-Meteo, available".** Only partly true: it needs venue
   coordinates that API-Football doesn't provide, and as-issued forecast history starts about
   Feb 2024, so weather forecasts can't be backtested on the 2019–2023 dev window.
2. **Appendix C, "Congestion (back and forward): fixture calendar, available".** Partial: the
   competitions we load leave out European and League Cup games, and the forward calendar is
   as-of reconstructable only from live capture (started 2026-11-05).
3. **§4.1, Open-Meteo "No key, no cost".** The free API is for non-commercial use; a commercial
   or betting-facing service would need its paid plan. **Resolved for now (D24):** adatta is
   for the owner's personal use only, so the free tier applies. Revisit before any commercial
   or public use; per CLAUDE.md the owner decides before any paid service is added.
4. **"Phase 3c"** names two different things in §19.3 (see Scope).

## Proposed next steps (each needs the owner's go-ahead; none is done here)

1. Venue table: country-filtered geocoding of about 330 venues with a manual review queue,
   versioned home venue per team-season (`valid_from/to`). Free (Open-Meteo), no API-Football
   quota.
2. Fixture lists for UCL, UEL, UECL, EFL Cup and the super cups, 2019–2025: 20–35
   API-Football requests through the quota planner.
3. Manual tables: zone rules and point deductions per league-season; VAR start per
   competition.
4. Decision: forward congestion and referee assignment in backtests (final dates vs live-only;
   the T-24h rule in 2.19).
5. Add weather-forecast capture to the poller (Open-Meteo forecast for each upcoming fixture,
   stored as a snapshot), so forecasts exist as of each match from now on.

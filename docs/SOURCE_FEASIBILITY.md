# Source-feasibility register

Spec: `docs/adatta_architecture_v2.pdf` §4.3, §4.4, Appendix C (traceability), Appendix E (template).
One row per field. Per §4.4 this must be completed before the dependent sub-model starts, and
coverage comes **from real payloads, not declared flags**.

**Evidence base (2026-11-05)**
- Capability audit `reports/capability_audit/20261105T115828Z.csv` (+ `.manifest.json`):
  15 competitions × 51 seasons, 40 finished fixtures per season (fixed seed 20261105),
  2,040 unique fixture-detail payloads. Rows live in `competition_source_capability` (Neon).
- Extension to 2016/17–2021/22 (2026-11-05, same sample size and seed; 356 requests):
  `reports/capability_audit/20261105T163915Z` … `20261105T164501Z` (one file per season).
  **xG is absent in every competition for 2016–2021**, so it starts with the Top-5 in 2022/23
  as recorded below. All 10 leagues have lineups, shot and card data at about 100% except
  Segunda 2018 (90–92%, partial) and Ligue 2 2016 stats (68–92%); Serie B has no player match
  stats for 2016–2018 (absent), and 2. Bundesliga 2016 is partial (65%). Cups are patchy and
  stage-dependent as in later seasons; Copa del Rey 2016–2017 has no fixtures at all; FA Cup
  2017 declares stats it doesn't carry (4 declared-vs-observed mismatches).
- Field inventory mined from those payloads (raw copies in R2).
- Live probes, 4 requests: `/injuries?league=39&season=2025`, `/sidelined?player=1125`,
  `/odds?fixture=868107` (a 2022/23 Premier League match), `/venues?id=556`.

**Groups used below**: *Top-5* = PL, La Liga, Serie A, Bundesliga, Ligue 1 (2022–2025, 20 seasons).
*Tier-2* = Championship (2022–25), Segunda, Serie B, 2. Bundesliga, Ligue 2 (2023–25), 16 seasons.
*Cups* = FA Cup, Copa del Rey, Coppa Italia, DFB Pokal, Coupe de France (2023–25), 15 seasons.
Season years are start years; all audited seasons (up to 2025/26) are complete. **The current
season, 2026/27, is not in `config/competitions.yml` and has not been audited.**

**Status key**: `verified` = coverage measured from payloads · `partial` = exists but not
everywhere · `unavailable` = verified absent from the source · `unverified` = not yet tested.

**Columns not yet measured (apply to every API-Football row unless stated)**
- *Update cadence*: not measurable from historical payloads. It will be measured once Phase 0
  snapshot capture (task 5) records `observed_at` against `event_time`. Marked "not measured".
- *Licensing*: API-Football terms for the intended use (including any betting-facing output) have
  **not been reviewed**. Every API-Football row is `[verify]` on licensing until that review is done.

---

## 1. Results and match facts (Phase 1a/1b, 3d)

| Field | Status | Source, endpoint | Historical availability | Coverage (from payloads) | Update cadence | As-of reconstructable | Licensing | Fallback | Required phase |
|---|---|---|---|---|---|---|---|---|---|
| Final score, kickoff, teams | verified | API-Football `/fixtures?league&season` | All audited seasons | Every finished fixture in all 51 seasons | not measured | Yes. Results are final facts; corrections are versioned in `match_result` | [verify] | none needed | 1a |
| Normal-time / ET / shootout split | partial | `/fixtures` `score.fulltime/extratime/penalty` | All audited seasons | FA Cup 2024 proper rounds: 31/31 ET/PEN matches reconcile. **Qualifying rounds: 19/60 inconsistent** (zeroed `extratime`, non-level 90' score) | not measured | Yes | [verify] | Store NULL where it doesn't reconcile (implemented); restrict cups to proper rounds (open decision) | 1a (NT only), 3d (ET, shootouts) |
| Half-time score | verified | `/fixtures?ids` `score.halftime` | All audited seasons | Top-5 and Tier-2 100%; Cups 45–100% | not measured | Yes | [verify] | Half-split off where absent (§4.4) | 3b |
| Neutral venue flag | partial (derived, 2026-11-13) | No field in `/fixtures`; derived by `adatta venues-neutral` (`adatta/features/neutral.py`, rules in `config/neutral_venues.yml`) | All loaded seasons | Venue identity against each team's league grounds at kickoff, then round rules (finals, FA Cup semis, super cups, Club World Cup), then per-fixture overrides. Bundle league matches: 42,187 of 42,574 home, 42 neutral (temporary grounds such as Chambly at Charléty, Atalanta at Parma 2019), 1 swapped, 2 review, 342 unknown (no venue listed). All 250 rule-covered finals and semis decided (244 neutral; 3 home and 3 swapped at a finalist's own ground, e.g. the Coppa Italia finals with Lazio at the Olimpico). Other cup and UEFA ties: mostly unknown, because one side is a lower-tier or foreign club with no league grounds | – | Yes (venue published pre-match; grounds from league fixture lists) | – | Unknown stays unknown (principle 8); the 25 review cases were settled by hand on 2026-11-13 (23 overrides, a Championship play-off final rule) | 1b, Stage A–C |
| Stage / round | verified | `/fixtures` `league.round` | All audited seasons | Present on every fixture | not measured | Yes | [verify] | – | 3d, cup stage gating |
| Referee name | verified | `/fixtures` `fixture.referee` | All audited seasons | Top-5 and Tier-2 100%; Cups 10–100% (Copa del Rey 2025 48%) | Assignment timing not measured | Name: yes. **Pre-match assignment time: no** (needs live capture) | [verify] | Pool prior by competition and tier (§6.2) | 2 |
| VAR in use (`has_var`) | partial | `/fixtures?ids` events `type=Var` | All audited seasons | VAR events seen (goal/penalty/card reviews); per-competition rate not yet tabulated | – | Yes | [verify] | Competition-level manual fact | 2 |
| Added (stoppage) time per half | partial | events `time.extra` | All audited seasons | Only events *during* added time carry `extra` (a lower bound). Announced added time is not provided | – | Lower bound only | [verify] | Competition-level average from max `extra` per half | 2, 3 |

## 2. Team statistics and style (Phase 1b, 2)

| Field | Status | Source, endpoint | Historical availability | Coverage (from payloads) | Update cadence | As-of reconstructable | Licensing | Fallback | Required phase |
|---|---|---|---|---|---|---|---|---|---|
| xG (team) | partial | `/fixtures?ids` statistics `expected_goals` | Top-5: from about **mid-Jan 2023** (2022/23 is 57–68% because it starts mid-season). Championship: from 2023. Other Tier-2: 2. Bundesliga, Serie B and Segunda only from partway through 2025/26 (Serie B and Segunda from about Nov 2025); **Ligue 2 none through 2025/26** | Top-5 2023+ 98–100%; Championship 2023+ 98–100%; Ligue 2, 2. BL, Serie B, Segunda 2023–24 **0%**; 2025: 2. BL 72%, Serie B 68%, Segunda 30%, Ligue 2 0%. Confirmed on every match by `team_match_stat` (2026-11-05); Cups 0–18% | not measured | Yes (post-match fact) | [verify] | Layer 2 fits on goals alone (§4.4) | 1b |
| Shots (total, on target, in/out of box, blocked) | verified | statistics `Total Shots` etc. | All audited seasons | Top-5 100%; Tier-2 98–100%; Cups 8–100% | not measured | Yes | [verify] | Style covariates absent from design matrix | 2 |
| Possession, passes, pass accuracy | verified | statistics `Ball Possession`, `Total passes`, `Passes accurate`, `Passes %` | All audited seasons | Top-5 100%; Tier-2 about 100%; Cups about 52% of sampled fixtures | not measured | Yes | [verify] | As above | 2 |
| Corners | verified | statistics `Corner Kicks` | All audited seasons | Top-5 100%; Tier-2 98–100%; Cups 8–100% | not measured | Yes | [verify] | Corner markets not offered | 3b |
| Cards, fouls | verified | statistics `Yellow Cards`/`Red Cards`/`Fouls`, events `type=Card` | All audited seasons | Top-5 and Tier-2 100%; Cups 18–100%. Null = zero for counts (verified) | not measured | Yes | [verify] | Discipline markets not offered | 3b |
| Offsides, GK saves, `goals_prevented` | verified | statistics | All audited seasons | Present wherever team statistics are present | not measured | Yes | [verify] | – | 2 (candidate) |
| **PPDA** | unavailable | No field in any endpoint checked | – | Not among the 18 statistic types in 2,040 payloads; no defensive-action locations | – | – | – | Not planned (§6.1, App. C) | – |
| **Directness** | unavailable | No field | – | Same as PPDA. Pass counts exist but not pass direction or length | – | – | – | Not planned | – |

## 3. Lineups, players and substitutions (Phase 3)

| Field | Status | Source, endpoint | Historical availability | Coverage (from payloads) | Update cadence | As-of reconstructable | Licensing | Fallback | Required phase |
|---|---|---|---|---|---|---|---|---|---|
| Starting XI and bench | verified | `/fixtures?ids` `lineups.startXI/substitutes` | All audited seasons | Top-5 and Tier-2 100%; Cups 15–100% (FA Cup 15–22%, Coupe de France 2025 40%) | **Release time vs kickoff not measured** | Final XI: yes. **Pre-kickoff timing: no** (live capture, task 5) | [verify] | Layer 4 off; match-level projections still run | 3a–c |
| Formation and grid position | verified | `lineups.formation`, `startXI[].player.grid` | All audited seasons | Grid on every starter: Top-5 1599/1600 team-lineups, Tier-2 1277/1280, Cups 663/998 | not measured | Yes | [verify] | Formation-only roles where grid is missing | 3c |
| Coach per match (manager history) | verified | `lineups.coach.id/name` | All audited seasons | Top-5 1599/1600, Tier-2 1279/1280, Cups 984/998 team-lineups | not measured | Yes (from historical lineups) | [verify] | `/coachs` career endpoint [unverified] | 1b (manager-change widening), 3c |
| Player match stats (minutes, shots, goals, passes, tackles, duels, dribbles, fouls, cards, penalties) | verified | `/fixtures?ids` `players[].statistics` | All audited seasons | Top-5 100%; Tier-2 98–100%; Cups 8–100% | not measured | Yes | [verify] | Player props suppressed (§4.4) | 3 |
| Substitution timing | verified | events `type=subst`, `time.elapsed` | All audited seasons | Top-5 and Tier-2 100%; Cups 15–100% | not measured | Yes | [verify] | Expected minutes from role priors only | 3, 3b |
| Goal type (normal / penalty / own goal / missed penalty) | verified | events `type=Goal`, `detail` | All audited seasons | Seen across the sample: Normal 4,981, Penalty 737, Missed Penalty 167, Own Goal 146. **No set-piece or header type** | not measured | Yes | [verify] | Set-piece share not decomposable from events | 3b |
| **Event coordinates** | unavailable | No x/y on any event | – | 0% in all 51 seasons (audited) | – | – | – | Not planned | – |

## 4. Availability and status (Phase 3a–b)

| Field | Status | Source, endpoint | Historical availability | Coverage (from payloads) | Update cadence | As-of reconstructable | Licensing | Fallback | Required phase |
|---|---|---|---|---|---|---|---|---|---|
| Per-fixture absences | verified (PL 2025 only) | `/injuries?league&season` (also `?fixture`) | PL 2025: 3,417 rows in 1 page | `type`: Missing Fixture 3,201, Questionable 216. `reason` such as Knee Injury or Hamstring Injury. Other competitions and seasons untested | **Publication time not given** | **Partly.** Rows are keyed to fixtures, but with no `published_at` a backtest can't tell whether the report existed before kickoff. Needs daily snapshots (task 5) | [verify] | Statistical availability model (§7.1) | 3a–b |
| **Injury expected-return date** | unavailable | Not in `/injuries` (fields: player id/name/type/reason, team, fixture, league) | – | Absent | – | – | – | Injuries go to the statistical bucket (§7.1); that bucket's share will be large | 3a–b |
| Past injury spells | verified (1 player) | `/sidelined?player` `type/start/end` | Back to at least early 2025 for the probed player | 1 player probed | not measured | **No as an expected return.** `end` is known only in hindsight (leakage) | [verify] | Use only for history covariates (`matches_since_return`), never as a pre-match return date | 3, 5 (ramps) |
| Squads, transfers | unverified | `/players/squads`, `/transfers` | – | not probed | – | Squads: current only [verify]. Needs snapshots | [verify] | – | 3a |
| Competition eligibility / registration | unavailable (structured) | – | – | Spec §7.1: not reliably available | – | – | – | `competition_eligible` defaults to true; corrected by override | 3a |

## 5. Venue, weather, crowd, physical (Phase 2, backlog)

| Field | Status | Source, endpoint | Historical availability | Coverage (from payloads) | Update cadence | As-of reconstructable | Licensing | Fallback | Required phase |
|---|---|---|---|---|---|---|---|---|---|
| Venue identity, surface, capacity | verified | `/fixtures` `fixture.venue{id,name,city}`; `/venues` `address, city, country, capacity, surface` | All audited seasons | Venue on every fixture | – | Yes | [verify] | – | 2 |
| **Venue coordinates (lat/lon)** | unavailable in API-Football | `/venues` has no lat/lon (probed id 556) | – | – | – | Static | – | Geocode once per venue (Open-Meteo geocoding by city, or a manual table), then cache. **Spec App. C lists weather/altitude as "available" via Open-Meteo, but this precondition isn't covered by API-Football. See open items** | 2 |
| Weather (forecast, climatology), elevation | unverified | Open-Meteo | – | Not probed in this audit | Forecast horizon about 16 days (§6.2) | Forecast: capture live. Climatology/archive: reconstructable [verify] | Open-Meteo free tier terms vs commercial use **[verify]** | Climatology before forecast (§6.2) | 2 |
| **Attendance** | unavailable | No field in `/fixtures` | – | 0% in all 51 seasons | – | – | – | Dynamic home advantage absent (backlog) | backlog |
| **Physical load (distance, sprints)** | unavailable | No field in player statistics (11 stat groups, none physical) | – | 0% in all 51 seasons | – | – | – | Workload risk dropped (§7.2, backlog) | backlog |
| **Tracking data** | unavailable | No endpoint | – | – | – | – | – | Not planned | – |

## 6. Market data and external sources (Phase 3, 7)

| Field | Status | Source, endpoint | Historical availability | Coverage (from payloads) | Update cadence | As-of reconstructable | Licensing | Fallback | Required phase |
|---|---|---|---|---|---|---|---|---|---|
| Pre-match odds | partial | API-Football `/odds?fixture` | **No history.** A 2022/23 PL fixture returns 0 results, so retention is short [exact window unverified] | Not measured | Snapshot schedule §12.3 | **No, capture-only.** Snapshots must start in Phase 0 (task 5) | [verify] | Historical-odds source if CLV backtests need older data (§4.1) | 7 (capture from 0) |
| Market values | unverified here | Transfermarkt | – | Not in this audit's scope | – | Point-in-time values [verify] | **Licensing review required** (App. C) | Position priors without market value | 3 (cold-start prior) |
| Contract end dates | unavailable (API-Football) | Transfermarkt | – | Not in this audit's scope | – | [verify] | Licensing review required | Covariate stays in backlog | backlog |

---

## Gate check against Appendix C

| Model term (App. C) | Phase | Capability verdict | Effect |
|---|---|---|---|
| Static Dixon-Coles baseline | 1a | Results verified for all 51 seasons; NT/ET split clean except FA Cup qualifying | **Go** (exclude or NULL the unreconciled cup matches) |
| Random walk, AR(1), home advantage | 1b | Results only, verified | Go |
| xG auxiliary likelihood | 1b | Top-5 2023+, Championship 2023+; Top-5 2022 partial; other Tier-2 not before 2025 | Gated per season |
| Manager-change widening | 1b | Coach per match from lineups, ≥99% in leagues | Go (App. C said "verify") |
| Neutral / host flag | 1b | Derived for club matches (2026-11-13); national-team host flag not yet | Club: go (review cases settled 2026-11-13); national: Stage D |
| Style covariates | 2 | Shots, possession, passes verified in leagues | Reduced set, as planned |
| PPDA, directness | – | Unavailable | Stays not planned |
| Weather, altitude | 2 | Needs venue coordinates, which API-Football doesn't provide | **Blocked on geocoding**: App. C says "available" |
| Referee strictness | 2 | Names verified; pre-match assignment timing needs capture | Go for history; pre-match needs task 5 |
| Status feed, availability | 3a–b | Per-fixture absences, no return dates, no publish time | Statistical bucket dominates; daily snapshots required |
| Formation, slot codes | 3c | Grid ≥99.8% in leagues | Go |
| Player rates, ramps | 3 | Player stats verified in leagues | Go |
| Goal decomposition | 3b | Penalty / own goal verified; no set-piece type | Set-piece allocation needs another signal |
| Discipline, corners | 3b | Verified in leagues | Go |
| Half-split | 3b | Half-time score verified | Go |
| ET and shootout models | 3d | Clean in cup proper rounds only | Proper rounds only |
| Workload injury risk | backlog | Physical load unavailable | Blocked (confirmed) |
| Attendance home advantage | backlog | Attendance unavailable | Blocked (confirmed; App. C said "unknown") |
| CLV | 7 | No odds history | Capture-only: task 5 is time-critical |

## 7. National teams (D35 Stage 0, for Stage D)

Evidence: `/leagues?country=World` (1 request, 181 competitions, 2026-11-13) and the capability
audit of `config/national_competitions.yml`, 22 senior men's competitions x 78 seasons, 40
sampled finished fixtures per season (seed 20261105), 245 requests:
`reports/capability_audit/20261113T074231Z.csv` (+ `.manifest.json`). Rows are in
`competition_source_capability`; the competitions are stored with `is_national = true`.
Youth, women's and Olympic competitions are out of scope (CLAUDE.md rule 10 for youth).

| Field | Status | Source, endpoint | Historical availability | Coverage (from payloads) | Update cadence | As-of reconstructable | Licensing | Fallback | Required phase |
|---|---|---|---|---|---|---|---|---|---|
| Fixtures and results | verified | `/fixtures?league&season` | World Cup 2014 on; Euro 2016 on (the Euro 2016 qualifiers of 2014-15 are absent; the Euro 2020 season includes its 2019 qualifiers); World Cup qualifiers from 2015 (Europe from Sept 2016); Nations League 2018 on; Friendlies 2017 on; Copa America 2016 on; AFCON 2017 on; Asian Cup 2019 on; Gold Cup 2017 on. The Euro 2028 and World Cup 2030 qualifiers are not listed yet | Every finished fixture | not measured | Yes | [verify] | – | D v0 |
| Lineups | verified (competitive), partial (friendlies) | `/fixtures?ids` | as above | Competitive competitions about 100% from 2018, except AFCON 2019 (23%) and AFCON qualifiers 2021 (28%). Friendlies 50-82% to 2024, **35-38% in 2025-26** (many minor nations) | not measured | Final XI: yes; release timing needs live capture | [verify] | D v1 squad offset falls back to team-only | D v1 |
| Team statistics (shots etc.) | partial | statistics | as above | Euro, World Cup and Copa America from 2016-18, Nations League and European qualifiers from 2020: 100%. African, Asian and CONCACAF qualifiers 0-72% (rising); Oceania 0%; Friendlies at most 28% | not measured | Yes | [verify] | v0 is results-only | D v1 |
| xG | partial (recent only) | statistics `expected_goals` | 2024 on, top competitions only | Euro 2024, Copa America 2024, World Cup 2026: 100%; Nations League 2026 82%; European WC qualifiers 2024 65%; elsewhere about 0 | not measured | Yes | [verify] | v0 results-only, as the spec says | later |
| Referee | verified (from 2019) | `fixture.referee` | as above | 90-100% in competitive competitions from 2019; 0% for World Cup 2014, AFCON 2017, Gold Cup 2017, African qualifiers 2018; Friendlies 23-82% | – | Name yes; assignment time needs capture | [verify] | – | – |
| Host / neutral (national) | unverified | venue name and city on `/fixtures`; venue country not given | – | Not measured. Needs the venue's country (geocoding returns one) compared with each team's country | – | Yes (published pre-match) | – | Host flag by venue country; travel from the capital or the national stadium | D v0 |

Gate (Stage 0 national): results and lineups verified, capability table filled. Still open:
the host flag, live capture before the March 2027 window, and the history load (about
1,000-1,500 requests).

## Open items

1. **Cups by stage.** Capability is recorded per season, but cup coverage depends on the round
   (§3.4 lists stage as a dimension). Decide: restrict cups to proper rounds, or add stage to the
   capability grain.
2. **Mid-season start dates.** xG in Top-5 2022/23 begins about mid-January 2023. The audit
   currently records the whole season as `partial`. §3.4's `valid_from` could carry the true
   start date (first fixture with the field, refined by a denser sample around it).
3. **Venue coordinates.** This conflicts with App. C ("Weather, altitude: available"). It needs a
   geocoding step and a venue table before Phase 2.
4. **Not yet probed:** `/players/squads`, `/transfers`, `/coachs` (not needed for 1b: coach per match from stored lineups covers 99.6% of bundle team-matches, measured 2026-11-07), Open-Meteo, odds retention
   window, and injury coverage outside PL 2025.
5. **Update cadence and licensing** columns stay open until snapshot capture runs and the
   API-Football / Open-Meteo / Transfermarkt terms are reviewed.

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
- [ ] UEFA detail backfill 2016-2025 (`adatta backfill-run`, by hand).
- [x] 2026-11-13: neutral venue flag for club matches, `adatta venues-neutral`
      (`adatta/features/neutral.py`, `config/neutral_venues.yml`): venue identity against
      each team's league grounds at kickoff, then round rules, then per-fixture overrides.
      Bundle league matches 99.1% home (the rest: 342 with no venue, 42 temporary grounds);
      all 250 rule-covered finals and semis decided. The 25 review cases were settled by hand
      the same day (22 overrides in `config/neutral_venues.yml`, a rule for the Championship
      play-off final; one left unresolved). `--write` set `match.neutral` on 362 matches.
- [ ] Third tiers.

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

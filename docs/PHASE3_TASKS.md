# Phase 3 — squad and player (spec §7, §8.1, §19.3)

Goal: let the team model see who plays. Per-player availability, selection and minutes
produce a lineup offset Δ_lineup that enters log λ exactly once (Layer 6, §8.1; CLAUDE.md
rule 7), fitted inside the training likelihood (§7.5, adopted design) so the strength walk
tracks a team's baseline with a typical lineup instead of absorbing injuries as strength
changes. The model it builds on is the production `phase1b_strength_open_xg`; Phase 2
closed on 2026-11-16 without a promotion (D37).

Status (2026-11-16): **stated, not started.** Order per §19.3, each step validated before
the next: (a) status feed and squad lists; (b) availability and selection, validated
standalone; (c) formation and slot-code roles, Manager entity; (d) lineup offset in the
likelihood vs serving-only; (e) the feedback loop wired into Layer 6. Phase 3b (goals
decomposition, discipline, half split) and 3d (knockouts, now Stage B) follow later.

Tick each item only when its acceptance check passes.

## Data position (measured 2026-11-16, dev seasons, no requests)

| What | Where | Coverage | As-of |
|---|---|---|---|
| Lineups: starting XI, bench, formation, grid, position code, coach | stored `/fixtures?ids` payloads (R2, `match_detail.flags.has_lineups`) | tier 1-2 dev league matches 2016-2023: 99.4-100% per league | post-match fact for history; live, first seen a median 22.7 min before kickoff (register 3c.1) |
| Player match stats: minutes, position, rating, goals, assists, shots, key passes, tackles, cards | same payloads, `players[]` (`has_player_match_stats`) | five top flights 100%; Championship 99.8%, Segunda 99.2%, 2. Bundesliga 95.9%, Ligue 2 95.7%; **Serie B 60.2%, only from 2019** | post-match fact |
| Substitution events with minute | same payloads (`has_substitution_timestamps`) | as lineups | post-match fact |
| Pre-match status (injured, suspended, doubtful) | `player_status_snapshot`, daily capture | **from 2026-11-05 only**: 632 rows on 294 matches; empty during the international break (the feed lists absences only for fixtures ahead) | as-of, cannot be reconstructed (§7.1) |
| Pre-match lineups | `lineup_snapshot`, capture from T-75 min | from 2026-11-07: 198 matches | as-of |
| Squad lists, registrations | `/players/squads` (not captured); `player_registration` | **0 rows** | would be live snapshots only |
| Players | `player` | 146 rows (from status capture only); payload players not normalised yet | n/a |
| Market values (cold-start prior, §7.4) | Transfermarkt | not loaded; no official API, terms not reviewed (register B.5) | current value only, past values not as-of |

Consequences:
- The **lineups-known horizon** (the confirmed XI, about 20-60 min before kickoff) can be
  backtested on every dev season from stored payloads, with no status history needed.
  The stored XI is the final one, so a late change after release is missed (rare; the live
  period measures it).
- **Earlier horizons** (initial, T-7d, T-24h) need the availability and selection model.
  Selection can be learned from realised appearances in dev, but its status inputs exist
  only from 2026-11-05, so a status-informed model can only be judged live (D39).
- The **cold-start prior** from market values is blocked on a Transfermarkt licensing
  review (owner, CLAUDE.md: ask before a new source). Until then, new and sparse players
  get the position x league pooled rate (§7.4 pooling) and are flagged in the confidence
  profile.
- **Minors.** Senior squads include under-18 players. Their appearances load like everyone
  else's (owner, 2026-11-15: the project is private and personal, so loads are not
  filtered). Per-player outputs for them stay internal: none is betting-facing without the
  owner's review (CLAUDE.md rule 10).

## Spec points raised (CLAUDE.md: raise, don't silently diverge)

1. **§7.5 and §8: "must show on the locked holdout"** that the in-likelihood offset beats
   serving-only. The 2024/25-2025/26 holdout has been opened twice already (1b, Phase 2).
   Proposed: the live 2026/27 period judges the promotion (D38, open).
2. **§7.1 status snapshots** exist only from 2026-11-05, so availability has no dev
   history. Proposed: dev backtests at the lineups-known horizon, and the availability
   model judged live; optionally the provider's past `/injuries` lists as realised-absence
   labels only (D39, open).
3. **§19.3 Phase 1b gate "every Phase 2/3c covariate has a verified entry"**: the register
   covers 3c (formation, slots, manager) but not 3a, 3b or the Layer 5 rates. P3-0 adds them.

## Decisions

- **D37 (resolved 2026-11-16):** Phase 3 next; Stage A finishes A1 and A2; Stages B-D wait.
- **D38 (open):** promotion window for step (d). Leaning: live 2026/27.
- **D39 (open):** pre-match availability before live capture. Leaning: both, the
  `/injuries` probe priced in requests first.

## Tasks

- [x] **P3-0 Player data from stored payloads (no requests).** Acceptance (stated
      2026-11-16, before building):
      (a) **Register.** `docs/FEASIBILITY_REGISTER.md` gains rows for the 3a/3b inputs and
      the Layer 5 rates: status feed, squads, appearances, minutes, each per-90 statistic.
      Each row records its coverage per bundle league-season 2019-2025, its as-of
      availability and what it degrades to.
      (b) **Tables.** A forward-only migration adds `player_appearance`, one row per player
      per team-match. Columns: started, bench, minute on and off, minutes, position code,
      grid, slot code (from formation + grid, §7.3), the per-90 statistics the payload
      gives, `source_id` and the temporal fields. Players are keyed by provider id, so one
      player is one row in `player` across clubs. The payload's shape is known only to the
      adapter (`api_football_stats`).
      *Amended 2026-11-16 while building, before any result was read: (1) storage is one
      Parquet file per competition-season in the object store
      (`players/appearances/v1/<league>/<season>.parquet`), not a Postgres table. About
      3.7M rows would take roughly 2.5 GB at `team_match_stat`'s size per row, against
      a 281 MB database today on the free tier. The model reads appearances in bulk, and
      every file is rebuilt from the raw payloads. `player` stays in Postgres. (2) The slot
      code moves to P3-c, which defines the formation vocabulary; the grid is stored, so
      nothing is lost.*
      (c) **Checks.**
      - Each team-match with a lineup has 11 starters.
      - A starter's minutes equal the match length or their substitution minute. A
        substitute's minutes come from their minute on. The payload's `games.minutes` is
        compared with this, and disagreements are reported, not fixed silently.
      - Own goals are never credited to a scorer (§7.4).
      - Rows exist only for matches whose detail is stored; a missing statistic stays
        null, never 0 unless the provider lists it.
      (d) **Report.** Coverage by league-season: appearances, minutes agreement, statistics
      present. Dev and warm-up seasons in the report; holdout seasons are loaded (needed
      for the live fits) but not summarised for modelling choices.
      (e) **Tests.** Parsing on synthetic payloads (starter subbed, substitute subbed off,
      red card, extra time, missing minutes, own goal, a player with two clubs in a
      season).
      (f) **Leakage test.** A loader at as-of t returns only appearances from matches final
      by t.
      **Done 2026-11-16; (a)-(f) pass.**
      - Code: `api_football_stats.player_appearances`, `adatta/features/appearances.py`
        (`build`, `load`, `AppearanceHistory.as_of`, `report`), the CLI commands
        `appearances-build` and `appearances-report`, and `tests/test_appearances.py`
        (5 tests).
      - Build: 362 competition-season files, 3,695,388 rows from 91,726 matches; players
        are registered in `player`. 8,345 finished matches have no lineup.
      - Counted and skipped:
        - 5,222 lineup entries with no player id;
        - 4,419 unclear substitutions (0.6%);
        - 12,345 statistics entries for a player not in the lineup;
        - 2,541 rows whose team is neither side of the match (0.07%).
      - Bundle leagues, dev seasons 2019–2023:
        - 11 starters in at least 99.9% of team-matches;
        - event goals agree with the provider's per-player total in 99.8–100%;
        - per-90 counts are collected in at least 99.2% of team-matches;
        - event minutes are within 3 min of the provider's for a mean of 89.8% to 96.7%
          per league. Lowest: Serie B 2019/20 at 66.6%, where the provider counts stoppage
          time.
      - Register section 4b added. Holdout seasons are built (the live fits need them) but
        not summarised.
- [x] **P3-a Status feed and squad lists.** Daily status capture runs (since 2026-11-05).
      To add: squad lists per bundle team into `player_registration` (versioned,
      `valid_from/to`), through the quota planner, with the request cost stated first.
      - **Probe (1 request, team 33):** `/players/squads?team` gives the current squad
        only: player id, name, age, number and position, with no dates. So history starts
        with the capture.
      - **Cost (stated before building):** one request per team per day. That is 198
        requests for the ten bundle leagues' 2026/27 teams, about 2.6% of the
        7,500/day quota. Daily use in the 8 days to 2026-11-16 peaked at 1,483, so it goes
        to about 1,680 (22%), well under the 70% upgrade trigger.
      - **Acceptance** (written while building, before the first live capture):
        (a) the client method `squad` and the adapter `parse_squad`, with the raw payload
        kept and every request through the quota planner's live reserve. Age and photo are
        not stored.
        (b) Registration spells:
        - a listed player without an open spell gets one (`valid_from` = response time);
        - a changed number or position supersedes the spell;
        - a player no longer listed has their spell closed (`valid_to`, not superseded);
        - an empty answer changes nothing;
        - a second run the same day is not made (daily job).
        (c) Tier 1-2 leagues only, inside the capture tick's daily jobs.
        (d) Tests with the fake provider.
        (e) On the first live run, at least 95% of the 198 teams return a squad, and at
        least 90% of the players who have played for a club in 2026/27 are in its list.
      - **Done 2026-11-16; (a)-(e) pass.**
        - Code: migration 0014 (`player_registration.shirt_number`, `position`, an index
          on open spells), `api_football_capture.parse_squad`, `ApiFootballClient.squad`,
          `capture.jobs.capture_squads` / `apply_squad` / `squads_due`.
        - Tests: 2 in `tests/test_capture.py`.
        - First live run, logged as the day's `squads` job: 198 of 198 teams answered (198
          requests; 765 used today); 5,989 open registrations, a median squad of 30.
        - 4,659 of the 4,829 players who have played for a club in 2026/27 (96.5%) are in
          its current list. The rest are mostly expected to be players who have since
          left.
        - From the next push the capture tick runs it daily.
- [ ] **P3-b Availability, selection and minutes, validated standalone (§7.1-§7.2).**
      `P(start | available)` and an expected-minutes distribution per player.
      **Acceptance stated 2026-11-16, before building and before any selection result is
      seen.** This is a standalone component study on dev only. It changes no production
      forecast, and the locked holdout is not touched.

      (a) **What is predicted.** For each team-match, each player in the team's candidate
      pool gets three probabilities: start, bench, or not in the matchday squad (§7.2:
      starters, N bench, the rest healthy scratch or out). Each player also gets a minutes
      distribution: zero-inflated, with Beta-scaled minutes given a start and given
      coming on (§7.2). Matches are bundle-league matches only. Minutes are on the 90'
      scale (`minutes_nt`, the model's own scale).

      (b) **When it is predicted, and from what.**
      - The as-of time is kickoff − 24 h, the T-24h forecast.
      - Inputs are only appearances final by then (`AppearanceHistory.as_of`), plus that
        match's context as of then: congestion (days since the team's last match and to
        its next, all loaded competitions, D25's labelled look-ahead for the forward
        side) and importance.
      - Pre-match status is not used on dev; it doesn't exist before 2026-11-05 (D39).
        Bench size is per competition-season, the largest bench seen in its matches before
        the as-of time.

      (c) **Candidate pool.**
      - Every player with a matchday-squad appearance for the team in the 365 days
        before the as-of time.
      - A player is dropped once they have appeared for another club after their last
        appearance for this one.
      - Live, the pool also includes the current squad list (P3-a).
      - Reported: the share of actual starters and bench players who were in the pool.
        Players outside it are new arrivals, the cold start; they are scored with the
        pool's leftover mass, so they are never silently dropped.

      (d) **Model to build.** A Bayesian hierarchical multinomial-logit model (start /
      bench / out), with player effects pooled player → position → league.
      - Covariates:
        - recent starts and squad selections (last match, exponentially weighted over
          about 10 matches);
        - minutes in the last match;
        - days since the player's last appearance, and the length of a gap ended by a
          return (injury-return ramp, §7.4);
        - matches since their first appearance for the club (new-signing ramp);
        - sent off in the team's last match;
        - whether the team's coach changed since their last start (the coach of the team's
          last match, known at the as-of time);
        - team congestion;
        - importance.
      - Fitted per bundle with NumPyro (D10), on the dev walk-forward plan: refit every 28
        days (D14), training on everything final by the refit date from 2016/17.
      - The §14.5 inference gates apply to the fits (R-hat, ESS, divergences, PPC of starts
        per team = 11). A fit that misses the CI time budget is raised before any
        approximation is substituted.
      - Per team-match, posterior draws give 11 starters and up to N bench by weighted
        sampling without replacement. Exactly one goalkeeper starts. The marginal
        probabilities are read off the samples, so they sum to 11 starters and N bench by
        construction.

      (e) **Baselines.** Both are fitted on warm-up seasons only (2016/17-2018/19) and fixed
      before any dev score is seen.
      - *B1, previous XI:* a player's three-way probabilities from the empirical rates for
        their outcome in the team's previous match (started / bench / out → start / bench /
        out next), by position family.
      - *B2, selection frequency:* the player's exponentially weighted start and bench
        shares over the team's last 10 matches, shrunk toward the position-family mean by
        a constant fitted on warm-up.
      - Both are normalised per team-match to 11 starters and N bench. Their minutes
        distributions are the player's empirical minutes over their last 10 matches, or the
        position-family distribution if they have fewer than 3.

      (f) **Metrics.** All on dev bundle-league matches 2019/20-2023/24, paired by
      team-match.
      - Three-way log loss per player (primary).
      - Brier score of start.
      - XI overlap: the actual starters among the 11 most likely.
      - CRPS of `minutes_nt`.
      - ECE of P(start) in 10 bins.
      - Reported overall, by league, by position family, and for the cold-start and
        return-from-gap subsets.

      (g) **Pass.** All of the following must hold:
      1. The three-way log loss beats **both** baselines overall, with the paired 95%
         interval excluding zero.
      2. The point estimate beats B2 (the stronger of the two on warm-up, else B1) in
         every bundle league.
      3. Minutes CRPS beats both baselines overall, with the paired 95% interval excluding
         zero.
      4. ECE of P(start) is at most 0.02 overall and at most 0.03 in every league.
      5. The inference gates pass in every refit (the dispersion PPC is a warning, as
         D20).
      6. No dev appearance after the as-of time changes any prediction (leakage test).

      A fail is reported with its segment. The model is respecified only on dev, and the
      respecification is disclosed.

      *Amended 2026-11-16 while building, before any model or baseline score on dev was
      seen:*
      1. **Context timing.** Congestion, next-match kind and importance come from the
         stored h48 context snapshot (b29bb7f0915abf76), i.e. as of kickoff − 48 h, a day
         earlier than the forecast. That is less information than T-24h, never more. A
         T-24h snapshot would need a new context build.
      2. **Coherent marginals.** The per-team totals are computed exactly per posterior
         draw by capped proportional rescaling ("water-filling"): starts sum to 11 with one
         goalkeeper, bench places to N, each player's three sum to 1. They are then
         averaged over draws. This replaces Monte Carlo sampling without replacement: it
         gives the same constraints without sampling noise.
      3. **Newcomers.** Expected newcomer starts and bench places are taken off the pool's
         totals, the same for every model. The figure is the league's training-window mean
         for the model, and its warm-up mean for the baselines. Newcomers have no player
         row, so they are counted and reported per team-match but not in the paired
         per-player metrics.
      4. **Bench size N** is the team's mean bench over its last 5 matches. The
         "largest bench seen in the competition-season" would overstate N for teams that
         don't fill the bench.
      5. **Sampler settings.** 4 chains × 500 draws, after 500 tuning steps. Predictions use
         200 thinned draws, and the minutes pmf 50 of them.
      6. **Jobs.** Each bundle-season runs in parts (`--part i/n`) to keep jobs under 6 h.

      **Built 2026-11-16, run pending:**
      - Code: `adatta/features/selection.py` (pool, covariates, outcome),
        `adatta/eval/selection.py` (inputs, table store, leakage check, baselines,
        coherent probabilities, minutes pmf, metrics, verdict, report) and
        `adatta/model/selection.py` (PyMC model, NumPyro sampler, walk-forward parts,
        assemble).
      - CI: `.github/workflows/selection.yml`.
      - Tests: `tests/test_selection.py` (7) and `tests/test_selection_model.py` (CI model
        job).
      - Selection table `061ebfdb66114294` (2016/17-2023/24, built locally in about 5
        min, stored in R2 only):
        - 2,011,411 player rows from 62,325 team-matches; median pool 32;
        - 11 starters in 99.9% of team-matches;
        - 99.1% of actual starters were in the pool (99.0-99.3% in every season); 0.30
          newcomers per team-match on the bench;
        - leakage check: 0 of 100 sampled matches changed when every later appearance was
          removed.
      - It runs after the A1 cup run (one backtest-type run at a time).

      (h) **Live check** (not part of the dev pass). The status-informed variant is judged
      on the live period once 1,000 bundle team-matches with status snapshots are scored
      (D39):
      - a "missing" status is a hard fact (P(start) = 0);
      - a "questionable" status enters as a covariate.
      It is measured against the dev model on the same matches with the same metrics.
- [ ] **P3-c Formation, slot-code roles, Manager entity (§7.3).** Register 3c rows are
      verified (formation 99.65%, grid 99.65%, coach 99.59%). Acceptance stated before
      building.
- [ ] **P3-d0 Player rates, Layer 5 (§7.4).** Per-90 rates pooled player → position →
      league, with a dispersion test by position and statistic (§7.4). Market-value prior
      only after the licensing review. Acceptance stated before building.
- [ ] **P3-d Lineup offset: in the likelihood vs serving-only (§7.5).** Both variants
      against the production 1b. Dev walk-forward at the lineups-known horizon, then the
      earlier horizons with P3-b's projected XI. Gate in the D18 form (1X2 log loss and
      RPS, paired 95% interval excluding 0, no league x market segment worse than 1.0%
      per D28, §14.5 inference gates, feasibility). σ_walk is reported for both variants.
      Promotion window per D38. Acceptance stated in full before building.
- [ ] **P3-e Feedback loop into Layer 6 (§7.5 steps 1-4).** Player goal shares sum to the
      team's non-own-goal rate by construction; own goals stay outside the allocation.

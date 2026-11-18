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
   Resolved: the live 2026/27 period judges the promotion (D38).
2. **§7.1 status snapshots** exist only from 2026-11-05, so availability has no dev
   history. Proposed: dev backtests at the lineups-known horizon, and the availability
   model judged live; optionally the provider's past `/injuries` lists as realised-absence
   labels only. Resolved: both, the probe priced first (D39).
3. **§19.3 Phase 1b gate "every Phase 2/3c covariate has a verified entry"**: the register
   covers 3c (formation, slots, manager) but not 3a, 3b or the Layer 5 rates. P3-0 adds them.

## Decisions

- **D37 (resolved 2026-11-16):** Phase 3 next; Stage A finishes A1 and A2; Stages B-D wait.
- **D38 (resolved 2026-11-17):** the step (d) promotion is judged on the live 2026/27
  period; the holdout is not opened a third time.
- **D39 (resolved 2026-11-17):** both. Dev backtests at the lineups-known horizon; the
  status-informed model judged live; the past `/injuries` lists priced in requests
  first, and if loaded used only as realised-absence labels.
  *Priced 2026-11-17 (4 test calls, raw payloads kept):* one request returns a whole
  league-season in one page. Premier League 2019 and Championship 2019 are empty (no
  history that far back). Premier League 2021: 2,563 rows on 350 of 380 fixtures, from
  2021-08-21; 2023: 3,853 rows on 380 fixtures (3,228 "Missing Fixture", 625
  "Questionable", with a reason such as "Knee Injury"). No publication time, as expected.
  The full probe is about 40 requests (10 bundle leagues x 2020-2023, holdout seasons not
  needed), under 1% of a day's quota. Dev seasons 2019 and 2020 (probably) and warm-up
  have no labels.

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
- [x] **P3-b Availability, selection and minutes, validated standalone (§7.1-§7.2).**
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
         D20; the starts and bench PPC too, amendment 8).
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

      *Amended 2026-11-17 after the first timing pilot (run 43931271338, England 2019,
      part 0/4), before any model or baseline score on dev was seen:*
      7. **Feasibility.** The pilot's first refit took 5 h 3 min and failed the R-hat and
         ESS gates (R-hat > 1.01, ESS < 100 per chain, 1 divergence). The second refit was
         cut off by the 6 h job limit, so no part finished and nothing was scored. The
         model as built could not run the dev walk-forward (about 325 refits). Three
         changes; the pass rule (a)-(g) and the baselines stay as stated:
         - **No per-player effects.** The player's own tendency enters through the
           covariates (`ew_start`, `ew_squad`, `last_start`, `never_started`, ...). The
           position effect is a fixed offset with the goalkeeper as the reference level
           (the hierarchical position scale on 5 levels, with a free league intercept,
           was weakly identified).
         - **Five separate fits.** The selection logit, full 90, coming on and the two
           minutes Betas share no parameter, so each is sampled on its own. The joint
           posterior is their product, so the predictions are the same as one joint fit.
           The inference gates are taken over all five (worst R-hat and ESS, total
           divergences).
         - **Training window.** Each refit trains on rows final by the refit time and
           with an as-of time in the 730 days before it (was: all history from 2016/17).

         Each refit now prints its time per sub-model and its gate values, and saves its
         part to R2 as it goes, so a job cut off at 6 h keeps the refits it finished.

      *Amended 2026-11-17 during the re-pilot (run 44027737564), before any model or
      baseline score on dev was seen; owner: "Yes, make it a warning":*
      8. **Starts and bench PPC are a warning.** Refit 2 of the re-pilot converged (R-hat
         1.006, ESS 636, no divergences) but its starts PPC was q = 0.02. The check reads
         the raw softmax, whose totals are free; the served probabilities are water-filled
         to 11 starters and N bench by construction (amendment 2), which is what the
         acceptance's "PPC of starts per team = 11" asks. Two 90% bands in each of about
         325 refits would also fail a well-specified model by chance. So R-hat, ESS and
         divergences gate every refit, and the PPC is reported as a warning, as D20 did
         for goal dispersion: the share of refits outside the central 90% (about 10%
         expected) and the q deciles. Calibration stays gated by ECE on dev outcomes
         (g)4.

      *Amended 2026-11-17 after the dev run (run 44033304307), whose scores had been seen;
      owner: "Yes, rerun the 11":*
      9. **Longer sampling for marginal refits.** The run failed only (g)5: 11 of 283
         refits each had one parameter at R-hat 1.010-1.011 (limit 1.01), with bulk and
         tail ESS 587 or more and no divergences in the whole run. Those 11 refits are
         redone with 2,000 draws per chain after 2,000 tuning steps (was 500/500), and
         their team-matches and diagnostics replace the first run's
         (`.github/workflows/selection_rerun.yml`, `assemble --override`). The model,
         baselines, gates and pass rule are unchanged; only sampler length changes, and
         only for those refits. Scores were seen before this choice, so it is disclosed
         here: it touches 11 of 283 four-week blocks, against a model margin of about 12%
         in log loss over B2.

      **Dev run 2026-11-17 (run 44033304307, 50 jobs, 283 refits, median fit 17 min):**
      verdict fail on (g)5 only, 11 refits at R-hat 1.010-1.011. Every other check
      passed (below).

      **Result 2026-11-17: pass** (rerun 44110912496 per amendment 9, assembled over the
      dev run). The 11 refits redone at 2,000 draws all pass the gates, so all 283 refits
      do; all 27 checks of (g) pass. The redone refits barely move the scores (log loss
      vs B2 per team-match -2.7324 [-2.8021, -2.6627], was -2.7321). Production is
      unchanged: P3-b is validated standalone and feeds P3-d0/P3-d. Figures:
      - three-way log loss per player row 0.591 against B1 0.693 and B2 0.672, the
        paired interval clear of zero against both; below B2 in all 10 leagues, every
        position, cold-start and returning players;
      - minutes CRPS 9.50 against 11.61 (B1 and B2 share the minutes baseline);
      - ECE of P(start) 0.0077 overall, at most 0.011 per league;
      - XI overlap 8.49 against 8.13 (B1) and 8.08 (B2); leakage 0 of 100;
      - PPC warning: 0.7% (starts) and 0.4% (bench) of refits outside the central 90%.

      **Built 2026-11-16, reworked 2026-11-17 (amendment 7):**
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
- [x] **P3-c Formation, slot-code roles, Manager entity (§7.3).** Register 3c rows are
      verified (formation 99.65%, grid 99.65%, coach 99.59%).
      Acceptance (stated 2026-11-17, before building; no formation, slot or tenure figure
      on dev has been looked at beyond the register's coverage counts):

      **Scope.** Three parts, each judged on its own: (1) slot codes, (2) Manager and
      tenure, (3) a formation model. Plus a role-assignment check that uses all three.
      Nothing reaches production: selection (P3-b) stays as passed, and conditioning it
      on formation is a later challenger. The Layer 5 rates (P3-d0) and the lineup offset
      (P3-d) use what P3-c builds. Dev seasons 2019-2023, warm-up 2016-2018, no holdout
      season loaded (the loader refuses > 2023, as P3-b).

      (a) **Slot codes (data).** From the stored `/fixtures?ids` payloads (0 requests):
      per team-match the formation string, and per starter the grid "row:col" mapped
      deterministically to a slot code (GK, RB, RCB, CB, LCB, LB, RWB, LWB, CDM, CM, CAM,
      RM, LM, RW, LW, ST and so on; §7.3). A rule table per formation row shape, in code,
      with a unit test for every formation in the vocabulary.
      - **Orientation.** Which grid column is the right side is not documented by the
        provider. It is fixed from a hand-checked sample of 20 well-known full-backs and
        wide players. If the sample doesn't settle it (fewer than 18 of 20 agree), codes
        are side-neutral (FB, WB, CB, WM, W) rather than guessed.
      - Stored as Parquet in R2 beside the appearances (`players/lineups/v1/...`), with
        the temporal fields and `source_id`, as P3-0 did for appearances (Neon free
        tier); the spec's Lineup entity is this table.
      - **Checks:** every starter of a team-match with a formation and a full grid gets
        exactly one code; exactly one GK per team-match; the codes' line counts match the
        formation string; slot family agrees with the provider's G/D/M/F code for at
        least 95% of starters where that code exists. Reported per league-season.

      (b) **Manager and tenure.** Tables `manager` and `manager_tenure` (migration
      0015, forward-only) from `match_coach`: a tenure is a run of consecutive team
      matches under one coach, so a coach who leaves and returns has two. Start and end are
      the first and last match in the tenure (exact to the match; register 3c.5: the
      provider's dates are month-only and incomplete).
      - **As-of:** a tenure's end is not known while it runs. A forecast at T sees the
        team's coach from its last match final by T and that tenure's length up to T,
        never the end date.
      - **Checks:** every `match_coach` row is in exactly one tenure; a team's tenures
        don't overlap; the leakage test of (c) also covers the manager inputs.

      (c) **Formation model** (§7.3), at T-24h like P3-b:
      - **Vocabulary:** the formations covering at least 97% of warm-up team-matches, the
        rest as "other". Fixed on warm-up only.
      - **Model:** a conditional multinomial logit over the vocabulary, sampled with
        NumPyro (P3-b's lesson: covariates, no per-team random effects). Inputs: the
        team's recency-weighted formation shares; the coach's career shares pooled across
        their earlier clubs, shrunk towards the team's by the coach's match count; the
        team's last formation; opponent tier; home/away; importance. Opponent tier is the
        production model's pre-match P(win) - P(loss) for the team, from the stored dev
        backtest predictions (as-of by construction; a model output, not odds); 0 where a
        match has none.
      - **Walk-forward:** refit every 28 days on the 730 days before (as P3-b
        amendment 7), per bundle.
      - **Baselines,** fixed on warm-up: F1 = the team's last formation, smoothed towards
        the league's shares (weight fitted on warm-up); F2 = the team's recency-weighted
        shares, smoothed the same way.
      - **Metrics:** multiclass log loss of the actual formation, paired by team-match;
        back-line family (3/4/5 at the back) log loss; ECE of the top class.

      (d) **Role assignment** (§7.3), at the lineups-known horizon: given the actual
      formation and the actual XI, each starter is assigned a slot from the codes they
      have occupied before (as-of), as the most likely complete assignment of the 11 to
      the formation's slots. Baseline R1: the same assignment using only the provider's
      G/D/M/F code and the team's slot shares. Metric: share of starters whose assigned
      code equals the actual one, paired by team-match; reported for new signings and
      players with fewer than 5 coded starts.

      (e) **Pass.** All of the following must hold:
      1. (a) and (b) checks pass in every dev league-season (a shortfall in one season is
         reported with its segment and degrades to formation-only codes there, never to
         invented ones).
      2. Formation log loss beats both F1 and F2 overall, with the paired 95% interval
         excluding zero, and the point estimate beats the stronger of the two in every
         bundle league.
      3. Back-line family log loss is no worse than the stronger baseline overall (point
         estimate).
      4. ECE of the top class is at most 0.03 overall.
      5. Inference gates (R-hat, ESS, divergences) pass in every refit; the PPC of
         formation shares is a reported warning (as P3-b amendment 8).
      6. Role assignment beats R1 overall, paired 95% interval excluding zero.
      7. Leakage: no information final after the as-of time changes any formation or
         role input (100 sampled team-matches, as P3-b).

      Reported, not gating: the first 5 matches under a new coach (does career pooling
      help?), formation log loss by league and by opponent-tier band.

      Cost: 0 API requests (stored payloads and `match_coach`). One CI walk-forward
      run (one backtest-type run at a time).

      *Amended 2026-11-17 while building, before any formation, role or tenure result on
      dev was seen; owner: "Degrade before 2022":*
      1. **The grid is positional only from 2022/23.** The orientation check found that
         grid columns before 2022 are not pitch positions. 779 players who are clearly
         full-backs in 2022-2023 (at least 80% of their back-four starts on one edge, 10 or
         more starts) sit on that edge in only 5-45% of their back-four starts in most
         league-seasons 2016-2021, against 94-100% from 2022; the provider's G/D/M/F
         code agrees with the grid row's line for 74-91% of starters before 2022, against
         94-100% after. Exceptions: Serie A 2019 (86% / 97%) and La Liga 2019 (94% / 99%).
         Columns are fixed with column 1 on the left (from 2022: a left-back in column 1,
         a right-back in the last).
         - **Full slot codes** only in league-seasons that pass a measured test: the
           full-backs on their usual side at least 90%, and G/D/M/F agreeing with the row
           line at least 95%. Full-backs are identified from 2022-2023 only (no holdout
           season read).
         - **Elsewhere,** line-level codes from the provider's G/D/M/F (GK, D, M, F),
           never guessed sides; a starter with no provider code gets U.
         - **Formations** come from the grid rows: the row counts reproduce the
           provider's formation string in 596 of 596 sampled lineups that have one (the 4
           others have neither). So the lineup table is built from the stored
           appearances, without re-reading the payloads.
         - **(a) checks** (one code per starter, one GK, line counts, 95% agreement) are
           required in the positional league-seasons; the others are reported as
           degraded. Pass rule 1 reads accordingly.
         - **(d) role assignment** is judged on positional league-seasons only (in
           practice 2022-2023), with slot history counted from positional matches.
         - The formation model (c) and Manager and tenure (b) are unaffected.

      *Amended 2026-11-17 after the dev run (run 44168855657), whose scores had been
      seen; owner: "Yes, rerun at 2,000":*
      2. **Longer sampling.** The run failed only (e)5: 24 of 283 refits had R-hat
         1.010-1.016 (limit 1.01), 13 of them in Italy (mostly the 4-3-1-2 intercept and
         the per-class home terms), with bulk ESS 448 or more and no divergences. The whole
         dev run is repeated with 2,000 draws per chain after 2,000 tuning steps (was
         500/500), the workflow's `draws` input; nothing else changes. If refits still fail,
         the model is respecified on dev rather than sampled longer.

      **Dev run 2026-11-17 (run 44168855657, 25 jobs, 283 refits, median fit 61 s):**
      verdict fail on (e)5 only. Every other check passed (below).

      **Result 2026-11-17: pass** (run 44186511938, amendment 2: the whole dev run at 2,000
      draws, median fit 103 s). All 283 refits pass the inference gates and all 19 checks
      of (e) pass. Scores are essentially unchanged: vs F2 -4.69% [-5.05%, -4.32%], vs F1
      -14.94%, ECE 0.019, role assignment +0.359. Production is unchanged: the slot codes,
      tenures and formation distribution feed P3-d0 and P3-d. Figures of the first run:
      - formation log loss per team-match 1.455 against F1 1.710 and F2 1.526; vs F2
        -4.7% [-5.0%, -4.3%], vs F1 -14.9%; below F2 in all 10 leagues (-1.9% to -7.4%);
      - back-line family log loss 0.494 against F2 0.526; ECE of the top class 0.018;
      - first 5 matches under a coach: 1.625 against F2 1.757 (reported, not gating);
      - role assignment at the lineups-known horizon: 88.3% of starters in the actual
        slot against R1 52.4% (+0.359 [+0.356, +0.362]); sparse-history players 81.6%
        against 53.3%;
      - (a) and (b) checks pass; leakage 0 of 100; PPC warning 0% of refits outside.

      **Built 2026-11-17:**
      - Code: `adatta/features/lineups.py` (slot rules, positional test, lineup table,
        checks), `adatta/features/tenure.py` (tenures, checks, versioned storage),
        `adatta/features/formation.py` (as-of features), `adatta/eval/formation.py`
        (inputs, baselines, metrics, role assignment, leakage, verdict, report) and
        `adatta/model/formation.py` (conditional logit, NumPyro, walk-forward, assemble).
      - Migration 0015 (`manager`, `manager_tenure`), applied: 5,935 managers and 11,337
        tenures from 182,621 coach rows (all stored competitions); every row in one tenure,
        no overlaps; a rebuild changes nothing.
      - Lineup table `d1d11c0a7f3ce0a7` and formation table `9a05c70602047e45` (R2 only):
        62,080 bundle team-matches 2016-2023; vocabulary of 15 formations plus other
        (97% of warm-up); 21 positional bundle league-seasons (every league in 2022 and
        2023, and La Liga 2019), all passing the (a) checks; leakage 0 of 100;
        16,318 positional dev team-matches for the role check.
      - CI: `.github/workflows/formation.yml`. Tests: `tests/test_lineups.py` (25),
        `tests/test_tenure.py` (2), `tests/test_formation.py` (7),
        `tests/test_formation_model.py` (CI model job).
- [ ] **P3-d0 Player rates, Layer 5 (§7.4).** Per-90 rates pooled player → position →
      league, with a dispersion test by position and statistic (§7.4). Market-value prior
      only after the licensing review.
      Acceptance (stated 2026-11-18, before building; no rate figure on dev has been
      looked at. Only null handling was measured, on four league-seasons, as coverage: in
      the provider's per-player statistics a null means zero. For example, `goals` is
      null for 28-29% of appearances with minutes, and only 0-8 of those players per
      league-season have a goal event; assists are null for 41-46%):

      **Scope.** Per-appearance rates for the six statistics of §7.4:
      - goals (own goals excluded, from goal events as in P3-0);
      - assists;
      - shots;
      - key passes;
      - tackles;
      - yellow cards. Red cards are too rare per player and are left to discipline (5c,
        §8.4).
      A rate is judged given the realised appearance (minutes, starter or substitute,
      line, slot code). This measures it apart from selection and minutes (P3-b); P3-d
      and P3-e combine the two. The data is the ten bundle leagues, league matches only.
      Dev seasons are 2019-2023 and warm-up 2016-2018; no holdout season is loaded.
      Nothing reaches production. Outputs are per player and stay internal (R2 only),
      under-18 players included (CLAUDE.md rule 10).

      (a) **Counts (data).** From the stored appearances (P3-0), 0 requests.
      - **Exposure** is event minutes / 90, for appearances with minutes > 0.
      - **Nulls.** A statistic is collected in a team-match when at least one of its
        players has a value. Assists and yellow cards can be zero for a whole team, so
        for them it is enough that the team-match has a statistics block.
        - Where the statistic is collected, a null counts as 0.
        - Where it is not, the appearance drops out of that statistic and is never
          filled.
      - **Checks,** per dev league-season and statistic:
        - player goals plus the opponent's own goals equal the team's score in at
          least 99.5% of team-matches;
        - provider assists agree with the goal events' assist field, and provider
          yellows with card events, for at least 95% of appearances;
        - summed player shots agree with the team's shots (`team_match_stat`) within 1
          in at least 95% of team-matches where both exist.
        A statistic that fails in a league-season drops out there and is reported.

      (b) **Model.** For player i in appearance m and statistic s:
          E[y] = e_m · μ[s, line] · ℓ[s, league] · ρ_m · θ[i, s],
          θ[i, s] ~ Gamma(k[s, line], k[s, line])   (mean 1).
      - **Terms:**
        - line is the provider's G/D/M/F code for the appearance, or the player's most
          common one where it is missing;
        - ℓ is per bundle league;
        - ρ_m holds the appearance multipliers: a substitute appearance; the first 5
          appearances for a new club (no appearance for it in the previous 365 days);
          and the first 3 after a return. A return means the player was out of the
          matchday squad for at least 3 consecutive league matches of the club. This is
          a proxy: the reason for an absence isn't known in dev (D39).
      - **Fitting.** Counts are Poisson within a player, and θ is marginalised in
        closed form (the Poisson-Gamma marginal on per-appearance counts). So only the
        hyperparameters μ, ℓ, ρ and k are sampled, with NumPyro. Each player's posterior
        is Gamma(k + Y_i, k + Λ_i), where Y_i is their count and Λ_i their expected count
        at θ = 1 in the window.
      - **One fit across the ten leagues,** so a player who moves between them keeps
        their history. Players arriving from elsewhere start at θ = 1 (the line × league
        rate) and are flagged; there is no market-value prior (register 5.3).
      - **Goalkeepers** get a player term for yellow cards only. For the other five
        statistics they get the GK line rate: their counts are almost all zero, so a
        spread between players is not identified.
      - **Walk-forward.** The hyperparameters are refitted every 28 days on the 730
        days of appearances final by the refit time (as P3-b and P3-c). Each player's
        posterior and the ramps are updated at kickoff - 24 h, in closed form with the
        hyperparameters fixed, from appearances final by then.
      - **Dispersion test** (§7.4), per line × statistic, fixed on warm-up:
        - fit on 2016-2017 and score on 2018: Poisson within a player against negative
          binomial (a per-appearance dispersion φ[s, line] on top of θ, fitted by
          maximum likelihood with θ at its posterior mean);
        - NB is chosen where its paired log-loss gain has a 95% interval excluding zero,
          Poisson otherwise;
        - the choice is then fixed for dev, and φ is re-estimated at each refit.

      (c) **Baselines,** refitted with the model on the same window:
      - **R0:** the line × league per-90 rate, with no player term. Poisson.
      - **R1:** the player's own per-90 rate, shrunk towards R0 by a fixed
        pseudo-exposure c[s] (in 90s). c is chosen on warm-up (fit 2016-2017, score
        2018). Poisson.

      (d) **Slot codes** (§7.3), in positional league-seasons (2022-2023):
      - an appearance multiplier per slot code relative to its line, pooled code → line
        (log multiplier ~ Normal(0, τ[s]));
      - compared with the line-only model on those appearances, and adopted for
        positional seasons if its summed log loss is better with the paired 95% interval
        excluding zero. Otherwise rates stay at line level;
      - reported with the codes whose rates move most (for example RB against RWB on
        key passes and assists);
      - not part of the pass rule, except that its refits must pass the inference gates.

      (e) **Metrics:**
      - per-appearance log loss of the realised count given the realised appearance,
        paired by appearance and summed over the six statistics for the overall
        comparison;
      - E/O: the sum of predicted means over the realised sum;
      - ECE of P(count ≥ 1).

      (f) **Pass.** All of the following must hold:
      1. The (a) checks pass in every dev league-season, or the failing statistic drops
         out there and is reported.
      2. Every statistic beats R0, with the paired 95% interval excluding zero.
      3. The six-statistic sum beats R1, with the paired 95% interval excluding zero.
         No statistic and no bundle league is worse than R1 by more than 1.0% (point
         estimate, as D28).
      4. For every statistic, E/O is between 0.95 and 1.05 overall, and the ECE of
         P(count ≥ 1) is at most 0.02.
      5. Inference gates (R-hat, ESS, divergences) pass in every refit. The PPC (share of
         zero counts and variance-to-mean per line × statistic) is a reported warning,
         as in P3-b amendment 8.
      6. Leakage: nothing final after the as-of time changes any rate input (100 sampled
         appearances, as P3-b).

      **Reported, not gating:**
      - the dispersion table;
      - sparse players (under 900 minutes in the window), players new to the ten
        leagues, new signings and returners;
      - results by line, league and season;
      - the ramp and substitute multipliers;
      - for P3-e, summed player expected goals against team goals per team-match.

      **Cost:** 0 API requests. One CI walk-forward run (one backtest-type run at a
      time).
- [ ] **P3-d Lineup offset: in the likelihood vs serving-only (§7.5).** Both variants
      against the production 1b. Dev walk-forward at the lineups-known horizon, then the
      earlier horizons with P3-b's projected XI. Gate in the D18 form (1X2 log loss and
      RPS, paired 95% interval excluding 0, no league x market segment worse than 1.0%
      per D28, §14.5 inference gates, feasibility). σ_walk is reported for both variants.
      Promotion window per D38. Acceptance stated in full before building.
- [ ] **P3-e Feedback loop into Layer 6 (§7.5 steps 1-4).** Player goal shares sum to the
      team's non-own-goal rate by construction; own goals stay outside the allocation.

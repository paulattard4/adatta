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
- **D40 (resolved 2026-11-19, owner):** P3-d is closed as failed on dev, with no
  respecification. The honest offset signal is small (β about 0.15), so variants tried on
  the same dev seasons would be fitting to them. P stays production, the offset is not
  served, and under P3-d (f) the feedback loop is not wired into production. What P3-e
  and the rest of Phase 3 become without a passed offset is open.

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
- [x] **P3-d0 Player rates, Layer 5 (§7.4).** Per-90 rates pooled player → position →
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

      *Amended 2026-11-18 while building, before any rate result on dev was seen; owner:
      "Shots on target":*
      1. **Shots are shots on target.** The first table build failed the (a) shots check
         in every bundle league from 2020/21 (15-39% of team-matches within 1, against
         96-100% in 2019). The provider changed its per-player `shots.total`:
         - up to 2019 it includes blocked shots and equals the team's Total Shots;
         - from 2020 it leaves them out and equals Shots on Goal + Shots off Goal,
           about 3.3 fewer per team-match.
         Per-player shots on target equal the team's Shots on Goal in 99.3-100% of
         team-matches in every season checked (2018-2022). As stated, total shots would
         have dropped out of 2020-2023 and been judged on 2019 only.
         - The statistic is shots on target (`shots.on`), and the (a) check compares the
           players' sum with the team's Shots on Goal.
         - **Null rule:** a team with no shots on target leaves every player null, so a
           null counts as 0 for a player with a statistics block in a team-match whose
           shots were collected (any player has a shots value).
         - The other five statistics are unchanged.

      **Build (2026-11-18), before any dev run:**
      - **Code:**
        - `adatta/features/rates.py`: the counts table and the (a) checks;
        - `adatta/eval/rates.py`: design, as-of history, predictive, baselines, the
          dispersion choice, the pass rule, leakage and the report;
        - `adatta/model/rates.py`: the NumPyro fit, warm-up, walk-forward and CLI;
        - `api_football_stats.event_counts` (assists and yellows from the events, for
          the check).
      - **CI:** `.github/workflows/rates.yml`, with a warm-up job, then one job per dev
        season, then assemble.
      - **Tests:** `tests/test_rates.py` (10), and `tests/test_rates_model.py` in the CI
        model job.
      - **Counts table `8254f5e405c0eb4d`** (R2 only): 895,287 played appearances in
        the ten bundle leagues 2016-2023, 572,899 of them in dev. 4,182 appearances
        with no line, now or earlier, were dropped. There are 16 slot codes.
      - **(a) checks on dev**, 50 league-seasons:
        - shots on target vs the team: at least 98.8% everywhere;
        - assists vs the events: at least 99.5%;
        - yellows vs the events: at least 99.6%;
        - goals vs the score: fails only in Serie A, Serie B and La Liga 2019 (98.2%,
          99.4%, 99.2%). About 1 team-match in 50 there has one more goal event than
          the score (probably goals cancelled later), so goals drop out in those three
          league-seasons.
      - **Coverage:** tackles are missing in most leagues' 2016 and part of 2017
        warm-up, and in 5% of Serie B 2019; those rows drop out of tackles only.
      - **Leakage:** 0 of 100. The first build reported 82 changed, an artefact of the
        check running after the drops; it now runs before them.

      *Amended 2026-11-18 after the dev run (run 44251237152), whose scores had been
      seen; owner: "Yes, rerun at 2,000":*
      2. **Longer sampling.** The run failed only (f)5: 22 of 534 fits had R-hat
         1.010-1.018 (limit 1.01), 21 of them slot-variant fits in 2022-2023 (mostly
         goals and shots on target) and one line-only fit (goals, refit 2020-05-25, R-hat
         1.010), with ESS 395 or more and no divergences. As P3-c amendment 2, the whole
         dev run is repeated with 2,000 draws per chain after 2,000 tuning steps (was
         1,000/1,000), the workflow's `draws` input, reusing the same warm-up choices
         (run 44248965961). Each season is split into three parts instead of two so the
         longest job stays inside the 6-hour limit; parts only interleave the refits, each
         with the same fixed seed. Nothing else changes. If fits still fail, the model is
         respecified on dev rather than sampled longer.

      **Dev run 2026-11-18 (run 44251237152, 10 jobs, 534 fits, median fit
      59 s, longest job 156 min):** verdict fail on (f)5 only. Every other check passed:
      - every statistic beats R0 (95% intervals exclude 0): goals -2.30%, assists -2.08%,
        shots on target -3.39%, key passes -6.96%, tackles -4.25%, yellows -1.48%;
      - the six-statistic sum beats R1, -0.52% (paired difference in summed log loss per
        appearance [-0.0196, -0.0181]); by statistic -0.84% (tackles) to +0.15% (goals), by league
        -0.46% to -0.63%;
      - E/O 0.959 (tackles) to 1.021 (goals); ECE of P(≥1) at most 0.012 (key passes);
      - (a) checks pass with goals dropped in 135/136/140 2019; leakage 0 of 100;
      - slot codes in 2022-2023: sum -0.22% against line-only, interval excluding 0, so
        adopted subject to their fits passing the gates;
      - dispersion (warm-up): NB for key passes and tackles on D, M and F, Poisson
        elsewhere; R1's c in 90s: goals 20, assists 50, shots on target 10, key passes 5,
        tackles 10, yellows 50;
      - reported: by line vs R1 goalkeepers -3.94%, D -0.62%, F -0.48%, M -0.42%; sparse
        players -0.96%, new to the ten leagues -1.47%, new signings -0.96%, returners
        -0.98%, substitute appearances -1.76%; multipliers b_sub +0.28 to +0.60, b_new
        and b_ret -0.08 to +0.05 on the log scale;
      - for P3-e: summed player expected goals per team-match E/O 1.021, correlation
        with team goals 0.160 (36,484 team-matches);
      - **PPC warning:** 97-100% of refits outside the band for every statistic. With
        about 500k appearances per window, a small misfit in the zero share or
        variance-to-mean puts the observed value outside the predictive band; reported
        as a limitation, not gating ((f)5).

      **Result 2026-11-18: pass** (run 44289602612, amendment 2: the whole dev run at
      2,000 draws, 15 jobs, median fit 112 s, longest 747 s, about 3 h 50 min). All 534
      fits pass the inference gates and every check of (f) passes. Scores are essentially
      unchanged from the first run: the six-statistic sum vs R1 -0.52% (paired difference
      [-0.0196, -0.0181]), every statistic vs R0 -1.48% to -6.96%, goals vs R1 +0.15%
      (within 1.0%), E/O 0.959-1.020, ECE at most 0.012, slot codes -0.22% against
      line-only and adopted for 2022-2023. The PPC warning stands (98-100% of refits
      outside the band). Nothing reaches production: the rates, the dispersion choices,
      R1's c and the slot codes feed P3-d and P3-e.
- [ ] **P3-d Lineup offset: in the likelihood vs serving-only (§7.5).** Both variants
      against the production 1b. Dev walk-forward at the lineups-known horizon, then the
      earlier horizons with P3-b's projected XI. Gate in the D18 form (1X2 log loss and
      RPS, paired 95% interval excluding 0, no league x market segment worse than 1.0%
      per D28, §14.5 inference gates, feasibility). σ_walk is reported for both variants.
      Promotion window per D38. Acceptance stated in full before building.
      Acceptance (stated 2026-11-18, before building; no offset or P3-d score has been
      computed. Seen so far: P3-d0's dev result, including summed player expected goals
      per team-match against team goals, correlation 0.160):

      **Scope.** Three team models on the same weekly walk-forward grid (D11, D14 final
      runs), dev seasons 2019-2023, the ten bundle leagues, league matches only:
      - **P**, the production `phase1b_strength_open_xg`, unchanged;
      - **S**, serving-only: P's fits, with the offset added at prediction only;
      - **L**, offset in the likelihood (§7.5, adopted design): P refitted with the
        historical offset in every training match, and the offset at prediction.
      Holdout seasons are not loaded. Nothing reaches production; wiring is P3-e, after
      the live verdict below.

      (a) **Player quality** comes from P3-d0 as passed (line, league, multipliers, slot
      codes in 2022-2023, θ in closed form). A player's quality is their posterior-mean
      expected goals per 90 at their line (own goals excluded, §7.4). It is taken as of
      the match's kickoff - 24 h, from the latest P3-d0 hyperparameter refit before then,
      for training matches as well as targets. Quality as of the refit time would credit
      a player's past games to his later-estimated θ, which is circular. Training matches
      before the first P3-d0 refit (2018-07-01) use that refit's hyperparameters with
      player posteriors as of the match. This is disclosed: population-level parameters
      only, all final before every dev target.

      (b) **Offset.** For team T in match m:
      - **Lineup attack index** A = Σ over outfield players of (minutes / 90) x quality.
        Goalkeepers are left out (their goal rate is the GK line rate).
      - **Baseline** Ā is the mean of A over T's previous 10 league matches, with their
        realised lineups and minutes and the same as-of quality. This is the "typical
        lineup" the walk tracks (§7.5).
      - **Δ = log(A / Ā)**, and log λ_T += β x Δ_T. β ~ Normal(1, 0.5): a lineup with
        10% more expected goals than usual scores about 10% more at β = 1.
      - **Minutes:**
        - in training matches, ~~the realised minutes~~ the lineups-known construction
          below (amendment 8);
        - at the lineups-known horizon, the announced XI and bench, with P3-b's minutes
          model given the role;
        - at T-24h, P3-b's start and bench probabilities times those minutes.
      - **Where inputs are missing** (no lineup, fewer than 3 earlier matches for Ā, no
        player rows), Δ = 0 and the match is flagged. The share is reported per
        league-season (rule 8: no imputed offset).
      - **Defence:** none. The offset is attack-only; see the spec point below (owner,
        2026-11-18).

      (c) **Fits.**
      - **L:** β enters the NUTS fit with the other 1b parameters (2,000 draws, seed
        20261105), so the walk is fitted net of lineup changes.
      - **S:** uses P's refit draws, cached in R2 by fit hash where present. Under the
        current hash, R2 holds only 109-118 refits per bundle, mostly after dev, so the
        run refits P where its draws are missing. P's draws are needed anyway to score P.
        β_S is fitted per refit by maximum likelihood on the training matches' goals, with
        P's posterior-mean log λ as an offset. S then adds β_S x Δ to each draw at
        prediction.
      - The §14.5 inference gates apply to every L refit. σ_walk is reported per refit for
        P and L (expected lower in L, not gating), and β and β_S over time.

      (d) **Metrics.** As the 1b harness, warm matches:
      - 1X2 log loss and RPS, paired by match;
      - the D28 segment metrics per league (OU2.5 and BTTS log loss, total CRPS);
      - ECE of each 1X2 outcome.
      Reported:
      - at both horizons, overall and by league and season;
      - for matches with a big lineup change (|Δ| above its 90th percentile), where the
        offset should matter most.

      (e) **Pass on dev.** All of the following must hold:
      1. At the lineups-known horizon, L beats P in the D18 form: 1X2 log loss and RPS
         each better, with the paired 95% interval excluding 0. No league x market segment
         is worse than 1.0% (D28, point estimate).
      2. At the lineups-known horizon, L's point estimates beat S on both 1X2 log loss and
         RPS (§7.5: the in-likelihood design must beat serving-only). The interval is
         reported.
      3. At T-24h, L is no worse than P on 1X2 log loss and RPS (point estimates ≤ 0), with
         the intervals reported.
      4. The inference gates pass in every L refit, with the PPC a warning as in Phase 1b.
         L runs within the production budget (median refit no more than 1.5x P's on the
         same runner class).
      5. Leakage: for 100 sampled dev targets, nothing final after the as-of time changes
         Δ, A, Ā or any quality input.
      If S passes 1 and L doesn't, S is not adopted silently: it is reported and raised,
      since the spec adopts L. A fail is reported with its segment. Respecification is on
      dev only and disclosed.

      (f) **Promotion (D38, pre-registered here, judged later).**
      - L, S and P are scored on arrival on live 2026/27 bundle league matches, from the
        day the live shadow starts. The XI comes from `lineup_snapshot` (as-of), and
        status from `player_status_snapshot` where present.
      - The same criteria as (e)1 and (e)2 at the lineups-known horizon, judged once, after
        the last matchday of the 2026/27 bundle league seasons.
      - The feedback loop is not wired into production (P3-e's production step) before a
        pass.

      **Cost:**
      - 0 API requests.
      - One CI walk-forward that fits L, and P where uncached. A Phase 2 weekly run was
        about 50 jobs at about 2.5 h each, and this is up to about twice that: roughly 100
        jobs in parts, five or six waves of 18, about 12-15 h wall time. It runs after a
        timing pilot on one bundle-season part.
      - S adds no NUTS: it is rescoring plus a one-parameter fit per refit.
      - One backtest-type run at a time.

      **Spec point raised (defence).** §7.5 builds Δ_lineup from Layer 5 rate quality.
      Of P3-d0's six statistics, only goals (and assists, shots on target) measure
      attacking quality, and none measures defending. Tackles are a volume statistic, not
      a quality one. The options:
      - **Attack-only (as drafted).** The spec's letter. Defensive absences still leak into
        the walk, and that is flagged.
      - **A defensive proxy now.** Lineup continuity or with-without-you impact, register
        B.1 and B.2, which are now §20.2 backlog. This pulls a backlog item forward and
        adds a non-rate input.
      - **Defence later.** A defensive-impact term as a later challenger, after P3-d.

      Resolved 2026-11-18 by the owner, "Attack-only, defence later": the offset is
      attack-only, and defensive absences that still reach the walk are flagged in the
      confidence profile. A defensive-impact term (B.1 or B.2) is a later challenger
      against the passed P3-d model, with its own acceptance test.

      *Amended 2026-11-18 while building, before any offset or P3-d score was computed:*
      1. **Line-level quality.** Player quality uses P3-d0's line-level goals model, with
         no slot codes. The reasons:
         - T-24h has no slot, and one definition keeps Δ comparable across horizons;
         - goals gained only 0.35% from slot codes in P3-d0;
         - the slot variant would double the quality refits.
      2. **P3-b export rerun.** P3-b's stored dev outputs hold p_start and the scores
         only; there is no bench probability and no minutes given the role. P3-b's dev
         run is therefore repeated with nothing changed: model, table, seeds, 500 draws,
         and its 11 refits at 2,000 draws as in amendment 9. Its model rows add three
         exported columns:
         - `p_bench`;
         - `emin_start`, the expected minutes as a starter (90 with q_full, else the start
           Beta);
         - `emin_bench`, the expected minutes from the bench (q_on times the on Beta).

         Its verdict must reproduce P3-b's (a check that nothing changed). These columns
         give the lk and T-24h minutes of (b).
      3. **Announced players outside P3-b's pool** (newcomers) have no P3-b row. Their
         expected minutes are the mean exported minutes for their line and role, over
         P3-b rows of team-matches that kicked off strictly earlier, so nothing later
         leaks in. With none earlier they add nothing. They are counted per team-match and
         reported.
      4. **S's training rates.** P's posterior-mean λ per training match is the mean of
         its 8,000 posterior-predictive goal draws. The fitter keeps them as
         `train_lam_home` and `train_lam_away`.
      5. **Quality refits** use 2,000 draws per chain, as P3-d0 amendment 2. They are the
         warm-up refit plus P3-d0's 28-day dev grid, goals only.
      6. **P is refitted for every dev refit.** The L term edits `team_strength.py`,
         which is part of the fit hash, so no cached P draws apply. The production model
         is unchanged: no variable is added without `lineup`
         (`tests/test_lineup_model.py`).
      7. **Jobs.** Each bundle-season runs in 4 parts, so 100 fit jobs. Every refit fits P
         and L.
      8. **Training rows use Δ_lk, not Δ_real** (owner, 2026-11-19, after the timing
         pilot; see Runs).
         - **Why:** realised minutes follow the score through substitutions, so Δ_real
           carries the outcome and gives a negative β.
         - **What changes:**
           - training matches read the announced XI and bench with P3-b's role minutes,
             the same construction served at lk;
           - Δ_real stays in the table, for reporting only;
           - β_S is fitted on the same Δ_lk;
           - L's cache key carries the training version (`<table>:lk`), so the pilot's
             L fits are not reused. P is unchanged and its pilot fits are reused.
         - **Coverage:** Δ_lk needs P3-b's export, which starts with 2019/20. Training
           rows before then have Δ = 0 (rule 8), so β in the early 2019 refits rests
           mostly on the prior, and the per-season β is reported.
         - The quality refits and offset table 6c35e11357c8313d are unchanged and reused.
      9. **L refits that fail the gates are redone once** (owner, 2026-11-19, written
         while dev run 44467720850 was still running, before assemble and before any
         P3-d score was seen).
         - **Seen so far, mid-run:** 9 of 462 finished L refits fail the gates. Most
           have a single divergence, mostly in Spain 2022, where σ_walk is about 0.007.
         - **Rule:**
           - every L refit of the dev run that fails (e)4's gates (R-hat ≤ 1.01, ESS ≥
             400, no divergences) is refitted once with `target_accept` 0.99 (was 0.95)
             and 4,000 draws per chain (was 2,000);
           - the same data, offset table, seed and everything else;
           - one setting for every redone refit, fixed now, not chosen per failure;
           - P is not refitted: its cached draws are reused, so P and S score the same
             as before.
         - **Merge:** the redone refits' rows and diagnostics replace the base run's,
           as P3-b amendment 9 did. The verdict is assembled over the base run with
           them swapped in.
         - **If a redone refit still fails, (e)4 fails.** There is no third attempt and
           no other change.
         - **Which refits:** the list is taken mechanically from the base run's stored
           diagnostics (`lineup failing`), not picked by hand.
         - **Timing gate:** L's fit time in (e)4 is the base run's median. The redone
           refits are reported, not used in the timing gate.

      **Build (2026-11-18), before any run:**
      - **Code:**
        - `adatta/features/lineup_offset.py`: squads, the previous-10 baseline,
          quality as of kickoff - 24 h, A, Ā and Δ for real, lk and T-24h, the table
          store and `attach`;
        - `adatta/eval/lineup.py`: β_S, the pass rule (e) and the report;
        - `adatta/model/lineup.py`: quality refits, the table build with its leakage
          check, the P/L/S runner and assemble;
        - `team_strength`: `lineup` (β_lineup ~ Normal(1, 0.5) on lu_home and lu_away);
        - `backtest`: `RunSpec.lineup` and `lineup_table` (model tag `_lu`), the
          fitter's training means, and `score_targets(extra_offset=)`;
        - `model.selection.role_minutes`, the export of amendment 2.
      - **CI:** `.github/workflows/lineup.yml`, run in three stages: quality, table, fit
        (with assemble).
      - **Tests:**
        - `tests/test_lineup_offset.py` (10);
        - `tests/test_lineup_model.py` and a `role_minutes` check in
          `tests/test_selection_model.py`, both in the CI model job.
      - **Order of runs,** one at a time:
        1. the P3-b export rerun (`selection.yml`, about 6 h), then its 11 refits
           (`selection_rerun.yml`, about 2.5 h);
        2. quality (about 1-2 h);
        3. table;
        4. a timing pilot (one bundle-season part);
        5. the dev run (about 12-15 h).

      **Runs:**
      - **P3-b export rerun,** run 44339699090 (d422509, table 061ebfdb66114294,
        500 draws), success. Its verdict matches P3-b's first run (44033304307): every
        check is the same apart from (g)5, and the metrics agree to about 0.001. For
        example, log loss vs B2 is -2.7305 here and -2.7321 in the first run, and
        minutes CRPS vs B1 is -70.640 and -70.642. Sampling is not bit-for-bit
        reproducible across runners, so the refits that fail the gates are not the same
        as the first run's 11.
        - 13 of the 283 refits fail the gates here, all at R-hat 1.010-1.011, with ESS
          668 or more and no divergences. Only 6 of them are among the first run's 11.
        - Following amendment 9's rule (redo the refits that fail the gates, at 2,000
          draws), the rerun redoes these 13 in 11 jobs: run 44406512773.
        - A first rerun dispatched on the old list of 11 (44405614135) was cancelled
          within minutes.
        - **Rerun 44406512773: verdict pass.** All 283 refits now pass the gates.
          Against B2, log loss is -2.7311 [-2.8009, -2.6613] and minutes CRPS is
          -70.644. Leakage passes. This run's report is the export that the offset table
          reads.
      - **Quality:** run 44431797480. All 64 refits pass the gates, with R-hat 1.006
        or less and fits of 60-115 s.
      - **Table:** run 44436106852, offset table 6c35e11357c8313d, 62,498
        team-matches. Leakage: 0 of 100 changed.
      - **Timing pilot:** run 44437167996, England 2022 part 0/4, 3 h 18 min for 10
        refits. All P and L refits pass the gates. Median fit is P 556 s and L 592 s
        (1.06×).
        - **Problem: β is negative.** β_lineup is -0.10 to -0.22 and β_S is -0.14 to
          -0.26, against a prior centred on 1.
        - **Diagnosis** (dev 2019-2023, all bundles, the Poisson slope of a team's goals
          on Δ with the team-season mean as offset):

          | Δ | slope | se |
          |---|---|---|
          | real | -0.141 | 0.037 |
          | lk | +0.132 | 0.037 |
          | 24 | +0.080 | 0.041 |

        - **Cause.** Realised minutes depend on the score. A team that is behind brings
          on attackers and a team that is ahead brings on defenders, so Δ_real − Δ_lk
          correlates -0.10 with goal difference. Training on Δ_real, as (b) specifies,
          lets the outcome leak into the covariate through substitutions. L and S
          therefore learn a negative β, which then gets applied to the pre-kickoff Δ_lk
          and Δ_24 when serving. The fault is in the spec, not the code.
        - The dev run was held. The owner chose to train on Δ_lk (amendment 8).
        - Cold-start rows are 21,674 here and 22,540 in the first run. The data captured
          since then is the likely cause. This is recorded, not investigated.
      - **Dev run** 44467720850 (53d091b, table 6c35e11357c8313d, 100 jobs, 928 weekly
        refits per model, fits 64-248 min per job), finished 2026-11-19 20:21 UTC.
        Verdict fail on (e)1, (e)2, (e)3 and (e)4's gates: 9 of 928 L refits failed,
        each at R-hat 1.010-1.012 or with 1 divergence; 7 in Spain 2022, 1 in England
        2023, 1 in Italy 2019.
      - **Redo** 44608051780 (amendment 9, 5 jobs, assembled over the dev run): all 9
        redone L refits pass, so (e)4 passes. The other checks are unchanged.

      **Result 2026-11-19: fail** on (e)1, (e)2 and (e)3. 18,682 warm matches;
      relative, negative = first model better:

      | horizon | comparison | 1X2 log loss | 95% interval | RPS | 95% interval |
      |---|---|---|---|---|---|
      | lk | L vs P | +0.01% | [-0.03%, +0.05%] | +0.02% | [-0.04%, +0.08%] |
      | lk | S vs P | +0.01% | [-0.02%, +0.03%] | +0.02% | [-0.02%, +0.05%] |
      | lk | L vs S | -0.00% | [-0.03%, +0.03%] | +0.00% | [-0.05%, +0.06%] |
      | 24 | L vs P | +0.05% | [+0.01%, +0.08%] | +0.08% | [+0.03%, +0.13%] |
      | 24 | S vs P | +0.02% | [+0.00%, +0.04%] | +0.04% | [+0.01%, +0.07%] |
      | 24 | L vs S | +0.02% | [-0.00%, +0.05%] | +0.04% | [-0.00%, +0.08%] |

      - (e)1 fail: L does not beat P at lineups-known; both intervals include 0. No
        league x market segment is worse than 1.0% (the worst is +0.16%, league 79 total
        CRPS), so the segment part holds.
      - (e)2 fail: L ties S (log loss -0.00%, RPS +0.00%).
      - (e)3 fail: L is worse than P at T-24h, the intervals clear of 0.
      - (e)4 pass after the redo: every L refit passes the gates (worst R-hat 1.010,
        min ESS 508, 0 divergences). L's median fit is 449 s vs P's 438 s (1.03x).
      - (e)5 pass: leakage 0 of 100.
      - S does not pass (e)1 either, so the S-without-L case of (e) does not arise.
      - By league at lk, L vs P log loss runs from -0.05% (62) to +0.07% (135); by
        season from -0.03% (2023) to +0.04% (2019).
      - Big lineup changes (top 10% |Δ|, 1,869 matches): at lk L vs P is +0.02%
        [-0.25%, +0.29%]; at T-24h L vs P is +0.30% [+0.08%, +0.51%] and L vs S
        +0.24% [+0.03%, +0.44%]. The T-24h loss comes from the matches where the
        projected XI moves the offset most.
      - β_lineup is far below the prior's centre of 1. Median per season, with the range
        over refits:

        | season | β_L median | range | refits | β_S median |
        |---|---|---|---|---|
        | 2019 | 0.27 | -0.06 to 1.08 | 178 | 0.05 |
        | 2020 | 0.15 | -0.08 to 0.35 | 177 | 0.08 |
        | 2021 | 0.08 | -0.03 to 0.25 | 189 | 0.01 |
        | 2022 | 0.12 | -0.13 to 0.29 | 194 | 0.04 |
        | 2023 | 0.14 | -0.04 to 0.33 | 190 | 0.03 |

        2019 is highest because its early refits have few Δ_lk rows and rest more on the
        prior (amendment 8). σ_walk is the same in P and L (mean 0.0163).
      - P itself had 7 of 928 refits failing the gates (worst R-hat 1.016, 1
        divergence). P is production and not judged here; this is recorded.
      - **Consequence:** P stays production. Under (f) the feedback loop is not wired
        into production. Any respecification is on dev only and disclosed, and is the
        owner's call.

      **Closed 2026-11-19 as failed (owner, D40), no respecification.** Not ticked: its
      acceptance check did not pass. The live shadow in (f) is not started.
- [ ] **P3-e Feedback loop into Layer 6 (§7.5 steps 1-4).** Player goal shares sum to the
      team's non-own-goal rate by construction; own goals stay outside the allocation.
      *On hold 2026-11-19 (D40):* P3-d failed, so there is no passed offset to wire. Its
      scope is to be restated before any build.

      **Restated 2026-11-19 (owner: "Restate P3-e as scorers"): goalscorer and assist
      forecasts allocated from P (§7.5 steps 3-4, without steps 1-2).** P's team rate is
      split among the matchday squad as normalised shares, so player expectations sum to
      the team's non-own-goal rate by construction. Nothing goes back into Layer 6: P is
      unchanged, and no lineup offset enters it (D40).

      Acceptance (stated 2026-11-19 before building, owner: "Approve as drafted"; no
      P3-e score has been computed. Seen so far: P3-d0's dev result, including goals vs R1 +0.15% and summed
      player expected goals per team-match E/O 1.021, correlation 0.160 with team goals;
      P3-d's dev result):

      **Scope.**
      - Two statistics: goals (own goals excluded, §7.4) and assists. Shots on target,
        cards and the rest wait.
      - The ten bundle leagues, league matches only, dev seasons 2019-2023. 2019 is the
        first with P3-b's export, which gives the lineups-known minutes. No holdout season
        is loaded.
      - Every player in the announced matchday squad (XI and bench), goalkeepers
        included. A named player who doesn't come on scores 0. Bookmakers void that bet,
        so the conditional view is a later serving matter and is not scored here: it
        would condition on substitutions, which follow the score (P3-d amendment 8).
      - Outputs are per player and stay internal (R2 only). No betting-facing output, and
        under-18 players are flagged (CLAUDE.md rule 10).

      (a) **Inputs, all as of the target's own as-of times, nothing new fitted for P:**
      - **Team goals:** P's weekly refits (`phase1b_strength_open_xg`) from the fit
        cache, the same draws as P3-d's P. For each draw, the Dixon-Coles score matrix
        (rho, kappa) gives each side's goal distribution, averaged over draws.
      - **Own goals:** each goal is an own goal with probability o[league], the league's
        own-goal share over the 730 days final by the as-of time.
      - **Assists:** each non-own goal carries an assist with probability α[league], the
        league's assisted share over the same 730 days.
      - **Player weights:** w_i = e_i · q_i.
        - e_i is the expected minutes / 90 given the announced role, from P3-b's export
          (`role_minutes`, with the P3-d `role_fallback` for players outside its pool),
          as P3-d's Δ_lk.
        - q_i is P3-d0's posterior-mean per-90 rate at the player's line, in the match's
          league, as of kickoff - 24 h. It comes from the latest P3-d0 refit at or
          before then, with the substitute multiplier on a bench player's rate.
        - Line level, as P3-d amendment 1.
        - Goals reuse P3-d's quality refits (run 44431797480). Assists need the same
          quality stage for assists: about 64 refits, 1-2 h, one CI run.
      - **Shares:** s_i = w_i / Σ_j w_j over the team's matchday squad.

      (b) **Model M.** For a side whose goal count is N under P:
      - each goal is non-own with probability 1 - o and goes to player i with share s_i;
      - so P(i scores 0) = E_N[(1 - (1 - o) s_i)^N], and P(i assists 0) =
        E_N[(1 - (1 - o) α s^A_i)^N], with s^A from the assist rates;
      - and E[goals_i] = (1 - o) s_i E[N]. Summed over the squad this is the non-own
        part of P's team rate exactly; a test checks this to 1e-9.
      Nothing in M is fitted on dev outcomes except o and α, which are counted in closed
      form as of each target. So there are no inference gates beyond those P and P3-d0
      already passed.

      (c) **Baselines:**
      - **B0, rates alone:** e_i · q_i as a Poisson mean, with no team or opponent
        context. This is P3-d0 used directly.
      - **B_line, shares by line:** M with q_i replaced by the line x league rate (P3-d0's
        R0), so shares come from minutes and line only.
      - **B_R1, shares by R1:** M with q_i replaced by P3-d0's R1 (the player's own
        per-90 rate shrunk by c[s], with goals 20 and assists 50).

      (d) **Metrics,** per player row, paired by row:
      - **Primary:** log loss of "at least one" (anytime scorer, anytime assist).
      - Log loss of the count (0, 1, 2, 3+).
      - E/O of the summed expectation.
      - ECE of P(at least one).
      Reported:
      - by league, line, season, starter or bench, and P(at least one) decile;
      - P(2+ goals), against its rate;
      - penalty goals' share of the misfit, if any (penalty takers are §8.3, Phase 3b);
      - at T-24h with P3-b's projected squad (start and bench probabilities times those
        minutes), not gating, as the horizon where P3-d failed.

      (e) **Pass on dev,** lineups-known, for goals and for assists each:
      1. M beats B0 on log loss of "at least one", with the paired 95% interval
         excluding 0.
      2. M beats B_line the same way.
      3. M is no worse than B_R1 by more than 1.0% (point estimate), overall and in
         every bundle league. This is P3-d0's tolerance against R1: P3-d0 goals were
         +0.15% against R1, so M is not required to beat it.
      4. E/O is between 0.95 and 1.05 overall, and the ECE of P(at least one) is at most
         0.02 overall and at most 0.03 in each league.
      5. Consistency: summed player expectations plus the own-goal expectation equal P's
         team expectation in every team-match, to 1e-9.
      6. Leakage: for 100 sampled player rows, nothing final after each input's as-of
         time changes e, q, o, α or P's draws.
      A statistic that fails is reported with its segment. Respecification is on dev
      only and disclosed. If goals pass and assists don't, goals pass alone.

      (f) **Live (pre-registered, judged later).** On a pass, M is scored on arrival on
      live 2026/27 bundle league matches at lineups-known, with the same (e)1-4 against
      the baselines. Bookmaker anytime-scorer prices, where the kept raw odds snapshots
      hold them, are used for Edge and CLV only. They are never a feature (principle 1)
      and never part of the pass. Nothing is served to users before the live verdict.

      **Cost:** 0 API requests. One CI quality run for assists, then one scoring run:
      P is read from the fit cache, with no refits. The cache stays valid while
      `team_strength.py`, `covariates.py`, `config/evaluation.yml` and `uv.lock` are
      unchanged since 53d091b. Each is one backtest-type run, one at a time.

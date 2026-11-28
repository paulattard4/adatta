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
decomposition, discipline, half split; `docs/PHASE3B_TASKS.md`) and 3d (knockouts, now
Stage B) follow later.

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
- **D42 (resolved 2026-11-20, owner: "Close as failed"):** P3-g is closed as failed on dev,
  in both forms: picking the XI slot by slot (XI overlap -0.109 vs B_top) and arranging
  B_top (slot accuracy -0.030 vs B_last). Both lose to the team's last lineup in the
  projected formation, so a third design on the same dev seasons would be fitting to
  them. The panel keeps B_top with the keeper fix; no formation or slots are shown.
  P3-c's formation model passed on its own and stays unserved.
  *Amended 2026-11-20 (owner: "so youre saying we cant have a predicted formation with
  positions? thats very poor"):* D42 closes P3-g's two placement models only. A formation
  view built from what did pass (P3-c, and the last lineup in the projected formation,
  the stronger baseline) is P3-h.
- **D43 (resolved 2026-11-21, owner: "Live shadow, attack + defence", acceptance approved
  as drafted):** the lineup offset comes back as one pre-registered challenger, P3-i, judged
  only on live 2026/27 matches. Its attack half is P3-d's, and it adds a defensive half,
  the "defence later" challenger from the P3-d spec point. D40 stands for dev: P3-i
  computes no match-forecast score on dev seasons, so there is nothing on dev to fit it
  to. Its new defensive player rating is checked on dev on its own (P3-i0), like P3-d0.
- **D44 (resolved 2026-11-21, owner: "Close P3-i"):** P3-i is closed without a
  respecification. P3-i0's dev check passed only by the letter: k sat at the top of its
  grid in every refit, so every θ was about 1. No live shadow is started; P stays
  production.

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
- [x] **P3-e Feedback loop into Layer 6 (§7.5 steps 1-4).** *Dev pass 2026-11-20 as
      restated (scorers); the live verdict (f) is pending.* Player goal shares sum to the
      team's non-own-goal rate by construction; own goals stay outside the allocation.
      *On hold 2026-11-19 (D40):* P3-d failed, so there is no passed offset to wire. Its
      scope is to be restated before any build.

      **Restated 2026-11-19 (owner: "Restate P3-e as scorers"): goalscorer and assist
      forecasts allocated from P (§7.5 steps 3-4, without steps 1-2).** P's team rate is
      split among the matchday squad as normalised shares, so player expectations sum to
      the team's non-own-goal rate by construction. Nothing goes back into Layer 6: P is
      unchanged, and no lineup offset enters it (D40).

      Acceptance (stated 2026-11-19 before building, owner: "Approve as drafted"; no
      P3-e score has been computed. Seen so far: P3-d0's dev result, including goals vs R1
      +0.15% and summed player expected goals per team-match E/O 1.021, correlation 0.160
      with team goals; P3-d's dev result):

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

      **Build (2026-11-20), before any run:**
      - **Code:**
        - `adatta/features/scorers.py`: the player table (squad and P3-b pool rows, e at
          lk and T-24h, q as starter and substitute, R0 and R1, outcomes with P3-d0's
          null rule, penalty goals, o and α as of each target, the under-18 flag);
        - `adatta/eval/scorers.py`: the thinned allocation, B0, B_line and B_R1, the
          metrics, consistency, the pass rule (e) and the report;
        - `adatta/model/scorers.py`: the pmf stage (P from the fit cache; a missing fit
          stops the job, never refits), the table with its leakage check, and assemble;
        - `lineup quality --stat assists`, and `lineup_offset.quality` takes the statistic
          and the substitute flag (P3-d's defaults unchanged);
        - `backtest.score_targets(pmf=True)`: each side's goal distribution, the margins
          of the posterior-mean score matrix.
      - **CI:** `.github/workflows/scorers.yml`, run in four stages: quality (assists),
        pmf, table, assemble.
      - **Tests:** `tests/test_scorers.py` (7): the table's minutes, rates, outcomes and
        shares; nothing from the match or later changes an input; the under-18 flag;
        thinning against the binomial and Poisson closed forms; the allocation sums to
        the team rate; the T-24h population; the pass rule.
      - **Fit cache checked:** P's hash is a215d52e759864ed now and in P3-d's dev run, so
        the pmf stage reads its 928 fits.
      - **Order of runs,** one at a time: quality (assists, about 1-2 h), pmf (25 jobs),
        table, assemble.

      **Runs:**
      - **Quality (assists):** run 44684594053, 05:59-06:34 UTC. All refits pass the
        gates, worst R-hat 1.005.
      - **pmf:** run 44688838616, 25 jobs, all success, P read from the fit cache with no
        refits.
      - **Table:** run 44689625188, player table 33d106c3b5f490c3: 1,292,331 rows, 38,784
        team-matches, built in 71 s. Leakage 0 of 100. Announced players outside P3-b's
        pool (role fallback) 1.5-3.0% per league; o 0.022-0.034, α 0.78-0.80.
      - **First verdict,** run 44690643347: **goals fail, assists fail.**
        - Goals pass (e)1-4 and (e)6: vs B0 -0.79% [-0.86%, -0.71%], vs B_line -1.93%
          [-2.04%, -1.81%], vs B_R1 -0.00% (leagues -0.21% to +0.19%), E/O 1.002, ECE
          0.0014 (worst league 0.0031). They fail (e)5: largest gap 1.19.
        - Assists pass (e)1-3 (vs B0 -0.32%, vs B_line -1.65%, vs B_R1 -0.18%) and ECE
          0.0062. They fail E/O (1.147, 1.09-1.21 in every league) and (e)5 (gap 0.93).
        - Reported: penalty goals 4,285 of 46,034, M vs B_R1 +0.78% on those rows;
          goalkeepers' goal E/O 1.80 (rare counts).
        - **Cause of the assists E/O (a bug):** the provider leaves a player's assists null
          when they are zero. `shares` counted a team-match as having assists collected only
          when some player's value was non-null, which keeps mostly team-matches where
          someone assisted. So α came out 0.78-0.80; the realised assisted share is
          0.66-0.72 per league (2022: 6,754 assists for 9,772 goals). The acceptance says
          "collected", meaning P3-d0's rule (the team-match has a statistics block), which
          the code did not implement.
        - **Cause of the (e)5 gap:** one team-match of 38,784 has 7 squad rows, all with
          zero expected minutes, so the shares are undefined and nothing is allocated.

      *Amended 2026-11-20 after the first verdict, whose scores had been seen; owner: "Yes,
      fix and rerun":*
      1. **α uses P3-d0's collected rule.** A team-match counts towards α when it has a
         statistics block (any player's `minutes_provider` is non-null), with null assists
         counted as 0. This is what (a) stated; the code now does it.
      2. **A team-match with no positive weight gets no player forecasts** (principle 8:
         no imputed shares). It is left out of every model's scoring and of (e)5, and its
         count and realised goals are reported.
      Nothing else changes: the baselines, the gates, the rates, P and the pmf run
      (44688838616) are reused. The table and the verdict are rerun.

      **Result 2026-11-20: pass, goals and assists** (table rerun 44693306106, player
      table 0e81c3ec404d3635, leakage 0 of 100; verdict run 44694616526, both on 8fa7de3,
      amendments 1-2). Relative P(at least one) log loss, negative = M better, paired by
      player row at lineups-known:

      | | goals | assists |
      |---|---|---|
      | rows (realised) | 733,137 (46,033) | 780,653 (33,734) |
      | (e)1 vs B0 | -0.79% [-0.86%, -0.71%] | -0.64% [-0.71%, -0.56%] |
      | (e)2 vs B_line | -1.93% [-2.04%, -1.81%] | -1.64% [-1.76%, -1.53%] |
      | (e)3 vs B_R1 | -0.00% [-0.07%, +0.07%] | -0.18% [-0.24%, -0.12%] |
      | (e)3 worst league vs B_R1 | +0.19% (62) | -0.05% (140) |
      | (e)4 E/O | 1.002 | 0.996 |
      | (e)4 ECE, worst league | 0.0014, 0.0031 (61) | 0.0003, 0.0019 (78) |
      | (e)5 largest gap | 0.0000 | 0.0000 |

      - α after amendment 1 is 0.65-0.72 per league (0.78-0.80 before); goals' scores are
        unchanged from the first verdict.
      - Left out under amendment 2: 1 team-match (1 goal, 1 assist). P's distribution exists
        for 38,504 of 38,784 squad team-matches.
      - Reported, not gating:
        - **T-24h** (P3-b's pool): goals vs B0 -0.71%, vs B_line -1.83%, vs B_R1 +0.17%
          [+0.11%, +0.22%]; assists vs B0 -0.58%, vs B_line -1.37%, vs B_R1 +0.03%. A day
          out, the player's own shrunk rate is as good or slightly better than P3-d0's.
        - **Penalties:** 4,285 of 46,033 goals; on the 4,175 rows with a penalty goal M is
          +0.78% against B_R1 (penalty takers are §8.3, Phase 3b).
        - **Goalkeepers:** goal E/O 1.80 on rare counts (GK line rate); the GK rows' log
          loss is far below B_line's and B_R1's.
        - **By line,** goals vs B_R1: D -1.18%, F -0.00%, M +0.53%.
      - **Next, per (f):** M is scored on arrival on live 2026/27 bundle league matches at
        lineups-known, against the same baselines, with bookmaker anytime-scorer prices for
        Edge and CLV only. Nothing is shown to users before the live verdict.

      **Live verdict (f), acceptance (stated 2026-11-20 before building, owner: "Approve as
      drafted"; no live P3-e score has been computed):**

      *Design choice.* P3-b's minutes model and P3-d0's rates are not refitted in production.
      Two ways to score M live:
      - **As-of batch (this draft).** The dev pipeline, extended to season 2026, rerun on CI
        at intervals. Every input is taken as of its time (the temporal contract). Roles are
        those of the captured `lineup_snapshot`, first seen before kickoff. P is the
        production refit behind the forecast actually published by then.
      - **Frozen at lineup time.** P3-b and P3-d0 served live, and each forecast written to
        a table when the lineup is captured. Stronger against provider revisions, but it
        needs both models in production first. It is built only if M passes live and is to
        be shown to users.

      (a) **Targets.** Bundle league matches of season 2026 whose two lineups were captured
      before kickoff (`lineup_snapshot`, from 2026-11-07), regular season, with a 90-minute
      result. Population: the announced XI and bench of the snapshot. A named player who
      doesn't come on scores 0, as on dev.

      (b) **Inputs, each as of its time:**
      - **P:** the team goal distribution from the live serving refit behind the latest
        forecast published before the lineup was first seen (`projection_run` →
        `model_run`). This is what was served then, D41 refit milestones included.
      - **Minutes:** P3-b refitted on its 28-day grid continued through 2026, as on dev.
        P3-b is fitted per refit time and its role minutes taken for the announced roles;
        P3-d's role fallback covers players outside its pool.
      - **Rates:** P3-d0's goals and assists hyperparameters refitted on its 28-day grid
        continued through 2026, with the player posteriors as of kickoff - 24 h.
      - **o and α** as on dev (amendment 1), over the 730 days final by then.
      - **Holdout seasons (2024/25, 2025/26) are loaded as history only.** The 730-day
        windows and P3-b's training reach back into them, as the production model's refits
        already do. No holdout match is ever a target or scored, and the dev verdict is
        not revisited.

      (c) **Pass, judged once** after the last matchday of the 2026/27 bundle league
      seasons, for goals and for assists separately: P3-e (e)1-5 unchanged, at lineups-known,
      on the live targets. Interim runs (about monthly) are reported as interim; nothing is
      tuned or changed because of them. A change of model or rule restarts the live clock.

      (d) **Market, reported only (principle 1).** For goals, on players the stored odds
      price ("Anytime Goal Scorer", raw snapshots at the T-60m and close milestones): M's
      P(scores) against the median implied probability across books, with no margin
      removal, plus the log loss of each. Assists have no pure market ("Player to Score or
      Assist" is a different event), so none. Odds are read only here, downstream: never an
      input to M, P3-b or P3-d0, and never part of (c).

      (e) **Leakage:** for 100 sampled live player rows, nothing observed after each input's
      as-of time changes e, q, o, α or P. That covers appearances, the counts table, P3-b
      rows and serving refits. The lineup used must have been observed before kickoff.

      (f) **Nothing reaches users** before a pass: no dashboard panel, no API field.
      Outputs stay internal (R2), under-18 players flagged (CLAUDE.md rule 10).

      **Cost:** 0 API requests (the odds are already captured). Per interim run: P3-b and
      P3-d0 refits for the new 28-day steps (a few each), one table, one scoring run.
      Each is a backtest-type run, one at a time. Appearances for 2026 are rebuilt from the
      stored detail payloads before each run (`adatta appearances-build`).

      **Live build (2026-11-20), before any live run:**
      - **Code:**
        - live mode in P3-b (`eval.selection build-table --live`, `model.selection run
          --live`) and P3-d0 (`eval.rates build-table --live`, `lineup quality --live`).
          `check_max_season` lets a live build load through the live season with the
          holdout seasons as history; their matches are never targets. Live tables leave
          the dev "latest" pointers alone. The live refit grids continue the dev 28-day
          grids;
        - `features.scorers`: `snapshot_squad` (the announced squad of a captured lineup
          as the population), `name_keys` and `match_names` (odds names to players: full
          name, then initial and surname, then surname, only when unique in the match);
        - `api_football_capture.parse_scorer_odds` (the anytime-scorer market of a stored
          odds payload, principle 3);
        - `adatta/model/scorers_live.py`: live targets, snapshot squads, P as served (the
          refit behind the latest forecast before the lineup, recomputed and checked
          against the published 1X2), the live leakage check, the table and assemble;
        - `adatta/edge/scorers.py`: the market and its report, in a new downstream
          package. The principle-1 test bars odds from model, eval, sim and feature code.
      - **CI:** `.github/workflows/scorers_live.yml`, run in five stages: prep, minutes,
        quality (goals, then assists), table (then the edge market), assemble (then the
        edge report).
      - **Tests:** `tests/test_scorers_live.py` (7): season guards, the live grid, the
        snapshot squad, the odds parser, name matching, live targets, and P as served.
      - **Dry run on live data** (no model fits, counts only):
        - 91 matches have both lineups captured before kickoff (from 2026-11-07);
        - 11 of them have a forecast published before the lineup, because forecasting
          began 2026-11-11. The other 80 have no P as served and are not targets. In
          effect the live clock starts on 11 Nov;
        - P as served reproduces the published 1X2 to 6e-17, from the T-60m forecast;
        - all 91 matches are priced; 6,440 of 9,262 anytime-scorer names match a squad
          player (the market also lists players outside the squad);
        - 78 of about 3,850 snapshot players are not in the player table yet, so they are
          dropped (reported each run).

      **Live runs:**
      - **Interim 1 (2026-11-20, reported only):**
        - prep 44699330567: counts table 71358817b70d0753 (leakage ok), selection table
          399fb6ab0b584233 (79,771 team-matches, leakage 0). Goals drop out for Ligue 1
          2026 under the (a) check: 1 of 180 team-matches has a goal event missing
          (99.44%, the bar is 99.5%). This is a small-sample effect and clears once one
          miss falls under 0.5%;
        - minutes 44701271574: 22 P3-b refits, 20 pass the gates (England 3 of 4, Italy
          3 of 4). As on dev (P3-b amendment 9), refits failing the gates are redone before
          the final verdict;
        - quality 44712775639 (goals) and 44713867390 (assists): 5 refits each, all pass
          (R-hat 1.005 or less);
        - table 44715008063, live player table 28fc9a2dbbe6f5d6: leakage 0 of 100, P as
          served for 11 of 91 matches, reproduced to 5.6e-17;
        - assemble 44716136377: 11 matches (464 player rows, 27 goals, 20 assists, all
          from 7-16 Nov). Goals vs B0 +0.16% [-2.73%, +3.05%], vs B_line +1.44%, vs B_R1
          +1.33%; E/O 0.996. Assists E/O 0.927. Every interval spans zero widely; nothing
          is read from it.
        - **Market comparison as stated is not informative.** Without margin removal the
          anytime-scorer prices imply 0.184 on average against 0.066 observed (and M's
          0.061), so M "beats" the market by the bookmakers' margin alone (log loss 0.215
          against 0.287). Raised with the owner.
        - The report's footer counted the 91 table matches; it now counts the matches
          scored.

      *Amended 2026-11-20 after interim 1 (owner: "Yes, add AUC"):*
      1. **(d) adds AUC.** The market comparison also reports the AUC of M and of the
         market on the same rows: the chance a scorer is ranked above a non-scorer. A
         margin that keeps the market's order of players leaves it unchanged. It is
         reported with the difference and a 95% interval from 1,000 resamples of whole
         matches. The log loss stays as stated. Both remain reported only, never part
         of (c).
- [ ] **P3-f Status-informed availability (D39: judged live).** P3-b with the injury feed:
      players the provider lists before the match as out or doubtful. Requested 2026-11-20
      (owner: "simple first then proceed with using injury feed"). The simple version (P3-b
      served, the feed shown beside it, docs/DASHBOARD_TASKS.md) runs meanwhile.

      Acceptance (stated 2026-11-20 before building, owner: "Approve as drafted"; no
      P3-f score has been computed. Seen so far: the counts of captured spells, 958
      "missing" over 328 matches and 177 "questionable" over 57 matches, 2026-11-05 to
      2026-11-20):

      *Why an overlay, not a refit with status covariates.* Status can't be reconstructed
      for past seasons (D39), so a status term can only be learned from live captures, two
      weeks so far. A whole-model refit on that is out of reach. The overlay keeps P3-b as
      validated and learns two numbers from live data.

      (a) **Model.** For each pool player, P3-b's raw (start, bench, out) probabilities. If
      the open status spell at the as-of time t (kickoff - 24 h, or the projection time)
      lists the player:
      - "missing": the start and bench probabilities are multiplied by ρ_m;
      - "questionable": they are multiplied by ρ_q.
      The rest goes to out, and P3-b's coherence step then spreads the team's 11 starts
      and bench places over the others as on dev. Unlisted players are untouched, apart
      from that redistribution.

      (b) **ρ_m and ρ_q, learned live and as of t.** ρ_s is the posterior mean of the
      share of listed players who were still in the matchday squad, over the listed
      player-matches final by t (live captures only). Priors: ρ_m ~ Beta(1, 19) (mean
      0.05), ρ_q ~ Beta(5, 5) (mean 0.5), so early estimates stay near them. Updated at
      each projection in closed form; nothing else is fitted.

      (c) **Targets and baseline.** Live bundle league team-matches with a P3-b projection
      as of kickoff - 24 h and a captured status as of then. The comparison is paired by
      player row:
      - P3-f against P3-b on the same pool and draws;
      - the realised outcome from the final record (start, bench or out).

      (d) **Pass, judged once on 2027-01-04** (about seven weeks of captures), if at least
      1,000 listed player-matches are final by then. Otherwise the judgement moves on four
      weeks at a time, without looking at the scores. All must hold:
      1. Three-way log loss: P3-f beats P3-b over all pool rows, with the paired 95%
         interval excluding 0;
      2. on the listed players' rows, P3-f beats P3-b the same way;
      3. on unlisted rows, P3-f is no worse than P3-b by more than 1.0% (point estimate);
      4. ECE of P(start) at most 0.02, and of P(start) on listed rows at most 0.05;
      5. leakage: no spell observed after t changes a probability (100 sampled rows).
      Reported: ρ_m and ρ_q over time; listed players who started; by league.

      (e) **Then.** On a pass, the served projection switches to P3-f and the panel says
      the feed is used; the simple version's label goes. On a fail, the simple version
      stays, and any respecification is disclosed. Interim reports (about weekly) are
      reported only; nothing is tuned on them.

      **Cost:** 0 API requests (the feed is already captured daily). No refits: it reuses
      the serving P3-b draws and the stored projections.

      **Build (2026-11-20), before any P3-f score:**
      - **Code:**
        - `adatta/sim/lineups.py`: `status_as_of` (the spell open at t), `rho` (Beta
          posterior over listed player-matches final by t whose squads are captured) and
          `project_f` (the overlay, then P3-b's coherence per draw). P3-f is computed in
          shadow in every daily projection and stored beside P3-b (migration 0017, applied
          2026-11-20); the dashboard doesn't read it;
        - `adatta/eval/availability.py` and `adatta lineups-score [--final]`: the rows
          made as of kickoff - 24 h, outcomes from the final record, (d)1-5 and the report.
          Aggregates only.
      - **Tests** in `tests/test_projected_lineups.py` (4 more): the status open at t; ρ's
        posterior, counting only final matches with captured squads; a listed player's
        chances moving to his team-mates with the team's totals kept; scoring and the
        leakage check.
      - **The first shadow rows** come with the next daily run (04:41 UTC). Interim
        reports are `adatta lineups-score`, about weekly.
- [x] **P3-g Projected formation and XI by position (§7.3). Closed as failed (D42).** Owner, 2026-11-20: "we
      always need a gk projected to start, we need a projected formation based on previous
      matches and thus we need more specific player positions to better predict who will
      fill the starting role for each position". The keeper part is a display fix, done the
      same day (docs/DASHBOARD_TASKS.md). This task is the rest: P3-c's formation model
      (passed on dev) and the slot codes, combined with P3-b's starts into a lineup slot by
      slot.

      Acceptance (stated 2026-11-20 before building, owner: "Approve as drafted"; no P3-g
      score has been computed.
      Seen so far: P3-c's dev results, including role assignment 88.3% given the actual XI
      and formation; P3-b's dev results):

      (a) **Formation.** P3-c's model as passed, served like P3-b: refitted per bundle every
      28 days, predicted as of the projection time. The projected formation is its most
      likely class, shown with its probability and the next two.

      (b) **Who fills each slot.** For the projected formation's 11 slot codes:
      - **fit(i, s)** is player i's as-of share of starts in code s. It counts positional
        matches only (P3-c amendment 1) and is smoothed towards the codes of his line, with
        a fixed pseudo-count of 3 starts. A player with no coded starts takes his line's
        team shares;
      - **W(i, s) = P(start_i) · fit(i, s),** scaled by iterative proportional fitting so
        that each slot sums to 1 over players and each player sums to P(start_i) over
        slots. These are the per-slot probabilities;
      - **the projected XI** is the one-player-per-slot assignment with the largest
        Σ log W (Hungarian algorithm). The GK slot takes keepers only, so there is always
        exactly one keeper.

      (c) **Dev check, at T-24h like P3-b.** Positional dev league-seasons (2022-2023 and La
      Liga 2019), from P3-b's export rows and P3-c's stored dev predictions, with no refits.
      Paired by team-match:
      - **XI overlap** (actual starters in the projected XI, out of 11): P3-g against
        B_top, the keeper plus the top 10 by P(start) (today's panel);
      - **slot accuracy** (slots whose projected player started in that code), on
        team-matches whose projected formation was the actual one: P3-g against B_last,
        each slot's player from the team's last match in that formation;
      - **per-slot calibration:** ECE of W(i, s) for "i started in s", on the same
        team-matches.

      (d) **Pass.** All must hold:
      1. XI overlap no worse than B_top by more than 0.05 players a team-match (point
         estimate);
      2. slot accuracy beats B_last, paired 95% interval excluding 0;
      3. per-slot ECE at most 0.03;
      4. leakage: nothing final after the as-of time changes fit, W or the formation
         (100 sampled team-matches).
      Reported: formation top-1 accuracy; results by league and by position family;
      players new to the team.

      (e) **Then.** On a pass, the panel shows the projected formation and, per slot, the
      most likely player with alternatives, keeping P(start) and minutes. Live seasons need
      to pass P3-c's positional test before full codes are used; otherwise the slots are
      line-level (D, M, F) and say so. On a fail, the panel stays as now.

      **Cost:** 0 API requests. The dev check reuses stored outputs; serving adds P3-c
      refits (median 103 s on dev) to the daily workflow when due.

      *Amended 2026-11-20 while building, before any P3-g score was computed:*
      1. **P3-c's dev predictions are rerun, not reused.** P3-c's stored dev rows hold
         each team-match's scores (log loss, the top class's probability, whether it was
         right), not which formation it predicted. So (c) can't read the projected
         formation from them, as the cost line assumed. P3-c's scoring now also stores the
         predicted class and its distribution (`top`, `p`; nothing else changes). Its dev
         walk-forward is rerun on the positional league-seasons only (all bundles in
         2022-2023, and La Liga 2019), at 2,000 draws as it passed, without its pass rule.
         The P3-g check reads those rows.
      2. **Player totals for the scaling are rescaled to the slots.** P3-b's starts sum to
         11 minus the newcomer allowance, but the formation has 11 slots, so the two
         margins of W can't both hold as stated. Within keepers, P(start) is rescaled to
         the one GK slot; within outfield players, to the other 10. A player's share of his
         group is unchanged.

      **Build (2026-11-20), before any P3-g score:**
      - `adatta/eval/positions.py`: slot codes per formation (P3-c's rules), the as-of
        position history and fit, W and its scaling, the Hungarian XI, B_top and B_last,
        the evaluation, calibration, leakage, the verdict and the report;
        `python -m adatta.eval.positions --formation-runs RUNS` prints metrics only.
      - `eval.formation.score` stores `top` and `p` (amendment 1).
      - `tests/test_positions.py` (5): slot codes, the scaling's margins and the single
        keeper, the fit's smoothing, a settled team's evaluation and leakage, and that the
        target match's own positions are never in its history.

      **Result (2026-11-20): fail.** P3-c reruns 44743758503 (2022-2023) and 44748840252
      (La Liga 2019); 16,318 dev team-matches across 29 positional league-seasons; the
      projected formation was right in 58.9%.

      | check | value | 95% interval | pass |
      |---|---|---|---|
      | (d)1 XI overlap, P3-g - B_top (players) | -0.109 | [-0.121, -0.098] | no |
      | (d)2 slot accuracy, P3-g - B_last | -0.002 | [-0.005, +0.001] | no |
      | (d)3 per-slot ECE | 0.017 | | yes |
      | (d)4 leakage (100 sampled) | clean | | yes |
      | keeper in every projected XI | 0.9998 | | no |

      Mean XI overlap 8.449 (P3-g) vs 8.558 (B_top) of 11; mean slot accuracy with the
      formation right 0.704 vs 0.709 (B_last). Worse than B_top on overlap in all ten
      leagues; on slot accuracy, ahead of B_last in leagues 135, 140 and 141, level in
      136, behind in the other six.

      Diagnosis (same inputs, aggregates only):
      - **The slot assignment itself costs starters.** With the formation right, P3-g's
        XI is 0.080 players behind B_top (8.633 vs 8.713, 9,611 team-matches); with it
        wrong, 0.151 behind (8.186 vs 8.338, 6,707). Per team-match, P3-g gets more
        starters right in 2,633, fewer in 4,224, the same in 9,461. Forcing one player per
        slot code swaps a likely starter for a worse-fitting player's position more often
        than it fixes a P(start) ranking.
      - **The 4 keeperless XIs** are team-matches whose P3-b pool has no player coded G
        (25-33 players each), so there is no keeper to place. B_top has the same gap.

      Per (e), the panel stays as it is. Nothing was tuned; holdout untouched.

      **Restated (2026-11-20, owner: "Restate: arrange B_top"). Acceptance stated before
      building (owner: "Approve as drafted"; no restated score computed).** Informed by the failed dev check above (dev window only; holdout
      untouched). Who starts is no longer P3-g's job:

      (a) **Formation:** unchanged (P3-c's most likely class, with its probability and the
      next two).

      (b) **XI and arrangement.** The XI is B_top: the keeper and the 10 outfield players
      most likely to start by P3-b. P3-g only places them: the keeper takes the GK slot,
      and the 10 outfield players take the other 10 slots by the assignment with the
      largest Σ log fit(i, s) (Hungarian algorithm; fit as before). Alternatives per slot
      come from W as before (shown, not used to pick). If P3-b's pool has no player coded
      G, the GK slot is shown empty, labelled "no keeper in the squad list", and never
      filled by an outfielder.

      (c) **Dev check.** Same team-matches, inputs and as-of times as above. Paired by
      team-match, on team-matches whose projected formation was the actual one:
      - **slot accuracy** of the arrangement against B_last;
      - also reported: against B_xi_last, which places the same B_top XI by each player's
        code in the team's last match in that formation (others fill the remaining slots
        of their line by fit). This separates "better placement" from "better XI".

      (d) **Pass.** All must hold:
      1. slot accuracy beats B_last, paired 95% interval excluding 0;
      2. the XI is B_top's in every team-match (overlap difference exactly 0);
      3. per-slot ECE of W at most 0.03. W is unchanged, and its ECE on this dev data is
         already known (0.017). This is a consistency check, not new evidence;
      4. leakage: as before (100 sampled team-matches).
      Reported: by league and position family; the keeperless count; formation top-1;
      the reference ceiling, P3-c's role assignment given the actual XI and formation
      (88.3%).

      (e) **Then.** As before: on a pass, the panel shows the formation and the arranged
      XI with per-slot alternatives; on a fail, the panel stays as now.

      **Cost:** 0 API requests; local, minutes (the stored reruns are reused).

      *Clarified 2026-11-20 while building, before any restated score:* when P3-b's pool
      has no player coded G, B_top is 11 outfield players. The GK slot stays empty, the 10
      outfield slots are filled from those 11 by fit, and the one left over stays in the
      XI unplaced. (d)2 compares the XI as a set, so it holds exactly.

      **Build (2026-11-20), before any restated score:**
      - `adatta/eval/positions.py`: `arrange` (keepers only in goal, outfield never;
        Σ log fit), `place_last` (B_xi_last), slot hits by line, `verdict_arranged` and
        `markdown_arranged`. The CLI prints the restated report by default and the first
        statement's report with `--first-statement`. Leakage also compares the arranged
        and B_xi_last slot accuracies.
      - `tests/test_positions.py` (+4): the keeper in goal and an empty GK slot when there
        is no keeper; B_xi_last keeps last-match codes and fills the rest by fit; a
        settled team's arranged evaluation; the target match's own positions never in
        the arrangement's history.

      **Restated result (2026-11-20): fail.** Same 16,318 dev team-matches; 9,525 paired
      with B_last (formation right and a last match in it).

      | check | value | 95% interval | pass |
      |---|---|---|---|
      | (d)1 slot accuracy, arranged B_top - B_last | -0.030 | [-0.034, -0.027] | no |
      | (d)2 XI is B_top's in every team-match | yes | | yes |
      | (d)3 per-slot ECE of W | 0.017 | | yes |
      | (d)4 leakage (100 sampled) | clean | | yes |

      Mean slot accuracy: arranged 0.679, B_last 0.709, B_xi_last 0.695 (arranged -
      B_xi_last -0.016 [-0.019, -0.014]). Behind B_last in every line (G 0.900 vs 0.911,
      D 0.679 vs 0.706, M 0.662 vs 0.697, F 0.610 vs 0.647) and every league. GK slot
      empty in 4 team-matches (no keeper in P3-b's pool). Ceiling for reference: 0.883.

      Reading: the smoothed as-of fit places a known XI worse than "where each played in
      the team's last match in this formation" (B_xi_last), and B_top's XI is itself
      behind the last XI in that formation slot for slot, keeper included. Both forms of
      P3-g lose to the last lineup in the projected formation. Per (e), the panel stays as
      it is. Nothing was tuned; holdout untouched.

      **Closed 2026-11-20 (D42), owner: "Close as failed".** Not served; the panel is
      unchanged.

- [ ] **P3-h Formation view: P3-c served, and the last lineup in that formation.** Owner,
      2026-11-20: "i will want to display the probability of the selected formation along
      with the two next best options".

      Acceptance (stated 2026-11-20 before building, owner: "Approve as drafted"). Seen so far: P3-c's
      dev results (top class right 58.9% on the positional dev seasons, top-class ECE
      0.018); P3-g's dev results (slot accuracy of the last lineup in the projected
      formation 0.709 when the formation is right); and a pre-check (2026-11-20, metrics
      only):
      - the last lineup in the projected formation exists in 98.1% of dev team-matches. It
        has 8.29 of the 11 actual starters against 8.59 for B_top (-0.30 ± 0.02). So it is
        better at where players line up and worse at who starts;
      - live 2026 grid test: positional in 135, 140, 141, 39, 40 and 78; not yet in 61, 62,
        136 and 79 (full-backs on their side 0.72-0.87 against 0.90 required, on 60-99
        rows; it may pass as the season fills).

      (a) **Formation served.** P3-c's model as passed, refitted per bundle every 28 days
      at 2,000 draws in the daily projected-lineups workflow (dev 28-day grid, as P3-b).
      A refit must pass P3-c's inference gates (R-hat, ESS, divergences, top-class PPC).
      Otherwise the previous passing refit stays, and with none the view shows no
      formation. Predicted as of the projection time for the same fixtures as P3-b (the
      next 7 days). Inputs are final before that time only: formation and coach history,
      and the 1X2 probabilities of the forecast published before it.
      Stored with run id, refit id and as-of time in a new table (forward-only migration
      0018, nothing dropped).

      (b) **Shown.** The most likely formation with its probability, and the next two with
      theirs ("other" shown as "other shape"). The pitch uses the most likely named
      formation, the next one when the top is "other", as P3-g did.

      (c) **Pitch.** The team's last lineup in the pitch formation, as of the projection
      time, labelled "last lineup in this formation (dd Mon)". Each player shows his P3-b
      start probability and any status tag (missing or questionable).
      - In league-seasons passing the grid test, it is laid out by slot code (LB, LCB...).
      - Elsewhere it is laid out by line only, labelled "positions by line only".
      - With no previous lineup in that formation, the formation is shown without a pitch.
      P3-b's starts list stays as it is, beside the pitch. The pitch is history, not a
      forecast of who starts. No player is swapped in or out.

      (d) **Checks.**
      1. Serving: the stored predictions equal P3-c's on the same rows and refit, and are
         unchanged if lineups or coaches after the as-of time are added (tests and 20
         sampled live fixtures).
      2. Live monitoring, reported, not a gate: top-1 accuracy and top-class ECE on 2026
         team-matches as they finish, at the P3-e/P3-f interim reports. If top-class ECE
         exceeds 0.05 on at least 1,000 team-matches, I raise it with you and the
         probabilities come off the panel until it is resolved.

      (e) **Then.** On (d)1 passing, the panel shows (b) and (c) on the match page.

      **Cost:** 0 API requests. Refits about 2 minutes per bundle when due; migration
      0018 (new table).

      *Amended 2026-11-20 while building, before anything was served (owner: "Yes, rerun
      without it"):*
      1. **No opponent tier when served.** P3-c's tier came from a stored dev backtest.
         Published forecasts start 2026-11-11, so a served refit's 730 training days would
         have almost no tier while the fixtures it predicts would; trained one way and
         predicted another, its probabilities could be off. Served P3-c sets tier unknown
         (tier = 0, tier_known = 0) in training and prediction. Before serving, P3-c's dev
         walk-forward is rerun the same way (table bd4423912693aa6d = 9a05c70602047e45
         with tier unknown on every row; all bundles, 2019-2023, 2,000 draws; run
         44785445515). It must pass P3-c's rule (e) again; otherwise no formation
         probabilities are served and this comes back to the owner.

      **Build (2026-11-20), nothing served yet:**
      - `adatta/features/formation.py`: `build(..., unplayed=True)` keeps fixtures with no
        lineup yet (outcome -1) and honours a target's own `as_of`. The dev path is
        unchanged.
      - `adatta/eval/formation.py`: `load_raw(..., live=True)` loads through the live
        season with tier unknown; `no_tier` and `python -m adatta.eval.formation no-tier`
        derive amendment 1's dev table; `save_table(..., latest=False)`.
      - `adatta/model/formation.py`: `serve_refit` (dev 28-day grid, 730 days, 2,000
        draws, gates; stored, and latest only on a pass) and CLI `serve-refit`.
      - `adatta/sim/formations.py`: refit store, `last_lineups`/`pitch` (final by as_of -
        RESULT_LAG), `distribution`, and `run`, which appends to `formation_projection`;
        CLI `adatta formations-project`.
      - `adatta/db/models.py` + `migrations/versions/0018_formation_projection.py`.
      - API: `TeamSquad.formation` (three most likely, the pitch with P3-b's P(start) and
        feed tags). Web: `FormationBlock` in `web/components/Squad.tsx`.
      - `tests/test_formation_view.py` (4): tier-unknown rows equal the rerun's table;
        unplayed rows, their own as-of, and later formations changing nothing; the pitch
        as of the projection (lines only when not positional); the refit store and the
        distribution equal P3-c's; the panel shows the latest projection's top three and
        the pitch.
      - The workflow steps (refit, project) are added only once the rerun passes.

      **Amendment 1 rerun (2026-11-20): pass** (run 44785445515, table bd4423912693aa6d,
      2,000 draws). All 19 checks of P3-c's (e) pass, with every refit passing the gates.
      Log loss 1.460 against F1 1.710 and F2 1.526 (vs F2 -4.35% [-4.71%, -4.00%]; with
      tier, -4.69%). It beats F2 in every league. Family log loss 0.497; top-class ECE
      0.0205 (with tier, 0.018); top-class accuracy 0.584. Dropping tier costs 0.005 in log
      loss, so P3-c is served with tier unknown.

      **Ready to serve (2026-11-20).** Migration 0018 applied. The refit and project steps
      are added to `projected_lineups.yml`; each runs even if P3-b's step fails. Local
      smoke test on live data, writing nothing (no refits yet, so every bundle reports no
      refit):
      - 121 fixtures, 242 team rows, tier unknown on all, every team with formation
        history;
      - a last lineup in the formation was found for all 242. Positional ones are 70: the
        grid test on everything loaded (all stored competitions, as P3-c's build) passes
        2026 in 135, 140, 141, 78 and 80. The bundle-only pre-check also passed 39 and 40;
        the build's set is what's served.

      **First live run (2026-11-20, run 44823849231).** All five formation serving refits
      pass the inference gates (tier unknown, 2,000 draws, 0 divergences):

      | bundle | refit as of | train rows | R-hat max | ESS bulk min | PPC q |
      |---|---|---|---|---|---|
      | England | 2026-11-09 | 3,735 | 1.003 | 5,494 | 0.560 |
      | Spain | 2026-10-26 | 3,371 | 1.002 | 4,637 | 0.525 |
      | Italy | 2026-11-02 | 3,037 | 1.002 | 2,987 | 0.415 |
      | Germany | 2026-11-02 | 2,457 | 1.002 | 3,693 | 0.535 |
      | France | 2026-11-02 | 2,436 | 1.002 | 4,257 | 0.485 |

      The refit time is the last point of each bundle's 28-day dev grid, as P3-b, so Spain's
      next refit is due on 2026-11-23. The projection wrote 234 team rows for 117 fixtures:
      every team has a last lineup in its pitch formation, 72 of them positional.
      **(d)1 passes:** 20 sampled stored rows (18 fixtures, all five refits), recomputed from
      the stored refit on current data at the stored as-of time, match the stored
      probabilities to 4.95e-07 (the 6-decimal rounding), with the same pitch formation and
      the same pitch on 20 of 20. Per (e), the panel shows the three options and the pitch on
      the match page. Live monitoring, (d)2, starts with the interim reports.
- [ ] **P3-h2 Projected pitch: the last lineup in the formation, with unavailable players
      replaced.** Drafted 2026-11-27 before building; owner: "Approve both as drafted"
      (D64, shadow then show).

      **Why.** P3-g's slot-by-slot XI lost to "the team's last lineup in the projected
      formation" (D42), and P3-h now shows that lineup unchanged ("no player is swapped in
      or out"). On 2026-11-27 the owner agreed the next step: start from that lineup and
      replace only the players who can't play. The played page already scores the pitch
      against the last lineup unchanged ("Positions right: pitch · last lineup"), so the
      judge costs nothing new.

      (a) **The rule, nothing fitted.** As of the projection time:
      - A pitch player is replaced if the injury feed lists him out (status "missing").
        A questionable player stays, tagged.
      - The replacement is the available squad player with the highest P3-b start
        probability among those with a coded start in the same slot code. With none, it
        comes from the same line; with none there either, the slot keeps its player,
        flagged.
      - Each player is used once.
      - Inputs are final before the projection time only: lineups, slot codes, feed
        status and P3-b.

      (b) **Shadow first.** Stored beside P3-h's pitch (a new column, forward-only
      migration), not shown until (d) passes. The panel keeps today's pitch.

      (c) **Window.** Team-matches of the five bundles from approval to the winter break,
      at least 1,000 with an announced lineup. It is read once (D59 default).

      (d) **Gate, against the last lineup unchanged on the same team-matches:**
      1. XI overlap (projected pitch players who started) is better, with the paired 95%
         interval excluding zero;
      2. in positional league-seasons, slot accuracy is not worse by more than 0.01
         (point estimate).

      Reported: the number of replacements, replacement hit rate, and by bundle.

      (e) **On a pass,** the panel shows the replaced pitch, labelled "projected from the
      last lineup in this formation (dd Mon), N changes", with replaced players marked.
      **On a fail,** the panel stays as it is.

      **Power (D59):** a team-match changes only when a pitch player is out (often 0-2),
      so most pairs are tied. About 1,000 team-matches detect a mean XI gain of about
      0.05 players if a fifth of them have a replacement. This is stated as an estimate,
      to be checked when the window opens.

      **Cost:** 0 API requests; one migration.

      **Built (2026-11-27), shadow only:**
      - `adatta/sim/formations.py`:
        - `replace_unavailable` (the rule);
        - `coded_starts`, the slot codes and lines each player started in, final by the
          run's cutoff;
        - `_squads`, P3-b's latest stored start probabilities and feed status per match
          and team, published by the run time.
      - The daily formation run stores `slots_replaced` and `replacements` beside
        `slots`. The API and the panel don't read them.
      - Migration 0019 is additive (two nullable columns) and was applied 2026-11-27.
      - **Tests:** 3 new in `tests/test_formation_view.py`.
      - **Verdict script built 2026-11-27, before any score** (`adatta/eval/pitch_replaced.py`;
        2 tests in `tests/test_pitch_replaced.py`).
        - It reads both pitches of the stored formation row as of kickoff − 24 h, against
          the announced XI (11 known starters). Slot accuracy only counts where the
          league-season is positional and the announced formation is the pitch's.
        - `count` prints the eligible number only. `judge` runs once: a marker in the
          store refuses a second reading.
      - **Window fixed now (D59: in advance; it read "to the winter break"):** kickoff
        from 2026-11-27 to before 2026-12-24.
        - With fewer than 1,000 eligible team-matches by then, it runs on a week at a time
          until it has them. Only the count is read, never a score.
        - The last two weeks (the international break) gave 218 eligible team-matches
          from 132 matches. A normal week should give about 170, so about 750 by
          2026-12-24, and the window will probably run into January.

- [ ] **P3-i Lineup offset with attack and defence, live shadow (D43). Closed (D44).** Owner, 2026-11-21:
      "any way we can have lineup projections and changes affect our match projections?",
      then "Live shadow, attack + defence".

      Acceptance (stated 2026-11-21 before building, owner: "Approve as drafted"). No P3-i
      or P3-i0 score has been computed. Already seen:
      - P3-d's dev result: L vs P +0.01% at lineups-known, +0.05% at T-24h, β about 0.15;
      - P3-d0's result;
      - P's live scores on the Played page (P only; nothing with a lineup term);
      - counts only: 3,758 regular-season bundle league fixtures in 2026/27, 1,160
        finished, 103 of them with both lineups captured before kickoff.

      **Why it can be judged honestly.**
      - D40's concern was variants fitted to the same dev seasons. P3-i is specified
        once, here, and scored only on 2026/27 matches.
      - No P3-i match score is computed on dev, at any horizon, at any stage.
      - The holdout is not opened (D38).

      **Expected power, stated now.**
      - P3-d's dev interval on 1X2 log loss was about ±0.04% on 18,682 matches.
      - On the roughly 3,700 live matches below, it will be about ±0.09%.
      - So P3-i passes only if it gains about 0.1% or more. That is ten times what
        attack alone gained on dev.
      - Expect a fail unless the defensive half carries real signal. A fail closes the
        question for this season.

      **P3-i0: defensive player rating, checked on dev on its own** (register B.1,
      with-without-you; 0 requests).
      - **Data.** A stint table from the stored detail payloads: each team-match is cut
        at every substitution and red card, in normal time (minutes 0-90). Each stint
        records its length, the players on the pitch, and goals conceded (own goals
        count against the team that conceded them). It is rebuilt from raw payloads
        (principle 4) and stored next to `players/appearances`.
      - **Expected goals conceded.** E_s = (stint length / 90) × the opponent's λ in the
        match. λ is P's posterior mean from the latest P refit of the bundle before the
        rating refit (its in-sample training rates, as S's in P3-d amendment 4). Only
        matches covered by a P refit are used, which means 2019/20 on.
      - **Model.** Per player i, a ratio θ_i ~ Gamma(k_line, k_line) with Poisson goals
        conceded: G_s ~ Poisson(E_s × θ_i). θ_i's posterior mean is
        (k + G_i) / (k + E_i), where G_i and E_i sum over the stints the player was on
        the pitch for. k_line (G / D / M / F) is the maximum-marginal-likelihood fit,
        in closed form as P3-d0. The rating is as of the refit, on P3-d0's 28-day grid
        (2,000 draws are not needed: no sampling).
      - **Known bias, disclosed.** Substitutions follow the score, so stints are not
        chosen at random. This affects the standalone check only; P3-i itself uses the
        announced XI, chosen before kickoff.
      - **Dev check (gate).** On dev seasons 2019-2023, walk-forward on the 28-day grid:
        - R0 predicts each held-out stint's goals conceded as E_s;
        - R1 predicts E_s × exp(mean over the 11 on the pitch of log θ_i).
        - It passes if R1's Poisson log loss beats R0's, with the paired 95% interval by
          team-match excluding 0.
        - A fail stops P3-i (it would have no defensive input) and is reported.

      **P3-i model (LD), defined once.**
      - **Attack.** Δ_A,T is P3-d's lineups-known construction unchanged: the P3-d0
        line-level goal quality, the announced XI and bench with P3-b's role minutes,
        and the mean over the team's previous 10 league matches as the baseline.
      - **Defence.** Δ_D,T = Σ_j w_j log θ_j − the same over T's previous 10 league
        matches. Here w_j = expected minutes / 990, over the announced XI and bench
        with P3-b's role minutes (keeper included). θ is the latest P3-i0 rating before
        kickoff - 24 h.
      - **Offset.** For each team's goal rate, one lineup term, entered once
        (principle 7):
        log λ_T += β_A × Δ_A,T + β_D × Δ_D,opp(T).
        Priors: β_A ~ Normal(1, 0.5), β_D ~ Normal(1, 0.5).
      - **Missing inputs.** Where an input is missing, its Δ is 0 and the team-match is
        flagged (rule 8). This covers no lineup, fewer than 3 earlier matches, or a
        player without a rating; newcomers follow P3-d amendment 3.
      - **Fit.** In the likelihood (§7.5), on P's model otherwise unchanged. Training
        rows use the lineups-known construction (P3-d amendment 8). 2,000 draws, seed
        20261105. Gate failures are redone once at target_accept 0.99 and 4,000 draws
        (P3-d amendment 9).
      - No other variant is fitted or scored: no serving-only version, and no attack-only
        or defence-only version.

      **Targets and horizons.**
      - **Targets.** Every 2026/27 regular-season match of the ten bundle leagues
        finished by the judgement date, from the season's start.
        - Lineups-known XI: the `lineup_snapshot` first seen before kickoff where
          captured (from 2026-11-07).
        - Before that, the final stored XI, as P3-d on dev. A late change after release
          is missed (rare).
        - The captured subset is reported separately.
      - **Refits.** P and LD are both refitted on the live weekly grid (D11), each bundle
        from the season's start. Both use the same data and seed, with history through
        2025/26 (holdout seasons as history only, never targets).
      - **Horizons.**
        - Lineups-known is primary.
        - T-24h is secondary. It uses P3-b's projected minutes from its live 28-day grid
          (as P3-e live). If P3-f passes on 2027-01-04, T-24h is also reported with
          P3-f's minutes on the matches it covers.

      **Pass (judged once, after the last 2026/27 regular-season matchday of the ten
      leagues).**
      1. At lineups-known, LD beats P in the D18 form: 1X2 log loss and RPS both better,
         each with the paired 95% interval excluding 0. No league × market segment is
         worse than 1.0% (D28, point estimate).
      2. Inference gates in every LD refit (R-hat ≤ 1.01, ESS ≥ 400, no divergences,
         PPC a warning as in 1b).
      3. Feasibility: LD's median refit time is no more than 1.5× P's on the same runner
         class.
      4. Leakage: on 100 sampled targets, nothing final or observed after the as-of time
         changes Δ_A, Δ_D, θ or any quality input.

      Reported, not gating:
      - T-24h vs P;
      - β_A and β_D over time;
      - the share of team-matches with Δ = 0, by league;
      - matches with the biggest lineup changes (top 10% of |Δ|);
      - by league and month.

      **Interim reports** (monthly): coverage, gates, timing, leakage, β_A and β_D only.
      No 1X2 or RPS comparison of LD vs P is computed before the judgement date, so
      nothing can be tuned on it.

      **Then.**
      - **On a pass:** LD gets a serving acceptance of its own: a lineups-known forecast
        milestone after lineups are captured, and T-24h use only if T-24h was no worse
        than P.
      - **On a fail:** closed and disclosed; P stays production.
      - In neither case is anything served before the verdict. The dashboard keeps
        saying the forecasts don't use lineups.

      **Cost.**
      - 0 API requests.
      - P3-i0: the stint table build, plus a closed-form dev check (minutes on CI, no
        sampling).
      - The live shadow: about 42 weekly refits × 5 bundles × (P + LD). At P3-d's timing,
        that is about 50 CI hours over the season, run as monthly batches.
      - One backtest-type run at a time.

      **Build, P3-i0 (2026-11-21), before any P3-i0 score:**
      - **Code:**
        - `adapters/api_football_stats.stints`, the stint parser. Own goals in the feed
          are listed under the side credited (checked on 15 Premier League 2022 own
          goals), so every goal is conceded by the other side.
        - `features/stints.py` and `adatta stints-build`, the stint files.
        - `eval/defence.py`: exposure, lines, the k fit, ratings, scores and the gate.
        - `model/defence.py` and `.github/workflows/defence.yml`: `lambdas`, one job per
          bundle, and `assemble`.
      - **Stint files built** for the ten bundle leagues, 2016-2026 (0 requests), with
        79,269 team-matches in all. Dropped:
        - 562 team-matches with a substitution that couldn't be followed;
        - 24 whose stints don't add up to the 90-minute score;
        - 81 matches without two full XIs.
        Holdout and live seasons are built for P3-i but not loaded by the dev check.
      - **P's dev refits:** all 928 come from the P3-d dev run's cache, under the current
        fit hash (8 of 8 sampled were found, each with its training rates). The job stops
        rather than refit P if one is missing. Each refit's training rows are matched to
        match ids with the same filter and sort as `team_strength.prepare`, reordered
        only within one kickoff time, and checked against the fit's teams and goals.
      - **Clarifications while building, before any score:**
        1. *Training window.* The training stints for a rating refit are the matches in
           the training window of the latest P refit of each bundle (1,095 days), which
           reaches back to 2016. The draft's "2019/20 on" was wrong: every match in a
           dev P refit's window has a rate.
        2. *Scored per team-match.* The dev check scores each team-match's normal-time
           goals conceded: R0's mean is the opponent's expected goals, and R1's is that
           mean times the minute-weighted sum over its stints. It is the same model as
           per stint, summed. The reason is that two changes in the same minute make a
           stint of zero length, which can hold a goal. Paired by team-match as stated;
           the interval clustered by match is reported.
        3. *R0's expected goals.* These are the marginals of P's posterior-mean score
           matrix from the weekly refit before the match (out of sample). The training E
           uses P's in-sample rates, as stated.
        4. *Targets and lines.* A target uses the latest rating refit at or before
           kickoff - 24 h. Ratings pool a player's stints across bundles. A player's line
           is his most frequent position in appearances final by the refit; one with
           none is rated as M, and those are counted.
      - **Tests:** `tests/test_defence.py` (6), covering:
        - stint cuts, credited goals, extra time and unfollowable substitutions;
        - the 90-minute score check;
        - the closed-form rating and k;
        - lines as of the cutoff;
        - R1 against R0 and the gate;
        - matching the fit's training rows.

      **Result, P3-i0 dev check (2026-11-21, run 44882446126): passes by the letter, but
      the pass is degenerate; raised to the owner, P3-i on hold.**
      - **Scope:** 38,307 team-matches (19,228 matches), 61 rating refits. All 928 P
        refits were found in the cache (England 186, Spain 203, Italy 181, Germany 179,
        France 179).
      - **Gate:** R1 - R0 Poisson log loss per team-match is -0.000004 [-0.000006,
        -0.000002], about -0.0003% of R0's 1.43179. The interval clustered by match is
        the same. By season: 2019 to 2022 between -0.000004 and -0.000007, and 2023
        +0.000002.
      - **Why it is degenerate:**
        - k sits at the top of its grid (20,000) for every line in all 61 refits. The
          marginal likelihood finds no player-level variation, so every θ is about 1.
        - The minute-weighted lineup shift has sd 0.0001 (5-95%: -0.0003 to +0.0001),
          against a team-match log loss of 1.43.
        - With k unbounded, R1 would equal R0 and the interval would be 0, a fail. The
          sign of the "pass" comes from where the grid stops.
        - Served in P3-i, Δ_D would be about 0, and LD would be P3-d's attack-only
          offset, which failed.
      - **Diagnosis (England, last dev P refit, players with E > 5; aggregates only).**
        Goals conceded on the pitch against expected have a dispersion index of 0.856
        with P's in-sample rates, the approved choice. The walk already fits each team's
        goals conceded, so nothing is left over for players. With P's out-of-sample
        forecasts it is 1.108. That excess may still be team-level (shared by
        team-mates) rather than player-level.
      - **Not done:** no respecification has been run. Whether to run one, disclosed,
        or close P3-i is the owner's call.

      **Closed 2026-11-21 (owner: "Close P3-i", D44), no respecification.** Not ticked:
      the rating carries no player-level signal, so the live shadow would be P3-d's
      attack-only offset again. No live shadow is started. P stays production, and the
      dashboard keeps saying the forecasts don't use lineups. The stint files and the
      rating code stay as built (0 requests to rebuild).

- [ ] **P3-j Lineup strength from three signals, in the likelihood (§7.5, principle 7).**
      Drafted 2026-11-27 before building (owner: "Yes, that order"; the three signals
      chosen 2026-11-27, Transfermarkt values declined). **Approved 2026-11-27** (owner:
      "pushed and approve", D65), the recommended choice in each.

      **Why, and what is different from P3-d and P3-i.**
      - P3-d summed player shares of expected goals. Trained on the minutes actually
        played, it learned a negative weight: substitutions depend on the score, so the
        result leaked into the input. Trained on the announced XI, it gained nothing
        (L vs P +0.01%).
      - P3-i's defensive rating was degenerate (k at its grid edge).
      - P3-j uses three different signals, each built only from matches final before the
        as-of time. It always describes the **announced or projected starting XI**,
        never minutes played in the match itself. Each signal must first show it predicts
        goals at all, alone, on dev.

      **Seen so far, for this draft (counts only):** provider ratings exist for 98-99% of
      appearances with minutes, in every bundle league and season checked (2016-2025);
      minutes on and off exist for all. Team xG is match-level only, so plus-minus is
      minutes-weighted, not built from stints.

      (a) **The signals, per team-match, as of the forecast's as-of time.**
      "Usual XI" is the team's eleven players with the largest minute shares over its last
      10 league matches final by then.
      1. **Missing-starter share S_miss:** 1 − (the XI's summed minute shares ÷ the usual
         XI's summed minute shares). 0 means the usual XI; larger means more of it absent.
      2. **Adjusted plus-minus S_apm:** a ridge regression over the bundle's team-matches
         of the last 365 days final by then.
         - Target: team xG minus opponent xG, per 90.
         - Design: each player's share of the 90 minutes (+ for the team, − for the
           opponent), plus a home term.
         - The ridge penalty is chosen by time-ordered cross-validation inside dev
           (owner choice 2), then fixed.
         - S_apm is the sum over the XI of each player's coefficient minus the same sum
           over the usual XI.
      3. **Rating gap S_rat:** each player's mean provider rating over his last 10
         appearances final by then, shrunk towards his line's mean with 5 pseudo-matches.
         S_rat is the XI's mean minus the usual XI's mean.

      The XI is the announced lineup at the lineups-known horizon (T-60m) and P3-b's
      projected XI at T-24h. A team without an announced XI at T-60m uses the projected
      one, flagged.

      (b) **Step 1, dev signal check, each signal alone:** dev 2019/20-2023/24, the five
      bundles, the announced XI.
      - Test: the Poisson slope of a team's goals on the signal, with the team-season mean
        as offset (P3-d's diagnosis method).
      - A signal passes if its slope has the expected sign (negative for S_miss, positive
        for S_apm and S_rat), with the 95% interval excluding zero.
      - Signals that fail are dropped. With none passing, P3-j stops here and the result
        is reported.

      (c) **Step 2, the model on dev:** the production spec, P, with one lineup offset in
      log λ for each side:
      - the offset is Δ = Σ_k β_k · S_k over the signals that passed, with the opponent's
        Δ entering its own rate. It is in the training likelihood, exactly once (principle
        7).
      - Priors: β_k ~ Normal(0, 0.5) on standardised signals.
      - The dev walk-forward runs on P's weekly grid at both horizons.
      - The inference gates apply to every refit; one redo at target_accept 0.99 and 4,000
        draws (as A3).
      - **Dev decides only whether to go live:** at T-60m the point estimates of 1X2 log
        loss and RPS must be better than P's, with no league × market segment worse than
        1.0%. Dev is the tuning window, so it does not promote.

      (d) **Step 3, judged live (D38: the live 2026/27 period).** Shadow refits and
      forecasts at T-60m and T-24h. They are stored and not shown.
      - **Window:** from the first shadow forecast to the end of 2026/27, the five bundles'
        league matches, read once (owner choice 3).
      - **Gate:** D59 against P at T-60m, either route 1 (1X2 log loss and RPS improve,
        intervals excluding zero) or route 2 (both better, upper bounds under +0.10%).
        The usual safeguards apply: segments, calibration, inference gates, runtime.
      - T-24h is reported.

      (e) **On a pass:** P3-j serves from T-60m. The forecast moves when the lineup is
      announced, and the Lineup certainty check becomes "modelled". T-24h is served only
      if it also passes. **On a fail:** nothing changes.

      **Power (D59), an estimate, recomputed from step 2's dev pairs before the window:**
      at about 1,800 league matches in the window, with a per-match difference spread of
      about 0.04 (P3-d's order), the detectable gain is about 0.25% at 80% power. Published
      lineup effects at T-60m are usually of that size or larger.

      **Cost:** 0 API requests (lineups, ratings and minutes are stored). Step 2 is a
      P3-d-sized dev backtest (about 100 jobs), one backtest-type run at a time, never on
      a Monday. The shadow adds one refit set a week.

      **Owner choices:**
      1. **Usual XI:**
         - (a) the last 10 league matches (recommended);
         - (b) season to date.
      2. **The ridge penalty for S_apm:**
         - (a) chosen by time-ordered cross-validation inside dev, then fixed
           (recommended);
         - (b) one fixed value, stated now.
      3. **Live window:**
         - (a) to the end of 2026/27, read once (recommended);
         - (b) D59's default half-season, to the winter break. The shadow could only start
           after step 2, in about two weeks, so that window would be short.

      **Step 1 run, 2026-11-27** (`adatta/features/lineup_signals.py`, run locally, 0 API
      requests, about 1 minute). Dev 2019-2023, the five bundles: 38,508 team-matches,
      announced XI at T-60m.

      | signal | n | slope | 95% interval | per sd | expected | result |
      |---|---|---|---|---|---|---|
      | S_miss | 38,365 | −0.102 | [−0.178, −0.026] | −1.2% goals | − | pass |
      | S_apm | 7,368 | +0.002 | [−0.141, +0.145] | +0.0% | + | fail |
      | S_rat | 38,365 | +0.459 | [+0.216, +0.703] | +1.6% goals | + | pass |

      - **S_apm fails, and is dropped as (b) states.** It can barely be tested: the stored
        team xG starts in 2022 (965 of 3,905 bundle matches that season, 2,302 of 3,832 in
        2023, none before). A 365-day xG window therefore exists only for 2023's
        team-matches, one season instead of five. This limit was not seen when drafting;
        the draft checked rating and minute coverage, not xG by season. The result is
        recorded as a fail and not rerun with another target.
      - **Ridge penalty** by time-ordered CV: 30. Next-week xG-difference MSE over 2,246
        matches: 1.599 (λ 1), 1.285, 1.113, **1.065 (λ 30)**, 1.102, 1.208, 1.340 (λ 1000).
      - **Correlations:** S_miss ~ S_rat −0.335, S_miss ~ S_apm −0.025, S_apm ~ S_rat
        +0.231.
      - **Effects are small:** about 1-2% of a team's goals per standard deviation. A fully
        changed XI (S_miss = 1) is about −10%.
      - **Step 2 uses S_miss and S_rat.**
      - Signals table `features/lineup_signals/v1/0d625a62f5d310dd` (deterministic: two runs
        give the same digest; its manifest holds the
        code SHA, λ, the line means and the library versions).

      **Step 2 built, 2026-11-27, not run** (owner: "Yes, build step 2"). Stated before
      any step 2 score:
      - **Signals in J:** S_miss and S_rat, each divided by its sd over the warm-up
        team-matches (before 2019-07-01; 0.1131 and 0.0322), 0 where absent (rule 8).
        - Training rows read every match's announced-XI signals at T-60m (pre-kickoff
          facts, as P3-d's amendment 8).
        - J_lk targets read the same. J_24 targets read the signals of P3-b's projected
          XI: the 11 likeliest starters in P3-b's pool (selection report 44406512773, the
          export P3-d used), as of kickoff − 24 h.
        - The table is `features/lineup_signals/v2/021eb8f8c77ed8f1` (2016-2023; the same
          digest from two builds, manifest code 5ac8f2e): 99.6% dev
          coverage at both horizons. A rebuild of 20 sampled dev team-matches, from the
          matches before kickoff plus the match with its minutes and ratings removed,
          changed 0.
      - **J** (`adatta/model/lineup_strength.py`) is P with Δ = β_miss·S_miss + β_rat·S_rat
        added to each side's log rate, before every term that reads the rates (goals,
        Dixon-Coles, draw inflation, xG), with β_k ~ Normal(0, 0.5).
        - It is written out in its own file, so P's file and P's cached weekly dev refits
          (P3-d's run, the same fit hash) stay valid. Only J is fitted.
        - A CI test checks that J's log density equals P3-d's lineup model with that
          offset (β_lineup = 1), up to the two priors.
      - **Rule** (`adatta/eval/lineup_j.py`), to go to the live shadow:
        1. at T-60m, J's 1X2 log loss and RPS point estimates beat P's;
        2. no league × market segment (1X2 log loss and RPS, O/U 2.5, BTTS, total-goals
           CRPS) is worse than +1.0%;
        3. every J refit passes the inference gates, after one redo at target_accept
           0.99 and 4,000 draws;
        4. J's median refit is within 1.5× P's, as P3-d;
        5. the signals leakage check passes.

        T-24h is reported. The power statement for the live window is recomputed from the
        dev pairs (J_lk − P per match, at 1,800 matches).
      - **Run:** `lineup_j.yml`, stage fit: 5 bundles × 5 dev seasons × 4 parts = 100
        jobs, J only, about half of P3-d's compute. Then stage redo for any failing J
        refits. One backtest-type run at a time, not on a Monday.

      **Step 2 run, 2026-11-27: go to the live shadow.** Fit run 46009315685 (101 jobs),
      redo run 46072332080 (10 J refits redone at target_accept 0.99 and 4,000 draws). Dev
      2019-2023, the five bundles, 18,682 warm matches. Negative means J is better.

      | check | value | 95% interval | pass |
      |---|---|---|---|
      | J vs P at T-60m, 1X2 log loss | −0.07% | [−0.11%, −0.04%] | yes |
      | J vs P at T-60m, RPS | −0.12% | [−0.18%, −0.07%] | yes |
      | worst league × market segment (of 50) | +0.20% (one league, O/U 2.5) | | yes |
      | inference gates, every J refit | 0 failing after the redo | | yes |
      | J median refit ÷ P's | 1.11 (488 s vs 438 s) | | yes |
      | signals leakage check | 0 of 20 rebuilt changed | | yes |

      - **T-24h (projected XI, reported):** −0.03% [−0.06%, +0.00%] log loss, −0.05%
        [−0.10%, +0.00%] RPS.
      - **By league:** 8 of 10 better on both scores at T-60m; one is +0.00% and one is
        +0.04%.
      - **By season:** 2020-2023 better (−0.04% to −0.14% log loss); 2019 is +0.02%.
      - **Fits:** J's worst R-hat is 1.010, min ESS 467, 0 divergences. β_miss median
        −0.021 per warm-up sd (range −0.045 to +0.010); β_rat median +0.010 (−0.018 to
        +0.034). These are small, as step 1 showed.
      - **Power for the live window, recomputed:** the per-match J − P log loss sd is
        0.0267 over the dev pairs. At 1,800 live matches the detectable gain is 0.17%
        (80% power, two-sided 5%), so dev's −0.07% would most likely not clear route 1.
        Its 95% half-width at 1,800 matches is about 0.12%, so if the live gain matches
        dev's, the upper bound would be about +0.05%, under route 2's +0.10%.
        - Route 2 is therefore the realistic way to pass. This is stated before any live
          score; the gate is unchanged.

      **Step 3 build plan, drafted 2026-11-27. Approved 2026-11-28 (owner: "sure go ahead", comparator (a)).** Stated before building and
      before any live score.
      1. **Live J refits** (`adatta/model/lineup_j_live.py`, a new job in `serve.yml` after
         P's refits):
         - J is fitted per bundle at every as-of time P is refitted live: Mondays, plus the
           results triggers, since RESULTS_REFITS is on (D33).
         - Training signals are rebuilt from the stored appearances, matches final by the
           as-of time (as step 2's table).
         - The draws go under `serving/lineup_j/`, with a `model_run` row: component p3j,
           window shadow, the inference gates and one redo as in step 2.
         - Cost: about 1.1× P's refit time per refit, 0 API requests.
      2. **Signal basis, daily:** each team's usual XI (minute shares over its last 10
         league matches) and each player's shrunk prior rating, as of the run.
         - It is written by the same 04:41 UTC daily job as P3-b's projected lineups, so
           the capture tick does no history reads.
      3. **Shadow forecasts, in the capture tick:**
         - **J_lk:** on the tick that first sees both announced XIs (`lineup_snapshot`,
           before kickoff). S_miss and S_rat come from the announced XIs and the basis. A
           side whose XI is missing uses its projected XI, flagged (as (a)).
         - **J_24:** at the T-24h milestone, from P3-b's projected XI as of then.
         - Both J and P are projected from their latest passing refits of the same as-of
           time. P's draws there are the served model's, so each pair differs only by the
           lineup offset.
         - Stored: the 1X2, O/U 2.5, BTTS and total-goals probabilities 0-10+, and the
           four signals.
      4. **Storage:** `lineup_j_shadow` (a new forward-only migration), append-only, one
         row per match × horizon × model. The API never reads it, and a guard test checks
         this, as the odds guard does.
      5. **Judgement:** `config/lineup_j_live_gate.yml` is written now, before any score,
         holding (d)'s gate, the segments and the power statement above. It is read once
         after 2026/27 ends (`python -m adatta.eval.lineup_j_live`), on the five bundles'
         league matches with a J_lk pair.
      6. **Tests:** J's offset is applied once and only from pre-kickoff facts. A lineup
         seen after kickoff gives no J_lk. A refit from the future is refused. The pairs
         share an as-of time. Youth sides follow D60, with no filter.

      **Owner choice, J's comparator:**
      - (a) **J vs P projected from the same as-of refit in the shadow step, at the same
        moment (recommended).** This is the served model's draws, paired exactly.
      - (b) J vs P's served T-60m milestone in `market_projection`. That is closer to
        "what was shown", but it holds O/U only at 1.5/2.5/3.5 (no total-goals CRPS), and
        P's T-60m is up to about an hour earlier than J_lk.

      **Timing:** about a day to build and test. The first J refit would come with the
      next serve run after approval, and shadow forecasts with that week's fixtures. Every
      week of delay shortens the window, which ends with the season.

      **Step 3 built, 2026-11-28, not deployed** (comparator (a)). As drafted above, with
      these details fixed before any live score:
      - **Live J refits** (`adatta/model/lineup_j_live.py`; `serve.yml` job `lineup_j`,
        after P's refits, one bundle at a time).
        - J is fitted at the as-of time of each passing P live refit not yet matched. The
          first run fits only each bundle's latest, with no backfill. A failed J refit is
          recorded and not retried.
        - Training signals are step 2's `signals_at` over the window, with step 2's
          warm-up scales and line means (table `021eb8f8c77ed8f1`).
        - Each refit's manifest records whether its training rows hash to P's
          (`same_training_as_p`) and the share of rows with a signal.
      - **Daily basis** (`adatta/sim/lineup_j_shadow.py basis`, `projected_lineups.yml`
        after the appearances rebuild):
        - each team's minute shares over its last 10 league matches, and each player's
          rating history at his last rated appearance, final by the run;
        - a basis older than 2 days, or dated after the tick, is reported and no row is
          written.
        - A test checks that the basis plus an XI give step 2's `signals_at` exactly
          (to 1e-12) for the same match.
        - What differs from dev: a match final between the 04:41 run and kickoff isn't
          in the basis. A team rarely plays twice in a day.
      - **The tick step** (`run_capture`, `lineup_j_shadow`, numpy and polars only):
        - lk is written on the first tick that sees both announced XIs, observed before
          kickoff. At the last call (20 min before kickoff), a side still without one uses
          P3-b's latest projected XI, flagged; with neither, its signals are 0 (rule 8).
        - 24 is written in the T-24h milestone window, from P3-b's run at or before T-24h.
        - J's matrices are P's projection path (`serve.fixture_matrices`, with the D34
          walk) plus β·x on each side's log rate. P's row is the served model's
          projection from the same refit.
        - The tick module loads without the model group.
      - **Storage:** `lineup_j_shadow` (migration 0020), unique per match × horizon × model.
        It holds the signals and the posterior-mean 11×11 matrix. A test checks that
        nothing in `adatta/api` reads it.
      - **Gate:** `config/lineup_j_live_gate.yml`, written now. The judge
        (`adatta/eval/lineup_j_live.py judge`) refuses before 2027-06-15 and after a
        stored verdict.
      - **Tests:** `tests/test_lineup_j_shadow.py` (10) and `tests/test_lineup_signals.py`
        (live parity) cover:
        - pairs from the same refit, and P equal to the served projection;
        - one row per match × horizon;
        - a lineup seen after kickoff never read;
        - rule 8 giving J = P;
        - T-24h from P3-b's projection in its window;
        - no pair, or a stale basis, writing nothing;
        - J following P's refits;
        - the redo;
        - training alignment;
        - the scores and routes;
        - read once.
      - **To go live:** apply migration 0020 to the database, push, then run
        `projected_lineups` once (the basis) and `serve` once (the first J refits, about
        40 min for the five bundles).

- [ ] **P3-k Team strength from penalty-free xG (§5.2; improvement 5, 2026-11-27).**
      **Drafted 2026-11-28, not approved (D71).** Stated before building and before any
      score.

      **Why.** P's xG term (each side's xG ~ Gamma around its goal rate) reads the
      provider's match xG, and that xG includes penalties. On dev 2022-2023, 6,534
      team-matches with xG:

      | penalties taken in the team-match | team-matches | mean xG |
      |---|---|---|
      | 0 | 5,659 | 1.26 |
      | 1 | 811 | 2.13 |
      | 2 | 63 | 2.90 |

      - A penalty adds about 0.86 xG: the kick, plus the attack that won it.
      - Penalties come fairly randomly, about 0.14-0.17 per team-match (dev 2019-2023),
        and are 8-10% of goals.
      - So an awarded penalty moves a team's rating through xG as if it had played much
        better. Its goals are unchanged and still count.
      - The numbers above are dev seasons only. A first count also read 2024-2026 penalty
        counts and xG levels (descriptive, no model or score); they are not used.

      **What changes (challenger Q):** P with each side's xG replaced by xG − 0.76 × the
      penalties it took, where the count is from the stored appearances (scored plus
      missed). The goal likelihood is unchanged. Nothing else changes.
      - A side whose penalty count is missing has no xG row (rule 8: no imputed value).
      - The penalty rate itself, as a term of its own, is left for the penalty market
        (§8.3); it is not in Q.

      **Acceptance:**
      - **Dev:** the walk-forward on dev 2022-2023, the only dev seasons whose refits see
        xG, with P read from its cached refits and only Q fitted. Q goes live only if its
        1X2 log loss and RPS point estimates beat P's, with no league × market segment
        worse than 1.0%, the inference gates on every refit, and the runtime within
        1.5× P's.
      - **Live:** D59 against P, route 1 or 2, on a pre-registered 2026/27 window.
      - **Power:** recomputed from the dev pairs before the window. The plausible gain is
        small (the xG term is one signal among several), so route 2 is the likely route.
      - Under D70, Q and P3-j would be a family (both challenge the served 1X2 forecast),
        so each would be read at 97.5% if both are judged on 2026/27.

      **Cost:**
      - 0 API requests: the penalty counts are in the stored appearances.
      - A dev backtest of about 40 jobs (2 seasons × 5 bundles × 4 parts), Q only, about
        half of P3-j step 2's run.
      - One backtest-type run at a time, never on a Monday: after the cards dev run.

      **Owner choices:**
      1. **The penalty's xG:**
         - (a) 0.76, the usual public value, fixed now (recommended): the provider gives
           no per-shot xG, so its own value can't be read;
         - (b) estimated on dev 2022-2023 as the regression coefficient above (0.86,
           which also counts the attack that won the penalty).
      2. **Live window:**
         - (a) to the end of 2026/27, read once, shared with P3-j's season-end reading
           (recommended);
         - (b) D59's half-season default.

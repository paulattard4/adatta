# Phase 1b — production team model (spec §5, §8.1, §12, §14.5–14.6, §19.4)

Goal: replace the static benchmark with the production team model, i.e. Layers 1–2 synthesised
through Layer 6 as a Dixon-Coles bivariate Poisson, with full posterior simulation and
intervals. It is promoted only by the pre-registered holdout gate. The Phase 1a static model
stays the permanent floor (§14.4). Spec estimate: 60–100 h (§19.5).

Tick each item only when its acceptance check passes.

## Acceptance test (stated before building, per CLAUDE.md)

Order matters (§14.5): the diagnostics gate first, the backtest comparison second.

1. **Inference gate (§14.5)** on every production-configuration fit in a fixed audit sample
   (one refit per bundle per dev season, seeds logged):
   - R-hat <= 1.01 for every parameter; bulk and tail ESS >= 400.
   - Divergences: 0 after tuning (target_accept may be raised; the value is recorded).
   - Posterior predictive checks per league: total goals, home and away goal counts, draw
     rate, and the goal-count dispersion (variance / mean) each inside the central 90% of
     the PPC distribution.
   - Identifiability: attack and defence centred per time step; no posterior correlation
     > 0.9 between a league's mean level and its team effects.
   - Prior sensitivity: refit with every prior scale x0.5 and x2. Dev 1X2 log loss moves
     < 0.2% relative and no headline posterior mean moves > 1 posterior sd. The log-loss
     part is measured on the dev walk-forward (task 4), not on one fit's next 4 weeks;
     the gate script reports that 4-week figure as a non-blocking warning (decided
     2026-11-06).
   - Score-matrix truncation: tail mass < 1e-4 for 99.9% of predictions.
   - Reproducibility: the same seed and data give identical draws.
2. **Dev window (2019/20–2023/24), iterate freely:** 1b beats 1a on 1X2 log loss and RPS,
   paired 95% interval excluding zero, overall and for the England bundle. Each added
   component (AR(1) form, xG auxiliary, manager-change widening) is kept only if it improves
   dev log loss on the segments where it applies. Ablations are recorded in `model_run`.
3. **Locked holdout (2024/25–2025/26), opened once,** with the gate pre-registered in
   `config/evaluation.yml` (`phase1b_gate`, D18): any gain on 1X2 log loss and RPS vs 1a,
   paired 95% interval excluding zero, and no league x market segment worse than 1.0% relative. 1a is scored on the holdout in the same run. The outcome is recorded
   as `champion` or `rejected` in `model_run`, with reasons (§14.6).
4. **Calibration:** reliability per 1X2 outcome reported for dev and holdout. Recalibration,
   if ever wanted, is a separate challenger (§14.7).
5. **Serving:** posterior-predictive simulation from stored draws writes `market_projection`
   rows (probability + 90% interval) under a `projection_run` with the full §14.8 manifest.
   No live NUTS on request (§12.1).

## Decisions (all resolved 2026-11-05)

- **D9 Compute host — resolved: GitHub Actions** (free on the public repo; matrix jobs of
  bundle x season, up to 20 in parallel, 6 h per job). Logs are public: metrics only.
- **D10 Inference engine — resolved:** PyMC model, NumPyro (JAX) sampler, ArviZ diagnostics.
- **D11 Refit cadence — resolved:** weekly refits in production and backtest; daily
  resimulation from stored draws.
- **D12 Manager data — resolved:** probe `/coachs` coverage first; build only if adequate.
- **D13 Local model runs — resolved:** Linux Docker (`scripts/model.sh`); the Intel Mac
  can't install JAX or numba.
- **D14 Dev iteration cadence — resolved:** refit every 4 weeks while iterating (all models
  on the same grid); weekly for the final dev run and the holdout.

### D15 strength + openness: acceptance test (stated 2026-11-06, before building)

Dev window only, 28-day refit grid (D14), every model on the same grid:
- **Phase 1a probe** (static, minutes): adding an openness prior to the static model raises
  the actual-on-predicted total-goals slope towards 1 and improves OU2.5 log loss and total
  CRPS, with 1X2 log loss not worse than 0.1% relative. This tests the mechanism only; the
  1a benchmark itself stays as tuned (D7) unless a later decision changes it.
- **Phase 1b model:** the §14.5 total-goals dispersion PPC passes for both England leagues at
  2023-08-07 and every other gate check still passes or is no worse; then, once the harness
  runs 1b (task 4), dev OU2.5 log loss and total CRPS improve and 1X2 log loss is not worse
  than 0.5% relative vs the independent-prior 1b model.
- **Phase 1a probe result (2026-11-06): pass.** Static model, half-life 365, prior sd 0.5,
  18,601 warm dev matches, openness sd grid {none, 0.4, 0.25, 0.15, 0.1, 0.05}. Best
  0.15: slope 0.53 -> 1.04 (second tiers 0.24 -> 0.47); OU2.5 log loss -0.51% (paired 95%
  interval excludes 0), total CRPS -0.60% (excludes 0), BTTS -0.34%, 1X2 log loss -0.04%
  (interval includes 0, so no worse). Second tiers remain overspread: open item.
- **Phase 1b gate result (2026-11-06, England as of 2023-08-07, 4 x 1,000 draws): the
  dispersion PPC now passes in both leagues** (Championship quantile 0.006 -> 0.088, PPC
  median 1.057 -> 1.018 vs observed 0.971; PL 0.73). The data shrink openness hard:
  sigma_open 0.058 vs sigma_strength 0.28, openness walk sd 0.005 vs 0.028. Other checks
  unchanged or still passing: 0 divergences, prior and predictive sensitivity pass, seed
  reproducible, identifiability |corr| <= 0.13. Bulk ESS minimum fell 718 -> 407
  (sigma_strength, just above 400). Still failing, as before: one innovation at R-hat 1.011
  (was 1.010) and truncation at max_goals 10 (85%). Fits 13–18 min. Remaining D15 test:
  the dev harness comparison once task 4 exists.
- **Rerun (2026-11-06) with max_goals 15 and 2,000 tuning steps:** truncation passes (100%
  of predictions under 1e-4), dispersion PPCs still pass (Championship 0.084, PL 0.72),
  sensitivity and reproducibility pass. Longer tuning did not fix mixing: R-hat 1.015 on
  sigma_walk_open (3 of 16,280 over 1.01), bulk ESS 399 on sigma_strength, tail ESS 346.
  The slow parameters are now the hyperparameters; sigma_walk_open sits near zero
  (posterior ~0.005), a funnel more tuning doesn't remove. 18–19 min per fit; the first
  attempt exited silently after the third fit (likely out of memory in the container) and
  the report was rebuilt from the saved fits with --reuse.
- **Static openness (2026-11-06, decided after the rerun above):** openness no longer
  walks; strength still does. Gate 20/22: mixing now passes (R-hat <= 1.008, 0 of 8,221
  over 1.01; bulk ESS >= 586, tail >= 624; 0 divergences), truncation, all PPCs (Championship
  dispersion 0.095), identifiability, parameter sensitivity and reproducibility pass.
  sigma_open 0.075 +/- 0.021 vs sigma_strength 0.285. Fits ~8 min (half the walk version).
  **Failing: predictive sensitivity**, 1X2 log loss 1.0076 -> 1.0101 (x0.5, +0.25%) and
  -> 1.0109 (x2, +0.33%) vs the 0.2% limit. The gate measures it on the 87 matches in the
  4 weeks after the as-of date; the acceptance test says dev log loss, which needs the
  harness (task 4). Resolved: measured on dev log loss; the 4-week figure is a warning.
- Failing either: revert to the spec model and record the misfit as known (affects totals
  markets, not 1X2).

### D16 promotion/relegation shift: acceptance test (stated 2026-11-06, before building)

Measured on dev only, on the D14 28-day refit grid, warm matches; "mover matches" are a
promoted or relegated team's matches in its first season in the new tier.
- **Bias:** expected-minus-actual points per team-match for promoted and for relegated
  teams, in matches 1–10 and 11+, each within ±0.05 with a 95% interval that includes 0
  (today: −0.16, −0.09, +0.06, +0.12). Established teams stay within ±0.03.
- **Accuracy:** 1X2 log loss and RPS on mover matches improve (paired 95% interval
  excludes 0); overall dev 1X2 log loss is not worse than 0.1% relative, and no league is
  worse than 0.5% relative.
- **Sense check:** delta_up < 0 and delta_down > 0 in every bundle, or the bundle's
  posterior interval includes 0 (few movers); reported per bundle.
- **No leakage:** mover status for a match at as-of t uses the team's tier in the previous
  season's results and the target fixture's own league only (unit-tested).
- **Order:** first a Phase 1a probe (static model, an optional learned shift; minutes), as
  for D15. If it passes, add it to the 1b model and confirm with the dev backtest (task 4
  workflow) against 1b without it; the §14.5 gate must still pass. The 1a benchmark itself
  stays as tuned unless a later decision changes it.
- **Phase 1a probe result (2026-11-06): partial pass, so it fails the test as stated.**
  Static model, half-life 365, prior sd 0.5, 28-day grid, all 5 bundles, 18,601 warm dev
  matches (4,872 involve a mover). At the pre-stated prior sd 0.2:
  - Bias (actual − expected points per team-match): promoted 1–10 −0.165 -> **−0.103 ±
    0.087 (fail)**; promoted 11+ −0.087 -> −0.048 ± 0.053 (pass); relegated 1–10 +0.044 ->
    −0.002 ± 0.092 (pass); relegated 11+ +0.121 -> **+0.091 ± 0.056 (fail)**; established
    teams −0.003 to −0.004 (pass).
  - Accuracy: mover-match 1X2 log loss −0.18% [−0.34, −0.02] and RPS −0.28% [−0.54,
    −0.02] (pass); overall 1X2 log loss +0.02% [−0.03, +0.07] (pass); worst league +0.11%
    (2. Bundesliga) (pass).
  - Signs: mean delta_up < 0 and delta_down > 0 in every bundle (pass): up −0.08 (Spain) to
    −0.19 (Italy), down +0.004 (Italy) to +0.22 (Spain).
  - Prior sd 0.1 removes less bias; sd 0.5 removes more but makes overall 1X2 log loss
    worse (+0.08%, interval excludes 0) and still leaves relegated 11+ at +0.077.
  - Reading: a fixed shift halves the bias and fixes the early-season relegated case, but
    relegated teams' underrating grows through the season and promoted teams stay
    overrated early. A single offset in a static model can't follow that drift.
- **Exception (decided 2026-11-06):** despite the partial probe pass, D16 goes to the 1b
  model, because the residual bias drifts through the season and 1b's weekly strength walk
  may follow it where a static offset can't. The 1b criteria are unchanged from above (bias,
  accuracy, signs, gate) and are judged on the dev backtest, 1b with vs without D16; 1a
  stays as tuned. If 1b also misses, D16 is recorded as failed.
- **1b gate result (2026-11-06, England as of 2023-08-07): fail, so D16 is recorded as
  failed** under the exception's terms. 18/20 gating checks: mixing (R-hat <= 1.009, ESS >=
  509), PPCs, identifiability of league levels, truncation and reproducibility pass; **prior
  sensitivity fails** (delta moves 2.3 posterior sds at x0.5, 1.4 at x2). Posterior delta_up
  −0.43 ± 0.10, delta_down +0.44 ± 0.10.
  - Cause: the shifts are not identified. Within a bundle, the strength gap between tiers is
    learnt only from teams that move (no cross-tier matches are modelled), so any
    "promoted down, relegated up" shift is the same thing as a bigger tier gap. With D16 the
    fitted gap (top-flight minus second-tier mean strength) falls from 0.58 ± 0.11 to
    0.26 ± 0.11, corr(delta_up, gap) +0.40, corr(delta_down, gap) −0.37; the identified
    part, (delta_up + delta_down) / 2, is 0.005 ± 0.056. The prior then decides how the
    movers' jump is split between gap and shift, which the sensitivity check catches.
  - Reading: the bias is evidence that movers' carry-over is miscalibrated, but it can only
    be fixed with information that pins the tier gap independently: cross-tier matches
    (domestic cups, already loaded) or Q_league (§10.1). The shift code stays in the model
    and harness, off by default, for that later test.
- Failing: keep the unchanged carry-over and record the bias as known until Q_league.
- Reports gain promoted / relegated segments so this stays visible for every later model.

## Tasks

### 1. Data for Layer 2 (D8 follow-through)
- [x] `team_match_stat` table (migration 0006): per match x team, xG, shots, shots on target,
      corners, cards, possession, from the stored R2 detail payloads (0 API requests).
      Temporal contract: usable from kickoff + 3h, like results; provider revisions are
      versioned, not overwritten. Loaded 2026-11-05 by `adatta stats-load`: 82,336 rows for
      41,168 matches (the other 8,308 finished matches, mostly cups, have no statistics
      block). New matches get detail and stats from the daily `detail` capture job.
- [ ] xG gating by the capability table per league-season (available from 2022/23 only for
      the Top-5, 2023 for the Championship; other second tiers mostly absent).
- [ ] Probe `/coachs` coverage (D12); if adequate, a `manager_tenure` table and a
      `manager_change` flag per team-matchweek.
- [x] Migration: allow `model_run.status` values `challenger` and `rejected` (§14.6), in 0006.

### 2. Model v1: Layers 1–2 on goals (PyMC)

First timing (2026-11-05, Docker, 4 cores): England bundle as of 2023-08-07, 2,808
matches, 51 teams, 158 weekly steps: 878 s for 4 x 1,000 draws, but 57 divergences, R-hat
1.02 and ESS 118 on sigma_walk / sigma_season. Sampling needs fixing before the gate.
After two fixes (zero-sum innovations over teams; the season reset expressed as
`reset_weeks` of extra drift, LogNormal(log 16, 0.5), with target_accept 0.95): 783 s,
0 divergences, hyperparameter R-hat <= 1.005, ESS >= 1,062, BFMI 0.94–1.01. Still open:
PyMC warns R-hat > 1.01 for some team-level parameters (not yet inspected), and
`reset_weeks` is prior-dominated (posterior 16.3 +/- 7.8 vs prior 16), which the prior-
sensitivity check must report.

- [ ] Priors as §5.1: mu_league ~ Normal(log 1.35, 0.3); sigma_attack, sigma_defence ~
      HalfNormal(0.5); sigma_home_adv ~ HalfNormal(0.3).
- [ ] Attack and defence as non-centred Gaussian random walks per matchweek, sigma_walk ~
      HalfNormal(0.15) (§5.2). The prior season's final state carries over with inflated
      variance at season transitions (soft reset, §5.3). Promoted and relegated teams carry
      over within the bundle (§10.3).
- [ ] Hierarchical home advantage: h_team ~ Normal(h_league, sigma_home_adv). Regular season
      only, so no neutral venues yet (play-offs and cups come later, with the neutral flag).
- [ ] Dixon-Coles rho with a weak prior, per bundle (§8.1).
- [ ] Draws stored as ArviZ netCDF in R2 (§12.1); trace retention per §12.3.

### 3. Diagnostics and simulation
- [ ] `adatta/model/diagnostics.py`: every check in acceptance item 1, as a report.
- [ ] Posterior-predictive score matrices give market probabilities with intervals; the
      full-time matrix is primary and halves are binomial thinning (§8.2), deferred until
      half markets are needed.

### 4. Walk-forward at production cadence

Acceptance test (stated 2026-11-06, before building):
- **Same grid:** for a bundle, seasons and cadence, the 1b runner's (match_id, as_of) pairs
  are exactly the 1a harness's pairs on the same cadence (one shared refit plan).
- **No leakage:** every 1b refit trains only on matches with kickoff + 3h <= as_of
  (asserted per refit and unit-tested); holdout seasons raise `HoldoutLocked`.
- **Resumable:** each refit's prediction draws are cached under (config hash, bundle,
  as_of); a rerun with a warm cache does zero fits (unit-tested with a fake sampler). The
  config hash covers the model code, sampler settings, prior scale, structure and
  `config/evaluation.yml`, so any change refits.
- **Scored like 1a:** the same metrics per match (1X2, OU2.5, BTTS, total CRPS) from the
  posterior-mean score matrix at max_goals 15, plus each refit's R-hat / ESS / divergences.
- **Report:** paired 1b vs 1a comparison per bundle and overall (relative change, 95%
  interval), cold-start matches reported apart, and the dev predictive prior sensitivity
  (x0.5 / x2 vs x1) when those runs exist. Logs print metrics only (public CI).
- **Smoke:** two consecutive England refits run locally end to end in Docker, and a rerun
  hits the cache.
- [x] Harness runs the 1b model as one more model on the same as-of grid. Each refit's draws
      are cached by (bundle, as_of, config hash), so reruns are resumable.
      `adatta/model/backtest.py` (2026-11-06): `harness.refit_plan` is the one grid both
      models use (1a predictions reproduced exactly after the refactor); the cache holds
      the prediction draws only (400 thinned draws of the final-step state, ~0.45 MB per
      refit as .npz; full traces stay a gate/debug artefact). Smoke: two England 2019
      refits, 532 s and 539 s, 0 divergences, ESS >= 926, R-hat 1.011 / 1.012 (recorded per
      refit; the gate threshold is 1.01); the rerun hit the cache (12 s, identical
      predictions). Assembly scores 1a on the same plan, flags any grid mismatch, writes
      paired comparisons, refit diagnostics and the dev predictive prior sensitivity, and
      can record `model_run` rows.
- [x] `backtest.yml` workflow (D9): manual dispatch, matrix of bundle x season, each job
      resumable from cached draws in R2, results written to Neon; a final job assembles the
      report. Built 2026-11-06, not yet dispatched. Parts and full reports go to R2; the
      only artifact is the metrics summary (public repo).

### First dev backtest (2026-11-06)

GitHub Actions run 42210383573: phase1b_strength_open, prior scale 1, all 5 bundles x dev
seasons 2019–2023, 28-day grid, 266 refits (~140 s each on the runners), 26/26 jobs green.
The CI assembly compared 1a on a joint 5-season grid while each 1b part was planned per
season, so up to 4 weeks separated the two models' fits for the same match (grid check
said NO). Fixed (assembly now plans 1a exactly as each part was planned) and re-assembled
locally from the R2 parts: `reports/phase1b/backtest/20261106T124229Z` (grid identical).
The `model_run` row written by the CI run held the uncorrected comparison; it was deleted
on 2026-11-06, with that run's R2 parts and report (the 1,000-draw fits stay cached in R2).

Results, 18,591 warm matches, 1b vs 1a (negative = 1b better):
- **1X2: no gain.** Log loss +0.03% [−0.05, +0.11], RPS +0.06% [−0.06, +0.17]. Acceptance
  item 2 (1b beats 1a, interval excluding 0) **fails**; the holdout gate needs >= 1.0%.
  Per bundle: Italy −0.14% (RPS −0.34%, excludes 0), England −0.02%, Germany +0.04%,
  France +0.11%, Spain +0.16% (La Liga +0.32%, interval excludes 0).
- **Totals: better (D15).** OU2.5 log loss −0.34% [−0.55, −0.12], total CRPS −0.41%
  [−0.60, −0.21]; BTTS −0.14% (includes 0).
- **Cold start: better.** 663 matches, 1X2 log loss −1.06% [−1.73, −0.39].
- **Promoted / relegated bias: unchanged** (promoted 1–10 −0.161, relegated 11+ +0.118).
- **Sampling, per refit:** 160 of 266 refits have R-hat > 1.01 (most 1.010–1.014; worst
  1.025, Germany), 22 have ESS < 400 (13 Spain), 4 have divergences (3 Spain). The §14.5
  audit sample would fail on these as things stand.

### Sampling fix: 2,000 draws (2026-11-06)

Run 42220325734 refitted the two worst-mixing bundles (Spain, Germany; 108 refits) at
2,000 draws and 2,000 tuning steps per chain, everything else unchanged:

| | 1,000 draws | 2,000 draws |
|---|---|---|
| refits with R-hat > 1.01 | 70 of 108 | 1 (1.013, Germany) |
| refits with bulk ESS < 400 | 20 (min 239) | 0 (min 604) |
| refits with tail ESS < 400 | 0 | 0 (min 899) |
| refits with divergences | 4 (24 in all) | 1 (2 in all, Spain) |
| median fit time on a runner | 147 s / 231 s | 158 s / 346 s |

The slowest parameter is `sigma_walk` (63 of 108 refits) or a single strength innovation.
Both remaining misses are the 2022-12-26 refit, the first after the winter World Cup break
(six weeks with no league matches mid-season). Production setting is now 2,000 draws
(backtest default, workflow default); the full dev backtest should be rerun at 2,000 for
all bundles before the audit sample, and the World Cup refit looked at (target_accept or a
break-aware step) if it still misses.

### Full dev backtest at 2,000 draws (2026-11-06)

Run 42239760464 (commit 4bb5218), all 5 bundles x 2019–2023, 266 refits, 26/26 jobs green,
grid identical to 1a. Report `reports/phase1b/backtest/20261106T161314Z` (R2), recorded as
`model_run` 3.

- **Sampling: clean.** R-hat > 1.01 in 2 of 266 refits (Germany 2022-12-26 1.013, Italy
  2019-12-09 1.011), bulk ESS < 400 in 0, divergences in 2 (Spain 2022-12-26: 2, Italy
  2019-08-19: 1). Was 160 / 22 / 4 at 1,000 draws. The World Cup refit (2022-12-26) is
  marginal but no longer systematic; not worth a special case.
- **Scores: unchanged from 1,000 draws** (all within 0.02 points), so the earlier
  conclusions stand, now on clean sampling:
  1X2 log loss +0.03% [−0.05, +0.11], RPS +0.06% [−0.05, +0.18] (no gain; acceptance
  item 2 still fails); OU2.5 log loss −0.35% [−0.56, −0.13], total CRPS −0.41%
  [−0.61, −0.22]; cold start 1X2 log loss −1.18% [−1.84, −0.52]; La Liga/Spain +0.12%
  [−0.04, +0.28]; promoted/relegated bias unchanged.
- **Reading:** a weekly random walk on strength adds nothing over a static fit with a
  365-day half-life on 1X2 at a 28-day refit cadence. The gains are in totals (D15
  openness) and cold start (hierarchical shrinkage). Task 5 components (AR(1) form, xG
  auxiliary likelihood) are where a 1X2 gain would have to come from.

### 5. Components, each added only if it earns its place on dev
- [ ] AR(1) short-term form, phi in (0, 1) (§5.2).

#### AR(1) form: design and acceptance test (stated 2026-11-06, before building)

Design (`form=True`, strength_open only; off by default, so the reference model is
unchanged byte for byte):
- Per team, a weekly zero-sum AR(1) on the strength axis, m_t = phi m_{t-1} + sigma_form
  e_t, started at its stationary sd sigma_form / sqrt(1 - phi^2); added to strength, so
  attack and defence each move by m / sqrt 2 (form is about results, not openness). No
  season reset: the summer's ~13 empty weeks decay it by phi^13.
- Priors (§5.2 only fixes phi in (0, 1)): logit(phi) ~ Normal(logit 0.8, 0.75 k), i.e. a
  half-life of ~3 weeks with wide support; sigma_form ~ HalfNormal(0.1 k).
- Prediction for a match h weeks after the last training step uses phi^h m_last, so form
  fades over the 0–4 week horizon of a 28-day refit (reported by horizon week).
- Known risk: a random walk plus an AR(1) on the same axis are only weakly separable (they
  differ only in how fast shocks fade); sigma_form may collapse towards 0 or trade off
  against sigma_walk.

Acceptance test, dev only, 28-day grid, against `phase1b_strength_open` at 2,000 draws
(run 42239760464, same grid):
1. **Gate (§14.5)** on England as of 2023-08-07: every gating check passes, with phi and
   sigma_form added to the prior-sensitivity headline (posterior means move < 1 sd at x0.5
   and x2).
2. **Mixing on dev:** no more refits over R-hat 1.01 / under ESS 400 / with divergences
   than the reference (2 / 0 / 2 of 266), give or take 2.
3. **Accuracy:** 1X2 log loss improves vs the reference, paired 95% interval excluding 0;
   RPS not worse; OU2.5 log loss and total CRPS not worse than 0.1% relative; no bundle
   worse than 0.3% on 1X2 log loss.
4. **Sense:** posterior phi and sigma_form reported per bundle; a gain should be largest at
   horizon week 1 and fade by week 4.
- Failing: record it as failed and keep the code off by default, as for D16.

Result (2026-11-07, run 42267331196, commit dd32735, report `backtest/reports/20261106T233957Z`
in R2): **failed; the code stays off by default.** 255 of 266 refits and 17,795 of 18,591
warm matches: `refit (spain, 2021)` failed after 255 minutes with no log kept, so its 796
matches are missing and were not rerun. 10 of its 11 refits are cached, and adding them could
not change the verdict, because item 2 fails on the refits already counted.
- Item 1, gate: not run; it was only worth running after a gain.
- Item 2, mixing: **fails.** R-hat over 1.01 in 19 of 255 refits against 2 of 266 (worst
  1.018; Germany 8), ESS under 400 in 0, divergences in 1.
- Item 3, accuracy: **fails.** 1X2 log loss -0.02% [-0.04%, +0.00%] (exactly -0.0175%
  [-0.0361%, +0.0011%]), so the interval includes 0. RPS -0.03% [-0.06%, -0.00%]; OU2.5 +0.01%
  and CRPS -0.00%, both inside 0.1%. No bundle is worse than 0.3%: England -0.05% [-0.09%,
  -0.02%], France -0.03%, Germany -0.04%, Spain +0.00%, Italy +0.04% [+0.01%, +0.06%].
- Item 4, sense: **no sign of form.** Horizon weeks 0 to 3 give -0.03%, -0.01%, -0.02%, -0.01%,
  all intervals including 0, with no peak at week 1 and no fade. Posterior phi is close to its
  prior: means 0.76 to 0.78 with 94% intervals of about [0.47, 0.94] against a prior of about
  [0.49, 0.94], so the data barely inform it. sigma_form is not kept in the slim draws;
  implied from sd(form_last) sqrt(1 - phi^2) it is 0.017 (Spain) to 0.045 (Germany), with
  intervals reaching 0.001, which is the collapse named as a risk above. The spread of
  teams' posterior mean form is worth 0.2% to 0.7% on a scoring rate.
- Reading the report: with a challenger incomplete, the `base` column of `compare` still
  averages every base match, so it does not match the paired n. `rel` and its interval use the
  paired differences; only their denominator is that wider mean, which changes nothing here.
- [ ] xG auxiliary likelihood where `has_xg` holds (joint goals + xG likelihood).

#### xG auxiliary likelihood: design and acceptance test (stated 2026-11-07, before building)

Design (`xg=True`, strength_open; off by default; §5.2 allows a joint likelihood or a two-stage
prior, and the joint one is taken because it needs no second model):
- Each team-match with xG adds xG ~ Gamma(mean = lambda * exp(xg_bias[league]), shape =
  xg_shape), on the same lambda as the goals, so it informs strength and openness directly.
  xG never enters prediction: `attack_last` and `defence_last` are read as before.
- Priors: xg_shape ~ LogNormal(log 4, 0.5 k), i.e. a per-match CV of ~0.5; xg_bias ~ Normal(0,
  0.1 k). Values under 0.01 are floored at 0.01 (a Gamma has no mass at 0).
- Gate: a match counts only if its league-season has `has_xg` available or partial and both
  teams have xG; otherwise goals alone, and a window with no xG is exactly the goals model
  (principle 8). xG is usable from kickoff + 3h, like the result.
- Known risks: goals and xG are treated as independent given lambda though they come from the
  same chances, so the posterior may be overconfident (xg_shape absorbs only part of this).
  xG was fetched in 2026, so a later provider revision cannot be ruled out; it is a value of
  the same match, not a future result.

Coverage on dev (audited 2026-11-07): only the five top flights from about mid-January 2023,
plus all of 2023/24 for them and the Championship. No second tier other than the Championship
has any, and nothing before 2022/23. So xG can only affect about 1 in 6 dev matches, and the
all-dev figures would dilute any effect.

Acceptance test, dev only, 28-day grid, seasons 2022 and 2023 only (earlier seasons cannot
change), against `phase1b_strength_open` at 2,000 draws (run 42239760464, same grid).
"Informed" means warm matches in a league with at least 50 xG matches in that refit's
training window; everything else in those seasons is "control":
1. **Gate (§14.5)** on England as of 2023-08-07: every gating check passes, with xg_shape and
   xg_bias added to the prior-sensitivity headline; run only if item 3 passes.
2. **Mixing:** no more refits over R-hat 1.01 / under ESS 400 / with divergences than the
   reference has on the same refits, give or take 2.
3. **Accuracy, informed matches:** 1X2 log loss improves, paired 95% interval excluding 0; RPS
   not worse; OU2.5 log loss and total CRPS not worse than 0.1% relative; no bundle worse than
   0.3% on 1X2 log loss.
4. **Control:** 1X2 log loss on control matches within 0.1% of the reference, since only
   indirect effects should reach them.
5. **Sense:** posterior xg_shape and xg_bias reported per bundle; xg_bias near 0 (|mean| under
   0.1); the gain should be larger early in a season than late, as §5.2 predicts.
- Failing: record it as failed and keep the code off by default, as for D16.
- A pass means xG earns its place where it exists. It does not by itself reach the Phase 1b
  gate on 1X2 against Phase 1a; that gate is judged on all its segments, unchanged.
- [ ] Manager-change widening of sigma_walk for a fixed post-change window (if D12 allows).

### 6. Promotion
- [ ] Inference gate passes (acceptance item 1).
- [ ] Holdout opened once and the gate applied; outcome recorded in `model_run` and
      summarised in `reports/phase1b/`.

## Out of scope for 1b (spec places them later)
Global cross-league layer / Q_league (§10.1; needs continental bridging data), neutral and host
flags (needed with play-offs and cups), attendance modifier (`has_attendance_data` is absent),
Layer 3+ covariates, lineup offset (Phase 3).

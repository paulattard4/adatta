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
     < 0.2% relative and no headline posterior mean moves > 1 posterior sd.
   - Score-matrix truncation: tail mass < 1e-4 for 99.9% of predictions.
   - Reproducibility: the same seed and data give identical draws.
2. **Dev window (2019/20–2023/24), iterate freely:** 1b beats 1a on 1X2 log loss and RPS,
   paired 95% interval excluding zero, overall and for the England bundle. Each added
   component (AR(1) form, xG auxiliary, manager-change widening) is kept only if it improves
   dev log loss on the segments where it applies. Ablations are recorded in `model_run`.
3. **Locked holdout (2024/25–2025/26), opened once,** with the gate pre-registered in
   `config/evaluation.yml` (`phase1b_gate`): >= 1.0% relative 1X2 log-loss gain and >= 0.5%
   RPS gain vs 1a, paired 95% interval excluding zero, and no league x market segment worse
   than 1.0% relative. 1a is scored on the holdout in the same run. The outcome is recorded
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
- Failing either: revert to the spec model and record the misfit as known (affects totals
  markets, not 1X2).

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
- [ ] Harness runs the 1b model as one more model on the same as-of grid. Each refit's draws
      are cached by (bundle, as_of, config hash), so reruns are resumable.
- [ ] `backtest.yml` workflow (D9): manual dispatch, matrix of bundle x season, each job
      resumable from cached draws in R2, results written to Neon; a final job assembles the
      report.

### 5. Components, each added only if it earns its place on dev
- [ ] AR(1) short-term form, phi in (0, 1) (§5.2).
- [ ] xG auxiliary likelihood where `has_xg` holds (joint goals + xG likelihood).
- [ ] Manager-change widening of sigma_walk for a fixed post-change window (if D12 allows).

### 6. Promotion
- [ ] Inference gate passes (acceptance item 1).
- [ ] Holdout opened once and the gate applied; outcome recorded in `model_run` and
      summarised in `reports/phase1b/`.

## Out of scope for 1b (spec places them later)
Global cross-league layer / Q_league (§10.1; needs continental bridging data), neutral and host
flags (needed with play-offs and cups), attendance modifier (`has_attendance_data` is absent),
Layer 3+ covariates, lineup offset (Phase 3).

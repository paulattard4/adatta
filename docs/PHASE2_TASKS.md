# Phase 2 — context layer (spec §6, §11.3, §14, §19.4)

Goal: add the Layer 3 context terms to the production team model, so that rest, travel,
weather, referee, importance, kickoff time and style move the expected goals. Spec gate
(§19.4): "each covariate re-backtested and kept only if it improves calibration". The model
it builds on is the Phase 1b champion (`phase1b_strength_open_xg`, if the locked holdout
promotes it). If 1b is rejected, this plan is revisited before any build.

Status (2026-11-11): **stated, not started.** The inputs are built and verified
(`adatta/features/context.py`, `docs/FEASIBILITY_REGISTER.md`). Model work waits for the
Phase 1b audit to finish (it holds `adatta/model/` and the backtest and gate workflows).
The owner settled D29–D31 on 2026-11-11, each as leaned below.

Tick each item only when its acceptance check passes.

## What enters, and where (spec §6.1–§6.2, register sections 1–3)

Every term is a covariate on log lambda, per bundle, with one coefficient shared by the
bundle's leagues. The coding makes 0 the 1b model: a covariate at its reference value, or
null, adds nothing, so a missing value falls back to the simpler layer and is never imputed
(principle 8). Continuous inputs are clipped and scaled by fixed constants stated here, not
by statistics of the data, so no scaling can leak. Priors: Normal(0, 0.2) per coefficient
(§6.1) unless stated.

| Group | Register | Inputs (context columns) | Coding (0 = reference) | Enters |
|---|---|---|---|---|
| G1 Rest | 2.16 | `{side}_days_since_last` | `short_rest` = 1 if under 4 days; `long_break` = 1 if 14 days or more | own lambda (attack), §6.2 |
| G2 Travel and altitude | 2.10, 2.14 | `{side}_travel_km`, `{side}_tz_shift_h`, `{side}_altitude_diff_m` | log(1 + km / 100); absolute hours of timezone shift; max(0, altitude diff) / 1000 m (climbing only). All 0 for a side at its home base | own lambda (mostly the away side's) |
| G3 Weather | 2.12, 2.13 | `temperature_c`, `wind_speed_kmh`, `precipitation_mm`, `weather_source` | heat = max(0, t − 22) / 5; cold = max(0, 5 − t) / 5; wind = max(0, w − 20) / 10; rain = log(1 + mm) | both lambdas (total goals) |
| G4 Referee pace | 2.18, 2.19 | `referee_fouls_pm`, `referee_matches`, `pool_fouls_pm` | shrunk = (n·r + 20·pool) / (n + 20); x = shrunk / pool − 1; 0 under `pool_prior` | both lambdas, weakly: prior Normal(0, 0.1) |
| G5 Importance | 2.20 | `{side}_importance` | `dead_rubber`, `must_win` indicators; `live` and null are 0 | own lambda |
| G6 Kickoff hour | 2.23 | `kickoff_hour_local` | `early` before 14:00, `late` from 20:00 local | both lambdas; regularised horseshoe (§6.2 "minor") |
| G7 Style (3a) | 2.1–2.4 | as-of rolling team style (to build, task F1) | tempo: (shots for + against per match) / league mean − 1, both sides summed | both lambdas |

Not in Phase 2, with the reason:
- **Forward congestion (2.17)** enters the rotation prior in selection (§6.2, §7.2), which is
  Layer 4 (Phase 3). Putting it on lambda now would be a spec divergence; the column stays
  in the context table for Phase 3.
- **Referee strictness for cards and fouls** is Layer 5c (§8.4, Phase 3b). Phase 2 has only
  the secondary "pace" term (G4).
- **`has_var` and added time (2.21, 2.22)** act on expected minutes and each half's lambda
  (§6.2). The half split is Phase 3b. In dev, `has_var` is constant within almost every
  league-season, so on full-time lambda it can't be separated from the league level.
- **Surface and pitch dimensions (2.11)**: no history, near-constant; absent.
- **Style effect on variance (§6.1 second bullet, register 2.5)**: the 1b likelihood has no
  dispersion parameter (D20 found the data under-dispersed), so style enters the mean only
  in Phase 2, as the register's degrade rule allows. A variance term is a later challenger.
- **Altitude in continental play**: cups and continental matches are not modelled yet.

## Prediction time (the backtest must replicate production, §5.3)

Production refits weekly and resimulates daily from stored draws (D11, §12.2): the
posterior is fitted as of the refit, and each match's context is read as of the prediction
time. The backtest does the same:
- Coefficients are fitted on training matches, each with its own context row built at that
  match's kickoff − 1 h (what a T − 1 h prediction would have seen), and only on matches
  final by the refit's as-of.
- A target is scored at the **headline horizon T − 1 h**: the draws of the latest refit at
  or before the target's as-of, combined with the target's context row at kickoff − 1 h.
- A **secondary horizon T − 48 h** is reported, not gated: referee from the pool (D26) and
  weather from climatology or a 2-day forecast.
- The 1b reference has no context, so its predictions are the same at every horizon and it
  pairs with both.

## Decisions (resolved 2026-11-11 by the owner: the leaning in each)

- **D29: weather in backtests.** As-issued forecast history exists only from about Feb 2024
  (register 2.12), so dev 2019/20–2023/24 has climatology only. Climatology is near-constant
  for a venue and month, so a test on it says little about forecasts. Options: (a)
  climatology only in dev, with forecasts judged on the holdout and live; (b) Open-Meteo
  previous-runs forecasts for Feb 2024 onward (true 1-day-lead forecasts: the last months of
  dev plus both holdout seasons), climatology before; (c) as (b), plus ERA5 realised weather
  as a stand-in for a T − 1 h forecast in dev before Feb 2024, labelled an optimistic
  look-ahead like D25. Leaning: (c), with the gain required to hold on the as-issued
  forecasts subset. Cost: free tier only. (b) is roughly one call per match-day and venue
  point; (c) adds about 7 years of hourly archive for 277 points, several days of the
  10,000-call daily limit, run in the background like the climatology download.
- **D30: the Phase 2 promotion window.** The 2024/25–2025/26 holdout is opened once for 1b.
  Options: (a) open it a second time for the pre-registered Phase 2 gate; its 1b score
  doesn't guide Phase 2 iteration, but it is a second look; (b) promote on the live
  2026/27 period instead, which has real forecasts and real referee timing but reaches
  about 1,500 matches only around February 2027; (c) both: (a) to promote, (b) to confirm.
  Leaning: (c).
- **D31: "improves calibration" (§19.4).** Proposed reading: a group is kept if a proper
  score (log loss, RPS, CRPS) improves on the matches it acts on, with the paired 95%
  interval excluding zero, and the reliability of those matches is not worse. A group that
  moves only goal totals is judged on totals (CRPS, OU2.5), not on 1X2.

## Acceptance test (stated 2026-11-11, before building, per CLAUDE.md)

Order matters (§14.5): the diagnostics gate first, the backtest comparison second. Dev only
(2019/20–2023/24, warm matches, 28-day grid per D14) until the final combined run.

### Per group (G1–G7, each against the 1b reference on the same grid)

The "active" matches of a group are those where any of its covariates is non-zero (for
example G1: either side on short rest or back from a long break; G3: any weather term
non-zero).
1. **Mixing:** no more refits over R-hat 1.01 / under ESS 400 / with divergences than the
   reference on the same refits, give or take 2.
2. **Accuracy on active matches:** the primary score improves with the paired 95% interval
   excluding 0. Primary: 1X2 log loss for the per-side groups (G1, G2, G5); total-goals CRPS
   for the total-goals groups (G3, G4, G6, G7).
3. **No harm elsewhere:** on all warm matches, 1X2 log loss and RPS not worse (point
   estimate), OU2.5 log loss and total CRPS not worse than 0.1%, and no bundle worse than
   0.3% on 1X2 log loss.
4. **Calibration (D31):** on active matches, the expected calibration error of each 1X2
   outcome and of OU2.5 (probability deciles) is not worse than the reference's by more
   than 0.005.
5. **Sense:** posterior coefficients reported per bundle, with sign and size checked
   against the expectation (short rest, long travel and dead rubbers lower a side's goals;
   heat, wind and rain lower totals). A coefficient whose posterior stays on its prior in
   every bundle (sd ratio over 0.9) is recorded as uninformed, as change_weeks was.
- Failing: record it as failed, with the numbers, and keep the group off by default (as
  D16 and manager-change widening).
- D25 and D26 segments are reported separately: G1 and G4 results for Championship, La
  Liga and Segunda (referee known under 24 h before kickoff) are labelled optimistic.

### Combined model (the groups that passed)

1. **Gate (§14.5)** on the fixed 25-fit audit sample of Phase 1b (same dates and seed),
   every gating check, with the kept coefficients in the prior-sensitivity headline (x0.5
   and x2, parameter and predictive parts).
2. **Dev, weekly cadence (D11):** 1X2 log loss and RPS improve on the 1b reference with the
   paired 95% interval excluding 0, overall; and no league worse than 1.0% on any D28 gate
   metric. Also reported, not gated: by weather_source, by referee_basis, by bundle, and the
   T − 48 h horizon.
3. **Leakage:** the context-table test (`tests/test_context.py`) still passes. A new
   harness test checks that every target's context row has as_of ≤ its prediction time and
   every training row's has as_of ≤ the refit's as-of, and that a context value changed
   after as_of leaves the prediction unchanged.
4. **Feasible:** median refit time per bundle at most 1.25 times the 1b reference's, and
   every job inside the 6-hour runner limit (D9).
5. **Promotion (D30):** the pre-registered gate, written into `config/evaluation.yml`
   before the window is opened, in D18 form: any improvement on 1X2 log loss and RPS
   with the paired 95% interval excluding 0, no league x market segment worse than 1.0%.
   Outcome recorded in `model_run` as champion or rejected (§14.6).

## Tasks

Features (outside `adatta/model/`, can start now):
- [ ] F1 Style features: `adatta/features/style.py`, as-of rolling team style per §5.3
      (365 days, matches final by as_of, league-normalised tempo: shots for and against,
      on target, possession). Acceptance: leakage test as for context; coverage ≥ 99% of
      2019–2025 league matches with at least 5 prior matches in the window; null otherwise.
- [ ] F2 Weather history per D29 (c), free tier, in the background.
      Design: (1) as-issued forecasts for every bundle league match from 2024-02-01 (the
      previous-runs API has values from late January 2024: probed 2026-11-11, Manchester
      empty on 01-25, Madrid present on 01-28), read from the runs 1 and 2 days before
      (`previous_day1`, `previous_day2`) and stored as `weather_snapshot` rows with
      lead_hours 24 and 48 and observed_at = kickoff - lead, so the context table's
      forecast rule picks them up unchanged; (2) ERA5 realised weather at the venue point
      for matches from 2019-07-01 to 2024-01-31, read from the raw store at build time like
      the climatology and never stored as a forecast; (3) ERA5 also for Feb–May 2024, to
      measure the stand-in's optimism against the as-issued forecasts.
      Acceptance test (stated 2026-11-11, before building):
      - the context row uses the stand-in only when there is no as-issued forecast, the
        kickoff is before 2024-02-01 and the horizon is 24 h or less, labelled
        `weather_source = realised_proxy` (null lead); at T − 48 h such a match keeps
        climatology; unit tests on synthetic payloads, and the existing leakage test passes;
      - an as-issued row is used only from its observed_at (T − 1 h gets the day-1 run,
        T − 48 h the day-2 run);
      - coverage after the download: at T − 1 h, ≥ 99% of bundle league matches 2019/20 to
        2026-11-05 have a forecast or the stand-in; climatology only where neither exists;
      - the stand-in's error is reported on Feb–May 2024 (mean absolute difference from the
        day-1 forecast for temperature, wind and rain), so its optimism has a size;
      - free tier only: each run takes a call budget (default 8,000), paced under 4,000
        calls an hour, stops on a rate limit and resumes on the next run; requests are
        grouped into windows of at most 14 days per venue point (one call each).
- [ ] F3 Context rows for every training and target match at kickoff − 1 h and − 48 h,
      written once per data snapshot (`context-build --horizon-hours`), hashed into the
      reproducibility manifest.

Model (after the Phase 1b audit closes):
- [ ] M1 Harness: prediction-time scoring (latest refit's draws + the target's context
      row), with the leakage test above. The 1b reference scores unchanged.
- [ ] M2 Covariate terms in the team model, one flag per group, all off by default; with
      every flag off the model is bit-identical to 1b (same seed, same draws).
- [ ] M3 Group backtests G1–G7, one at a time (two backtests at once starve capture).
- [ ] M4 Combined model: gate, weekly dev run, feasibility.
- [ ] M5 Promotion per D30.

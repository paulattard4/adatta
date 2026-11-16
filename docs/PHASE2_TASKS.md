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
  When they are (knockout logic §9, cross-league strength Q_league §10.1), retest G2 on
  cross-nation matches only (European cups, internationals). The G2 fail (2026-11-12) is
  domestic only: there tz_shift was not identified (sd / prior about 1), climbs are small
  and distance is absorbed by home advantage. Cross-nation trips vary far more (shifts of
  up to +5 h within UEFA, climbs to 3,600 m in CONMEBOL), so the result does not carry
  over. Pool the coefficients across teams (few continental matches per team). The
  features need no rebuild.

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
2. **Dev, weekly cadence (D11):** passes if either route passes, both judged on all warm
   matches against the 1b reference on the same grid, paired by match (D36):
   - **Route A, 1X2 (D18 form):** 1X2 log loss and RPS improve with the paired 95%
     interval excluding 0, overall.
   - **Route B, total goals (D36, stated 2026-11-13 before any M4 run):** total-goals CRPS
     and OU2.5 log loss each improve with the paired 95% interval excluding 0, overall;
     1X2 log loss and RPS no worse (point estimate <= 0); ECE of each 1X2 outcome and of
     OU2.5 no worse than the reference's by more than 0.005.
   - Both routes: no league worse than 1.0% on any D28 gate metric.
   The report states which route passed. Also reported, not gated: by weather_source, by
   referee_basis, by bundle, and the T − 48 h horizon.
3. **Leakage:** the context-table test (`tests/test_context.py`) still passes. A new
   harness test checks that every target's context row has as_of ≤ its prediction time and
   every training row's has as_of ≤ the refit's as-of, and that a context value changed
   after as_of leaves the prediction unchanged.
4. **Feasible:** median refit time per bundle at most 1.25 times the 1b reference's, and
   every job inside the 6-hour runner limit (D9).
5. **Promotion (D30):** the pre-registered gate, written into `config/evaluation.yml`
   before the window is opened, with the same two routes as item 2 (D36): route A in D18
   form (any improvement on 1X2 log loss and RPS, paired 95% interval excluding 0), or
   route B (CRPS and OU2.5 log loss improve with the interval excluding 0, 1X2 log loss and
   RPS point estimates <= 0, ECE within +0.005); either way no league x market segment
   worse than 1.0%. Outcome recorded in `model_run` as champion or rejected (§14.6), with
   the route.

## Tasks

Features (outside `adatta/model/`, can start now):
- [x] F1 Style features: `adatta/features/style.py`, as-of rolling team style per §5.3
      (365 days, matches final by as_of, league-normalised tempo: shots for and against,
      on target, possession). Acceptance: leakage test as for context; coverage ≥ 99% of
      2019–2025 league matches with at least 5 prior matches in the window; null otherwise.
      **Pass, 2026-11-12.** League matches only (cup statistics are patchy, and tempo
      needs a league); each match's tempo is divided by its own league's mean over the
      same window, so a promoted side's earlier season is measured against the league it
      was played in. `tests/test_style.py` (6 tests: window means, the minimum, the
      365-day window, leakage at as_of − result lag, league normalisation incl. a promoted
      side, the loader's filters). Live, the 26,969 bundle league matches 2019–2025 at
      kickoff − 1 h (3 s to load, 1 s to build): 98.93% of the 53,938 sides have at least
      5 prior matches, and every one of those has all values (100%). The other 578 sides
      are null: 118 side-seasons entering the loaded leagues (promoted from an unloaded
      third tier, all in the five second divisions) during their first 4 matches.
      `tempo_rel` ranges −0.30 to +0.51, sd 0.063.
- [x] F2 Weather history per D29 (c), free tier, in the background.
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
      - free tier only: each run takes a call budget (default 8,000), paced at 2,000
        calls an hour, stops on a rate limit and resumes on the next run; requests are
        grouped into windows of at most 14 days per venue point (one call each).
      Progress: 2026-11-11, 1,551 of 6,641 as-issued windows stored; the ERA5 windows not
      started. Open-Meteo began refusing this machine ("Too many concurrent requests")
      after about 1,550 requests at 4,000 an hour on top of the day's climatology calls, so
      the pace is now 2,000 an hour. Resume daily with `adatta weather-history`.
      **Done 2026-11-16; passes.** The last 5 as-issued windows fetched; `weather-history
      --dry-run` now plans nothing (30,727 placed matches). Coverage at T − 1 h, bundle
      league matches 2019/20 to 2026-11-05 (28,014): 99.94% have a forecast (10,253, all
      lead 24 h, from 2024-02-01) or the stand-in (17,743, 2019-07-26 to 2024-01-31). The
      other 18 keep climatology: Premier League and Championship matches of 1–3 Feb 2024,
      whose as-issued rows exist but carry no values (the previous-runs archive starts a
      few days later at the English points; nothing to fall back to, principle 8). At
      T − 48 h every pre-2024-02-01 match keeps climatology (no stand-in) and 10,248 have a
      48 h forecast. Stand-in minus day-1 forecast, Feb–May 2024, 1,613 matches
      (`weather-history-report`): temperature MAE 0.98 °C (bias +0.06), wind MAE 3.0 km/h
      (bias +1.06), precipitation MAE 0.28 mm (bias +0.03). That is the stand-in's
      optimism: a weather effect fitted on 2019–2024 sees weather about this much more
      exactly than a forecast would give it.
- [x] F3 Context rows for every training and target match at kickoff − 1 h and − 48 h,
      written once per data snapshot (`context-build --horizon-hours`), hashed into the
      reproducibility manifest.
      Acceptance (stated 2026-11-12, before building): (1) every finished league match the
      backtest loads for the five bundles, from 2016/17 (the dev training windows reach
      back that far), has a row at both horizons, and a missing row stops the backtest;
      (2) every row's `ctx_as_of` is kickoff − horizon; (3) snapshots are named by a digest
      of their content, and the digest enters the backtest config hash, so draws fitted on
      one snapshot are never reused for another; (4) `--group` loads a snapshot and scores
      targets at − 1 h or − 48 h, tested end to end; (5) coverage reported by league-season.
      **Code and coverage pass 2026-11-12; ticked when the final snapshots are stored
      after F2.** `adatta/features/context_table.py` (context + F1 style, `ctx_as_of`;
      content digest; `features/context/h<hours>/<digest>.parquet` and `latest.json` in
      the private store, never git); `adatta context-build --horizon-hours 1|48 --upload`
      (now from 2016, with style coverage); backtest `--context <digest|latest>`,
      `--target-horizon-hours 48`, `RunSpec.context` in the config hash, predictions carry
      `ctx_horizon_h` and `context`. `tests/test_context_table.py` (5 tests). Live, both
      horizons: 42,624 bundle league matches 2016–2026 in 3.8 min each; every one of the
      39,749 matches the backtest loads has a row with `ctx_as_of` exactly kickoff −
      horizon (items 1, 2). Not yet uploaded: the F2 weather download is still running
      (as-issued forecasts so far reach La Liga, Serie A, Segunda and Serie B from 2024,
      Ligue 1 and 2 in part, England not yet), so a snapshot now would be superseded.
      **Final snapshots stored 2026-11-16, after F2:** h1 `7cf547d91cba6529`, h48
      `b29bb7f0915abf76` (`latest` points at them), 42,624 matches 2016–2026 each, the
      same match ids at both horizons, `ctx_as_of` 47 h apart on every row. The M5
      promotion spec stays pinned to `962f29fe719a9d58` (it reads only the style columns,
      which the weather download does not touch; still stored and loads), so the holdout
      run in progress is unaffected; a weather group run uses the new digests.
      **G5 gap closed 2026-11-12:** importance was null for every 2016–2018 match
      (zone rules from 2019 only), so dead rubbers in the dev training windows would have
      been fitted as live. `config/league_rules.yml` now has 2016/17–2018/19 rules (UEFA
      access lists and formats, cross-checked against the zone labels of the final tables,
      fetched for this: 30 requests), plus the deductions and awarded results those
      tables show (Birmingham 2018/19 dated; the rest with unknown dates, so a backtest
      applies them only after the season). `rules-check`: 110 tables, 0 problems (was 129).
      The provider's 2017/18 tables for the five second divisions are mid-season snapshots
      stored as final, and its 2018/19 Serie B table names Cosenza by another id; both are
      recorded as `table_issues`, so 2017/18 second-division deductions cannot be checked.
      Rebuilt at − 1 h: importance on 100% of 2016–2018 regular-season matches (11,844;
      446 dead rubbers, 421 must-win), was 0%.

Model (after the Phase 1b audit closes):
- [x] M1 Harness: prediction-time scoring (latest refit's draws + the target's context
      row), with the leakage test above. The 1b reference scores unchanged.
      **Pass, 2026-11-12** (CI run 43173650741). `backtest.score_targets` adds each
      target's context terms (`covariates.offsets`: its own row, observed by kickoff − 1 h)
      to the refit's draws; `RunSpec.groups` / `--group G1..G7` (canonical order, model
      name `_ctx-<groups>`). `covariates.check_as_of` refuses any training or target row
      whose context is later than its kickoff − 1 h. `--group` stops with a message until
      F3 provides the context table. `tests/test_covariates.py` (9 tests, pure numpy): each
      group's coding; late context refused (training and target); context of matches not
      final at the refit leaves the fit's design unchanged; changing one target's weather
      moves that prediction only; with no groups, or groups at coefficient 0, the scores
      equal the 1b scores (rtol 1e-12).
- [x] M2 Covariate terms in the team model, one flag per group, all off by default; with
      every flag off the model is bit-identical to 1b (same seed, same draws).
      **Pass, 2026-11-12** (CI run 43173650741). `adatta/model/covariates.py` codes
      G1–G7 as the table above (nulls add 0); `team_strength.covariate_terms` adds
      `beta_<group>` per group: Normal(0, 0.2), referee 0.1, kickoff a regularised
      horseshoe (global scale 0.01, Student-t(4) slab of scale 0.2, fixed before testing).
      `fd.cov == ()` adds no variable. `tests/test_covariate_model.py` (PyMC): no groups adds
      nothing; a simulated heat effect of −0.3 is recovered inside its 90% interval; the
      empty-design path samples identically. Bit-identical to 1b: `python -m
      adatta.model.identical --ref <commit>` samples the default model at that commit and
      now and compares every stored draw; the CI `model` job runs it against the previous
      push (a deliberate change to the default model fails that step, by design). The
      local Docker engine stopped creating containers on 2026-11-12, so these run in CI
      (the model tests never ran in CI before: `ci.yml` installed no model group).
      Result against 35a2813, the last commit before M2 (manual dispatch, `identical_ref`):
      strength_open and independent, 8 stored variables x 400 draws each, bit-identical;
      the 8 PyMC tests pass.
      Note: editing `team_strength.py` changes the config hash, so cached backtest draws
      are not reused; with the default path identical, the stored 1b dev predictions stay
      the M3 reference.
- [x] M3 Group backtests G1–G7, one at a time (two backtests at once starve capture).
      Tooling (2026-11-12): `backtest.yml` inputs `groups` (one group per run) and
      `context` (snapshot digest, default latest); `assemble --vs <1b reference>` writes
      `phase2_<model>.json` and a report section via `adatta/eval/phase2.py`, which applies
      items 1–4 as pass/fail and reports item 5 (coefficients per bundle, sign, sd / prior)
      for the owner, with rest and referee results for the Championship, La Liga and
      Segunda shown apart (D25/D26). Prediction rows carry `active__<group>` and each
      refit's coefficient mean and sd. A run's draws are keyed by a digest of only the
      columns its group reads, over the seasons its plan uses, so the weather rebuild does
      not invalidate G1, G2 and G4–G7 runs. Reference: `phase1b_strength_open_xg`, 28-day
      dev grid, run 42359964241 (43419004255 from 2026-11-13, see below). Snapshot at − 1 h stored 2026-11-12: `e9feb71c1fce2b40`
      (with the 2016–2018 importance labels; weather still partial, which only G3 reads).
      Order: G1, G2, G5, G4, G6, G7, then G3 after the weather download.
      `tests/test_phase2_report.py` (6 tests).
      **G1 rest: fail (2026-11-12, run 43186485166, commit c9ebc89, snapshot e9feb71c1fce2b40,
      reference run 42359964241; grid identical).** 18,591 warm dev matches, 8,685 active.
      (1) mixing pass: R-hat > 1.01 in 4 refits (reference + 2 = 5), ESS < 400 in 0,
      divergences in 1. (2) active 1X2 log loss −0.02% [−0.10%, +0.07%]: **fail**, no
      detectable gain. (3) all warm 1X2 log loss −0.00%, **RPS +0.02% (fail, limit 0)**,
      OU2.5 log loss +0.05%, total CRPS +0.07%; bundles −0.08% to +0.06%. (4) ECE better on
      every outcome (home −0.0037, draw −0.0031, away −0.0003, over 2.5 −0.0005). D25/D26
      leagues −0.04% [−0.15%, +0.08%], others −0.01% [−0.13%, +0.12%]. (5) short_rest −0.011
      to −0.032 in four bundles, +0.023 in England, each within about one sd of 0;
      long_break mixed (−0.054 to +0.011); sd / prior 0.12–0.24, so the data inform the
      coefficients and place them near 0. Reading: rest moves expected goals by a percent or
      two at most, which the strength walk already absorbs; it does not improve 1X2.
      Recorded as failed; the group stays off by default.
      **G2 travel: fail (2026-11-12, run 43213487067, commit c9ebc89, snapshot
      e9feb71c1fce2b40, reference run 42359964241; grid identical).** 18,591 warm dev
      matches, 18,303 active (log travel is non-zero for almost every away side).
      (1) mixing pass: R-hat > 1.01 in 1 refit, ESS < 400 in 0, divergences in 1.
      (2) active 1X2 log loss −0.01% [−0.06%, +0.05%]: **fail**, no detectable gain.
      (3) all warm 1X2 log loss −0.00%, **RPS +0.02% (fail, limit 0)**, OU2.5 log loss
      +0.03%, total CRPS +0.03%; bundles −0.11% (Italy) to +0.06% (England). (4) ECE pass
      (home +0.0012, draw −0.0039, away +0.0003, over 2.5 +0.0029). (5) log_travel −0.031
      to +0.030, signs mixed, sd / prior 0.15–0.20, so the data place it near 0; tz_shift
      sd / prior about 1 except Spain (0.44): no time-zone variation in domestic leagues
      outside Spain's Canaries, so the prior is returned; altitude_climb −0.135 (Spain) and
      −0.121 (Italy) in the expected direction, positive elsewhere, all within about one sd.
      Reading: within-country travel is already part of the home advantage; it adds nothing
      to 1X2. Recorded as failed; the group stays off by default.
      **G5 importance: fail (2026-11-12, run 43243963383, commit c9ebc89, snapshot
      e9feb71c1fce2b40, reference run 42359964241; grid identical).** 18,591 warm dev
      matches, 2,150 active (a dead rubber or must-win side). (1) mixing pass: R-hat > 1.01
      in 3 refits, ESS < 400 in 0, divergences in 2. (2) active 1X2 log loss **+0.40%
      [+0.13%, +0.67%]: fail, and worse with the interval excluding 0**. (3) all warm 1X2
      log loss **+0.02%** and **RPS +0.06% (both fail, limit 0)**, OU2.5 log loss -0.05%,
      total CRPS -0.02%; bundles -0.09% (Italy) to +0.10% (England). (4) ECE pass and
      clearly better on active matches: home -0.0090, draw -0.0060, away +0.0020, over 2.5
      -0.0169 (0.0383 vs 0.0552). (5) dead_rubber **positive in every bundle** (+0.002 to
      +0.176, Germany about 2.4 sd from 0), against the expected negative sign; must_win
      -0.001 to +0.081; sd / prior 0.27-0.37. Reading: a dead rubber does not make a side
      score less; if anything both sides score more (more open games), which the over 2.5
      calibration picks up, but as a per-side term it moves 1X2 the wrong way. Recorded as
      failed; the group stays off by default. A total-goals version (one term on both
      sides' rates, judged on CRPS and OU2.5) is a candidate for a later dev iteration, as
      a new pre-stated test, not a rescue of this one.
      **G4 referee: fail (2026-11-12, run 43274251776, commit 7681b82, snapshot
      e9feb71c1fce2b40, reference run 42359964241; grid identical).** 18,591 warm dev
      matches, 18,531 active (a referee with a pace estimate). Primary total-goals CRPS.
      (1) mixing pass: R-hat > 1.01 in 1 refit, ESS < 400 in 0, divergences in 0.
      (2) active total CRPS **+0.02% [−0.00%, +0.04%]: fail**, no gain. (3) all warm 1X2
      log loss −0.01%, **RPS +0.00% (fail, limit 0)**, OU2.5 log loss +0.01%, total CRPS
      +0.02%; bundles −0.11% (Italy) to +0.04% (England, Germany). (4) ECE pass (home
      −0.0007, draw −0.0041, away +0.0002, over 2.5 +0.0036). D25/D26 leagues +0.00%
      [−0.03%, +0.04%], others +0.02% [−0.00%, +0.05%]: the late-appointment optimism is
      moot. (5) referee_pace −0.036 to +0.002, sd / prior 0.89–0.92: the data barely move
      the prior, so there is no detectable referee effect on goals. Reading: referee pace
      adds nothing to goal totals once team strength is known. Recorded as failed; the
      group stays off by default.
      **G6 kickoff: fail (2026-11-13, run 43305718628, commit b184317, snapshot
      e9feb71c1fce2b40, reference run 42359964241; grid identical).** 18,591 warm dev
      matches, 6,656 active (early or late kickoff). Primary total-goals CRPS.
      (1) mixing **fail: divergences in 41 refits** (limit 4); R-hat > 1.01 in 4, ESS < 400
      in 0. The regularised horseshoe's funnel between the global scale and near-zero
      coefficients is the usual cause; G7 uses a Normal prior and is not affected.
      (2) active total CRPS **+0.02% [−0.02%, +0.06%]: fail**, no gain. (3) all warm 1X2
      log loss −0.02%, RPS −0.00%, OU2.5 log loss +0.00%, total CRPS +0.02%, all within
      limits; bundles −0.12% (Italy) to +0.06% (England). (4) **ECE draw +0.0080 (0.0187 vs
      0.0107): fail** (limit +0.005); home +0.0018, away −0.0005, over 2.5 −0.0009.
      (5) early −0.002 to +0.011 and late −0.008 to +0.002, sd 0.013–0.031: the horseshoe
      shrinks both to 0 (sd / prior not defined for the horseshoe). Reading: kickoff hour
      has no detectable effect on goals. Recorded as failed; the group stays off by default.
      A non-centred horseshoe would remove the divergences but would not change this
      verdict, since the primary metric shows no gain.
      **G7 style: pass (2026-11-13, run 43372042130, commit ec8c54c, snapshot
      e9feb71c1fce2b40, reference run 42359964241; grid identical).** 18,591 warm dev
      matches, 18,487 active (both sides' tempo known). Primary total-goals CRPS.
      (1) mixing pass: R-hat > 1.01 in 1 refit, ESS < 400 in 0, divergences in 0.
      (2) active total CRPS **−0.12% [−0.18%, −0.05%]: pass**, the interval excludes 0.
      (3) all warm 1X2 log loss −0.03%, RPS −0.01%, OU2.5 log loss −0.12%, total CRPS
      −0.12%, all within limits; bundles −0.10% (Italy) to +0.07% (England). (4) ECE pass
      (home +0.0002, draw −0.0029, away +0.0003, over 2.5 +0.0003). (5) tempo **positive in
      every bundle**, +0.163 (Italy) to +0.437 (Spain), 1.4 to 3.4 sd from 0, sd / prior
      0.59–0.68: two high-tempo sides (more shots for and against than the league) produce
      more goals than their strengths alone say. Reading: the first group to pass; it
      sharpens the total-goals distribution and leaves 1X2 about where it was. Carried to
      M4 (combined model); the 1X2 gate there (D18 form) is a separate, harder test.
      **Re-read against a clean reference (2026-11-13).** Reference 42359964241 was fitted
      on older code: its predictions differ from the current code's by up to 0.039 in
      p_home (found by D34, PHASE1B_TASKS.md 7c). It was rerun on commit 9765e73 as run
      43419004255 (`phase1b_strength_open_xg`, 28-day grid, the same refits), and each
      group run's parts were assembled locally against it (`assemble --vs`). Every verdict
      stands. Primary metric now: G1 rest +0.01% [−0.03%, +0.05%] (and mixing now fails,
      R-hat in 4 refits against a limit of 3); G2 travel +0.01% [−0.01%, +0.03%];
      G5 importance +0.32% [+0.09%, +0.55%]; G4 referee −0.00% [−0.01%, +0.01%];
      G6 kickoff −0.01% [−0.03%, +0.02%] (its ECE draw failure was drift: now −0.0012;
      it still fails on mixing and the primary metric); **G7 style −0.14% [−0.20%, −0.08%],
      pass**, all warm 1X2 log loss −0.01%, RPS −0.01%, OU2.5 −0.13%, ECE within limits.
      Reference for G3 and M4: run 43419004255.
      **G3 weather: fail (2026-11-15, run 43663032427, commit 1275d6c, snapshot
      962f29fe719a9d58, reference run 43419004255; grid identical).** Snapshot rebuilt at
      − 1 h after the weather download finished (0 jobs left): every dev match 2019–2023
      has weather (n 19,394: ERA5 stand-in 17,743, as-issued forecast 1,633, climatology
      18). 18,591 warm dev matches, 9,628 active (any weather term non-zero). Primary
      total-goals CRPS. (1) mixing **fail: R-hat > 1.01 in 5 refits** (limit 3); ESS < 400
      in 0, divergences in 1. (2) active total CRPS **+0.23% [+0.07%, +0.38%]: fail, and
      worse with the interval excluding 0**. (3) all warm **1X2 log loss +0.02%, RPS
      +0.02% (fail, limit 0), OU2.5 log loss +0.11% and total CRPS +0.12% (fail, limit
      +0.10%)**; bundles −0.03% (France) to +0.09% (Germany). (4) ECE pass (home +0.0039,
      draw −0.0014, away −0.0005, over 2.5 +0.0007). (5) heat −0.045 to +0.025, expected
      sign in 3 of 5 bundles; wind −0.083 to +0.035, 4 of 5; rain −0.024 to +0.058, 1 of 5
      (positive in four bundles, against the expected negative sign); cold −0.209 to
      −0.050 in every bundle (no sign stated), within about 1.8 sd of 0; sd / prior
      0.14–0.74. Every coefficient is within about two sd of 0. Reading: weather adds no
      detectable signal to goal totals once team strength is known, and the four free
      terms per bundle cost sharpness out of sample. The stand-in uses realised ERA5
      weather (D29), which flatters the group rather than penalising it, so a fail here is
      not an artefact of forecast error. Recorded as failed; the group stays off by
      default. The D36 combined gate printed for this run (G3 alone) fails on both routes,
      for information only.
      **M3 complete: G7 style is the only group that passes**; G1, G2, G3, G4, G5 and G6
      fail. M4 therefore starts from G7 alone (owner to confirm before any M4 run).
      Pipeline note: rebuilding the snapshot with the full weather first failed on the
      database's 5-minute idle-in-transaction timeout (16,728 ERA5 payloads read one by
      one inside the read transaction). `weather_history.load_realised` now ends the read
      transaction first and fetches in parallel (32 workers; the R2 client pool raised to
      match); the rebuild took 6 minutes.
- [ ] M4 Combined model: gate, weekly dev run, feasibility.
      - Prerequisites done 2026-11-14 (before G3 and any M4 run). Acceptance stated first:
        (a) `config/phase2_gate.yml` holds the gate with routes A and B exactly as D36 (not `evaluation.yml`: it is in every fit's cache hash, so editing it would discard the cached refits D33 step 2 reuses); `load_config` merges it;
        (b) the verdict passes on route A, on route B, on both, and fails when 1X2 is a
        hair worse (route B), when a league segment is over 1.0%, and when route B's ECE is
        over +0.005; the report names the passing route and holds metrics only; (c) a
        bundle-season split into N jobs scores the same refits once each, with predictions
        equal to the whole run's, and assemble refuses a missing or repeated piece.
        All pass: `adatta/eval/phase2.py` `combined_report` (applied by `assemble --vs` to
        every context model, a single kept group included, next to the per-group report),
        `tests/test_phase2_report.py` (+8); `backtest run --part K/N` and the workflow's
        `parts` input (`["0/2","1/2"]` halves each bundle-season), `fetch_part` finds a
        split run's pieces, `check_parts` in assemble, `tests/test_backtest.py` (+2).
        `holdout.yml` is not split yet: the Phase 2 holdout run (M5) needs the same input.
      - 2026-11-15: the owner confirmed M4 with G7 style alone (the only group that passed
        M3). Preview, no new fits: G7's 28-day run (43372042130) assembled locally against
        the clean 28-day reference (43419004255) with the D36 gate: **pass via route B**
        (total CRPS −0.14% [−0.20%, −0.08%], OU2.5 log loss −0.13% [−0.19%, −0.06%], 1X2 log
        loss and RPS −0.01% each, ECE within +0.005, 50 league segments, worst +0.09%);
        route A fails (1X2 −0.01% [−0.03%, +0.01%]). Indicative only: the gate is judged on
        the weekly run.
      - Item 2, weekly dev run: dispatched 2026-11-15, run 43691182011 (commit 1275d6c),
        `groups` G7, snapshot 962f29fe719a9d58, weekly grid, `every_days` 7, 2,000 draws,
        jobs split in halves, `vs` 43421580602 (`phase1b_strength_open_xg`, the weekly 1b
        reference of D33 step 1).
        **Result (2026-11-15): pass.** All 50 refit jobs and assemble succeeded on the first
        attempt. D36 gate on 18,682 warm matches: **pass via route B** (total CRPS −0.13%
        [−0.18%, −0.07%], OU2.5 log loss −0.11% [−0.18%, −0.05%], 1X2 log loss and RPS
        −0.00%, ECE home −0.0003, draw +0.0008, away +0.0008, over 2.5 −0.0016; 50 league
        segments, worst league 136 OU2.5 log loss +0.09%); route A fails (1X2 log loss
        −0.00% [−0.02%, +0.01%]). Group report for G7 passes (R-hat over in 5 refits, limit
        9; divergences in 2 refits, limit 2, at the limit; active total CRPS −0.12% [−0.18%, −0.06%]).
        Tempo coefficient positive in every bundle (+0.16 Italy to +0.44 Spain, sd/prior
        0.59-0.68). These match the 28-day preview.
      - Item 3, leakage. Acceptance stated first: through the backtest harness end to end,
        (a) every training row's context read by a refit has `ctx_as_of` <= the refit's
        as-of and kickoff + lag <= as-of, and every target's context row has `ctx_as_of` <=
        kickoff − 1 h; (b) changing every context value observed after a time T leaves
        every prediction made by T unchanged, while later predictions move (so the check
        can fail); the fake fitter's coefficients depend on the training design, so a leak
        into a fit shows. **Pass:** `tests/test_phase2_leakage.py` (2 tests, G7 and G3
        terms); a deliberate leak (training 60 days past each refit) makes (b) fail.
      - Item 1, gate tooling. Acceptance stated first: (a) `adatta.model.gate --group G7`
        (`--context` digest, default latest) fits the production 1b spec plus the group's
        terms on the same audit dates and seed, training rows and targets with their own
        context rows at kickoff − 1 h, and the group's coefficients enter the headline, so
        the x0.5 / x2 prior-sensitivity checks cover them; the report names the groups and
        the snapshot. (b) Without `--group` the gate is unchanged: same fit, headline, trace
        tag and report fields. (c) `gate.yml` takes `groups` and `context` inputs (default
        empty / latest).
        **Built, tests pass** (`tests/test_gate_groups.py`, 3 tests: the 1b trace tag
        unchanged, every group coefficient in the headline after the 1b names, targets
        scored with their own context row). `gate.yml` now carries the R2 settings (the
        snapshot is read from the store). The fit itself needs PyMC, which runs only in CI
        since the local Docker engine stopped (2026-11-12), so the first real check is the
        audit-sample run: `gate.yml` with `groups` G7 and `context` 962f29fe719a9d58, after
        the weekly run (one at a time) and once this change is pushed. Dispatched
        2026-11-15 after the weekly run passed: run 43730396857 (commit 6e7d753).
        **Result: fail.** All 25 jobs ran (4 chains x 2,000 draws, seed 20261105); 8 of 25
        fits pass every gating check, 17 fail, all on parameter prior sensitivity of
        `beta_style` (posterior mean moves > 1 posterior sd when every prior scale is x0.5
        or x2): x0.5 shifts 1.01-2.45 sd, x2 1.10-1.93 sd. By bundle: Spain 5 of 5 fail
        (worst 2.45 sd; the 2023 fit also has 2 divergences), France 5 of 5, Germany
        4 of 5, England 2 of 5, Italy 1 of 5. Every other check passes in every fit
        (R-hat, ESS, PPC per league, identifiability, truncation, reproducibility; a few
        total-dispersion warnings in 135 and 136, non-blocking as in 1b), and predictive
        sensitivity passes everywhere (4-week 1X2 log loss moves < 0.2%). Reading: with
        the coded covariate's small spread (`tempo` = home + away `tempo_rel`, sd about
        0.09) the Normal(0, 0.2) prior is informative on `beta_style` (posterior sd / prior
        sd 0.59-0.68 in the weekly run), so the coefficient's value depends on the prior
        even though the forecasts barely do. The rule (no headline posterior mean moves > 1
        sd) is unchanged and gates; **M4 fails on item 1, so the Phase 2 holdout is not
        opened.** Next is the owner's decision: stop Phase 2 without a promotion, or a dev
        respecification of G7 (for example, the covariate on a stated unit scale like the
        other groups, keeping Normal(0, 0.2)) rerun through M3, M4 and the gate on the dev
        window only, with the holdout as the guard against having chosen it after this
        result.
      - Item 4, feasibility tooling. Acceptance stated first: (a) `assemble --vs` reports,
        for every context model, the median refit time per bundle (each distinct refit
        once, `refit_fit_s`, cached refits at their original fit time) against the
        reference's, and passes a bundle when the ratio is at most 1.25; the report names
        the worst bundle and holds metrics only; (b) a model or reference without fit
        times is reported as not measured, never as a pass. The 6-hour job limit is read
        from the run's job durations (no job over 360 min, none rerun for a timeout).
        **Built, passes:** `phase2.feasibility` and its markdown, written by `assemble --vs`
        as `feasibility_<model>.json` next to the D36 gate; `tests/test_phase2_report.py`
        (+2). Indicative, on the 28-day runs (G7 43372042130 against 43419004255, run on
        different days): England 1.35 (fail), France 1.25, Germany 0.98, Italy 0.76, Spain
        0.98. Italy 24% faster with one more coefficient points to runner-to-runner timing
        noise of the order of the limit itself, so a fail on item 4 needs a look at whether
        it is the model or the runners before it is read as infeasible (raised for the owner;
        the criterion is not changed). Judged on the weekly run against 43421580602.
        **Result (2026-11-15): pass**, from 43691182011 re-assembled locally against
        43421580602 (the run was dispatched before this report existed; same gate verdicts
        as its CI assemble). Median refit time ratio: England 1.10 (474 s vs 432 s), France
        0.95, Germany 1.04, Italy 1.24 (513 s vs 413 s; worst, close to the 1.25 limit and
        within the runner noise noted above), Spain 0.93. Longest job 224 min (England
        2023, half 0), mean 144 min, none over 360 and none rerun.
- [x] M4b G7 respecified on the dev window (owner, 2026-11-15, after M4 failed item 1).
      Disclosed: this change is chosen after seeing the gate result, on dev data only; the
      locked holdout, still unopened, is the guard against that choice.
      Change: G7 is coded on a stated unit scale like the other groups, `tempo` =
      (home `tempo_rel` + away `tempo_rel`) / 0.1, a fixed constant (no data statistic);
      prior Normal(0, 0.2) per unit unchanged, so a priori one sd is a 20% change in the
      goal rate per 0.1 of combined relative tempo. On the old scale this is Normal(0, 2):
      the old Normal(0, 0.2) per unit of a covariate with sd about 0.09 said a priori that
      the effect per sd is within about +-4%, a strong prior on the quantity being
      measured, which is why the coefficient moved with the prior. Nothing else changes
      (groups, snapshot 962f29fe719a9d58, 1b spec, gates, criteria). `covariates.py` is in
      the fit hash, so every config hash changes once (the production spec's included: its
      next live refit fits from scratch; its predictions are unchanged, it reads no
      covariates). Acceptance stated first, each step only if the one before passes, one
      heavy run at a time: (a) code and tests: the style block equals the old one / 0.1,
      the other groups' blocks and the 1b spec are unchanged; (b) gate.yml with G7 on the
      25-fit audit sample: every gating check passes in every fit (M4 item 1, unchanged);
      (c) the 28-day group run against 43419004255 passes the per-group acceptance (M3,
      unchanged); (d) the weekly dev run against 43421580602 passes D36 and feasibility
      (M4 items 2 and 4, unchanged). Then M5 as planned. A fail at any step stops here
      and Phase 2 ends without a promotion (proposed, owner to confirm: no second
      respecification of G7 on the same dev window).
      - (a) passes: `covariates.TEMPO_UNIT` 0.1, tests updated (commit 5b2ef5e).
      - (b) **passes**, gate run 43737999116 (commit 5b2ef5e, snapshot 962f29fe719a9d58,
        same audit dates and seed): 25 of 25 fits pass every gating check, 0 divergences in
        every fit, R-hat at most 1.008. Parameter prior sensitivity worst 0.68 posterior sd
        at x0.5 (England 2024) and 0.21 at x2; `beta_style` is no longer the worst
        coefficient in any fit (the worst are the 1b spec's own: `kappa`, `mu_league`,
        `sigma_strength`, `h_league`, `xg_bias`). Non-blocking warnings as in 1b:
        total-dispersion PPC in some leagues, and the 4-week predictive proxy in 2 fits.
      - (c) dispatched 2026-11-15: run 43742923943 (commit 5b2ef5e), `groups` G7, snapshot
        962f29fe719a9d58, `every_days` 28, 2,000 draws, `vs` 43419004255.
      - (c) **passes** (run 43742923943, grid identical). 18,591 warm dev matches, 18,487
        active. (1) mixing pass: R-hat > 1.01 in 3 refits (reference 1 + 2, at the limit;
        worst 1.016), ESS < 400 in 0, divergences in 0. (2) active total CRPS **−0.15%
        [−0.25%, −0.05%]: pass**. (3) all warm 1X2 log loss −0.01%, RPS −0.01%, OU2.5 log
        loss −0.14%, total CRPS −0.15%; bundles' 1X2 log loss −0.10% (Spain) to +0.04%
        (England), all within limits. (4) ECE pass (home −0.0006, draw −0.0004, away
        +0.0013, over 2.5 +0.0015). (5) tempo **positive in every bundle**, +0.026 (Italy)
        to +0.073 (Spain) per 0.1 of combined relative tempo, 1.7 to 4.6 sd from 0, sd /
        prior 0.07–0.09 (the data, not the prior, set it). Per unit of the old scale that is
        +0.26 to +0.73, against +0.16 to +0.44 under the old prior: the old prior was
        shrinking the coefficient, as the gate failure said. Printed for information, not
        step (c)'s test: the D36 gate passes via route B (CRPS −0.15% [−0.25%, −0.05%],
        OU2.5 −0.14% [−0.25%, −0.04%], 50 league segments, worst +0.14%), and the
        refit-time ratio is 1.27 in France (limit 1.25; others 0.98–1.06). Feasibility is
        judged on the weekly run (d), where France was 0.95 before the respecification.
      - (d) dispatched 2026-11-15: run 43760181569 (commit 5b2ef5e), `groups` G7, snapshot
        962f29fe719a9d58, weekly grid, `every_days` 7, 2,000 draws, parts halves, `vs`
        43421580602.
      - (d) result (2026-11-16): **D36 passes, feasibility fails as written; owner to
        decide.** All 51 jobs succeeded on the first attempt (longest 217 min, mean 146,
        none over 360, none rerun). D36 gate on 18,682 warm matches: **pass via route B**
        (total CRPS −0.14% [−0.24%, −0.04%], OU2.5 log loss −0.13% [−0.24%, −0.03%], 1X2
        log loss −0.02% and RPS −0.03%, ECE home +0.0006, draw +0.0002, away +0.0008,
        over 2.5 −0.0017; 50 league segments, worst league 136 OU2.5 +0.19%); route A
        fails on log loss (−0.02% [−0.05%, +0.00%]), RPS alone passes (−0.03% [−0.05%,
        −0.00%]). Feasibility (median refit time ratio, limit 1.25): England 1.19, France
        1.05, **Germany 1.29 (440 s vs 340 s): fail**, Italy 1.06, Spain 0.98.
        The look item 4 asks for before a fail is read as infeasible (model or runners),
        from the runs' refit times in R2, metrics only: per job (one runner each), the
        Germany ratio against the reference is 0.50 to 1.85 across its 10 jobs, for the
        same model; the old-scale G7 run (43691182011) shows the same spread (0.68 to
        1.42) and passed with Germany 1.04 and Italy 1.24, where this run has 1.29 and
        1.06. The same model refitting the same dates on the old and new scale differs by
        a median 0.89 (Italy) to 1.17 (England) per refit. Runner-to-runner speed varies
        by more than the 25% margin, so this run cannot tell a 29% cost from noise; the
        respecification changes one covariate's scale and adds no parameter. Not changed:
        the criterion. A paired timing, both specs on the same runner over the same
        dates, would separate the two (owner's call, acceptance to be stated first).
        For information, outside (d)'s test: the per-group report fails on mixing, R-hat
        > 1.01 in 11 of 928 refits against a limit of 9 (reference 7 + 2; worst 1.017;
        divergences in 2, at the limit); active total CRPS −0.14% [−0.23%, −0.04%]; tempo
        positive in every bundle, +0.025 to +0.073 per 0.1, sd / prior 0.07–0.09.
        Promotion file not drafted: (d) has not passed.
      - (d') Paired timing (owner, 2026-11-16: "Paired timing test"), deciding item 4 for
        M4b. Acceptance stated first, before any fit. Dates fixed in advance: in each dev
        season (2019–2023) of each bundle's weekly plan, the 3 refits at positions
        floor(n/6), floor(n/2), floor(5n/6) of that season's n refits, so 15 dates per
        bundle, the same rule for every bundle. (a) Tool: one job per bundle-season, each
        date fitted on that job's runner by both the 1b production spec and the G7
        challenger (snapshot 962f29fe719a9d58), fresh (no refit cache read or written),
        production sampler settings and seed, the order alternating by date (reference
        first on even positions); a fit's time is measured as `refit_fit_s` is (sampling
        and convergence numbers, compilation included). Tests: the date rule, the
        alternation, no cache use, the verdict. (b) Verdict, item 4's rule on paired
        runners: per bundle, the median challenger time over its 15 dates divided by the
        median reference time over the same 15 dates is at most 1.25; **all five bundles**
        are tested and must pass (not Germany alone: the same noise may have flattered a
        bundle that passed). The median of the per-date ratios is printed for information.
        (c) A bundle with a missing date or season is "not measured", never a pass; a
        failed job may be rerun. Decision: (b) passing in every bundle reads item 4 as
        passed and the weekly run's Germany 1.29 as runner noise, and M5 follows; any bundle
        over 1.25 fails item 4 and Phase 2 ends without a promotion. Metrics only in the
        public artifact (dates, fit times, convergence numbers).
        Built (commit 2ab45e6): `adatta/model/paired_timing.py`, `timing.yml`,
        `tests/test_paired_timing.py` (10 tests). Dispatched 2026-11-16: run 43811483942,
        `groups` G7, snapshot 962f29fe719a9d58.
        **Result (2026-11-16): pass.** All 25 jobs and the report succeeded; 75 paired
        dates. Median fit time, challenger vs reference on the same runners: England 462 s
        vs 450 s (1.03), France 509 vs 494 (1.03), Germany 348 vs 340 (1.02), Italy 540 vs
        534 (1.01), Spain 442 vs 433 (1.02); median paired ratio 1.02-1.04. Per-date
        ratios 0.45-1.16; the order did not matter (median 1.02 with either spec first).
        Convergence on these fits: worst R-hat 1.011 (reference) and 1.010 (challenger),
        divergences 0 and 1. Per the decision stated before the run: item 4 passes, and
        the weekly run's Germany 1.29 is read as runner noise. **M4b complete: (a)-(d)
        pass.** Next: M5, the owner fills `config/phase2_promotion.yml` (G7 style,
        snapshot 962f29fe719a9d58) and dispatches the second holdout opening.
- [x] M5 Promotion per D30.
      - Holdout jobs split (prerequisite, 2026-11-15). Acceptance stated first: (a)
        `holdout.yml` takes a `parts` input (default `["0/1"]`, today's behaviour) and runs
        each holdout bundle-season as that many jobs (`--part`), within the same 18-job
        budget as `backtest.yml`; (b) a holdout run split in two assembles to the same
        predictions, gate verdict and metrics as the whole run, and assemble refuses a
        split holdout with a missing piece. **Built, passes:** `holdout.yml` `parts` input
        and matrix (max-parallel 18, was 10); `tests/test_holdout.py`
        `test_a_split_holdout_run_assembles_like_the_whole_run` (same predictions; same
        verdict and checks, values equal up to summation order; a missing piece refused).
        Still open for M5: the Phase 2 spec on the
        holdout (groups, context snapshot), the second claim (D30) and the D36 gate.
      - Second opening, plumbing (2026-11-15, built before M4 is judged; nothing is run on
        the holdout). Two facts shape it: the 1b holdout run (42983498620) used code f35b32c,
        whose predictions differ from today's (D34: up to 0.039 in p_home), so the Phase 2
        run scores the 1b production spec again on current code as its reference, paired by
        match on the same grid; and item 5's "written into `config/evaluation.yml`" is met by
        `config/phase2_gate.yml` (D36, recorded 2026-11-14), since evaluation.yml is part of
        every fit's cache hash. Acceptance stated first: (a) `config/phase2_promotion.yml`
        pins the challenger, its groups and its context snapshot digest; empty until the
        owner fills it after M4; not in the fit cache hash; loaded with the evaluation
        config. (b) The Phase 2 opening has its own marker (`backtest/holdout/
        opened_phase2.json`), claimed once, only after the 1b opening, and never touching the
        1b marker; a run that claimed it may score exactly two specs, the production spec
        and the pinned promotion spec (its snapshot pinned too: `latest` refused), and
        nothing else; the 1b opening behaves as before. (c) `assemble --holdout --opening
        phase2` needs both models on every holdout bundle-season and the 1a grid, applies
        the D36 gate (`phase2_gate.yml`, routes A and B, league segments) to the challenger
        against the rescored 1b reference, writes gate.json with the verdict (champion or
        rejected) and the passing route, and records the challenger's `model_run` row
        (component phase2, window holdout) with that verdict. (d) `holdout.yml` takes an
        `opening` input (phase1b or phase2, its own confirm phrase) and, for phase2, runs
        both specs. Tests with fakes cover (a)-(c), including refusals.
        **Built, passes (a)-(d).** `config/phase2_promotion.yml` (empty), `harness.
        Phase2Promotion`; `holdout.MARKERS` and `claim` / `unlock` per opening
        (`HoldoutKey.opening`); `backtest.promotion_spec` and `holdout_specs`, the run guard,
        the phase2 branch of `assemble` (gate.json with verdict, routes and checks; the
        report's "Phase 2 promotion (D30, D36)" line after the combined-model section;
        manifest `phase2_holdout`), `record` component phase2 for context models; CLI
        `--opening` on claim-holdout, run and assemble, `run --promotion` (groups and snapshot
        from the config only). `holdout.yml`: `opening` input (default phase2), its confirm
        phrase "open the locked holdout for phase 2 once", the `spec` matrix (production and
        promotion). `tests/test_holdout.py` +3 (markers, spec guard, end to end with record).
        Before the opening: M4 passes, the owner fills `phase2_promotion.yml` (G7 style,
        snapshot 962f29fe719a9d58 unless rebuilt) and commits it, then dispatches with
        `parts` halves (the weekly fits of the biggest bundle-seasons need them).
      - Promotion pinned by the owner after M4b (commit f72aa2f): groups [style], context
        962f29fe719a9d58. **Second opening made 2026-11-16 07:40 UTC (owner: "open the
        holdout"): run 43825480177** (commit f72aa2f), opening phase2, parts halves; the
        claim job succeeded. 40 refit jobs (5 bundles x 2 holdout seasons x 2 halves x 2
        specs), then assemble applies the D36 gate.
      - **Result (2026-11-16): rejected; Phase 2 ends without a promotion.** All 40 refit
        jobs and assemble succeeded (none rerun; longest 325 min of 360). 373 refits per
        model, none with R-hat > 1.01, ESS < 400 or divergences (worst R-hat 1.008 and
        1.007). On 7,261 warm holdout matches, style vs the 1b reference rescored on the
        same code and grid, paired by match: **D36 fails on both routes.** Route A: 1X2 log
        loss −0.01% [−0.04%, +0.02%], RPS −0.02% [−0.06%, +0.01%], neither interval
        excludes zero. Route B: total CRPS −0.06% [−0.16%, +0.05%], OU2.5 log loss −0.04%
        [−0.15%, +0.08%], neither excludes zero, and ECE over2.5 +0.0058 (0.0270 vs 0.0213)
        is over the +0.005 limit. League segments pass (50, worst +0.27% OU2.5 log loss in
        league 78). Group check fails the same way (active-match CRPS −0.05% [−0.16%,
        +0.05%]; ECE over2.5 +0.0056). Feasibility as written fails on Spain (median refit
        607 s vs 453 s, 1.34; the other four 1.01–1.16); not deciding, since the gate
        already fails. Tempo coefficients are positive in every bundle (+0.007 Italy to
        +0.054 Spain). gate.json verdict "rejected"; one `model_run` row recorded
        (component phase2, window holdout). Production stays `phase1b_strength_open_xg`.
        For information, 1b vs 1a on the same holdout and current code: 1X2 log loss
        −0.48% [−0.76%, −0.20%], RPS −0.76% [−1.19%, −0.32%], so the 1b promotion holds.
        The holdout has now been opened twice (1b, Phase 2); whether a later context claim
        goes to the live period (D30 (b)) is the owner's call.

# Gate policy (D59): how a challenger is promoted

**Status: approved 2026-11-27 (owner: "agreed with all"), the recommended option in each choice:** m = +0.10%, route 2T yes, half-season live windows, a floor-check failure raised to the owner. The route checks are coded, with tests, before the first challenger uses them.

The owner agreed the direction on 2026-11-27, after a review of what was rejected. Each owner
choice below lists the recommended option first. Nothing changes until approval, and the
UEFA holdout now running keeps its own pre-registered gate (`config/uefa_gate.yml`).

## Why

The D18 gate asks for proof that a challenger is better: the paired 95% interval of its gain
must exclude zero. That is the right rule for claiming an effect is real. It is the wrong rule
for choosing what to serve, because a challenger whose best estimate is better, and whose
worst plausible case is only trivially worse, gives the better expected forecast.

Two seasons of holdout can detect only gains of about 0.3-0.5% or more, so several
challengers lost on power rather than on merit:
- the full model on the eight C2 leagues: -0.38% [-0.81%, +0.06%];
- A3 on the league matches: -0.04% [-0.08%, +0.00%];
- C1 on the five alone: about -1%, intervals including zero.

Others failed on substance and stay failed:
- P3-d's lineup offset learned a negative weight;
- P3-i was degenerate;
- B3' was miscalibrated on the holdout;
- the style term failed its over-2.5 calibration check.

## The routes

A challenger is promoted against the **current production model** for its competitions, on a
window that it has never been tuned on: an unopened holdout or a live period. Every route
also needs all of the **safeguards** below.

**Route 1, proven better (D18, unchanged).** 1X2 log loss and RPS both improve, each with
the paired 95% interval excluding 0.

**Route 2, non-inferior.**
- 1X2 log loss and RPS are each lower than production's on the point estimate.
- The upper end of each paired 95% interval, relative to production, is below the margin
  **m = +0.10%** (owner choice 1).

**Route 2T, non-inferior on totals (owner choice 2).** For terms that act on total goals
(weather, tempo), D36's route B is applied in the same form:
- total-goals CRPS and OU2.5 log loss are each better on the point estimate, with each
  upper bound below m;
- 1X2 log loss and RPS are no worse on the point estimate;
- the ECE of each 1X2 outcome and of OU2.5 is within +0.005 of production's.

**Route 3, a blend.** Component models are combined by stacking their predictive
distributions.
- The weights are fitted on dev only and fixed before the judging window opens.
- The blend is judged once, as one challenger, by route 1, 2 or 2T.
- A component need not pass alone. Each one must pass the inference gates and the leakage
  tests, and a component with a weight near zero is reported, not dropped by hand.

**Models with their own markets** (cards, corners, goal types, scorers) state their primary
metric in their acceptance, and routes 1 and 2 apply to that metric in the same way.

## Safeguards (every route; unchanged from today)

1. Inference gates: R-hat, ESS, divergences, PPC including dispersion, prior sensitivity,
   identifiability (§14.5).
2. Calibration: 1X2 ECE per outcome within its noise floor.
3. Segments: no league x market segment worse than production by more than 1.0% (D28, point
   estimate).
4. Feasibility: it runs within the production budget.
5. Leakage tests pass.
6. Pre-registration: the acceptance, the window and the route are written before any score
   on that window is read.

## Guards against creep

- **One step at a time.** A challenger is always compared with the model currently served,
  never with an older one. Two challengers are never promoted together unless they are
  judged as one (a blend, or a combined model).
- **The floor check.** On every judging window, production must stay **provably** better
  than Phase 1a (route 1 form). On the five's holdout today it is: -0.48% [-0.76%,
  -0.20%]. If production fails this, the result is raised to the owner (owner choice 4).
- **No re-judging.** Holdouts already opened are not re-scored under this policy. Past
  rejects come back as new, pre-registered challengers, judged on the live period.

## Power, stated in every acceptance

Every acceptance states the smallest gain its window can detect at 80% power. It uses the
dev paired-difference spread and the window's expected match count. When the plausible gain
is smaller than that, the acceptance says so and chooses route 2, route 3 or a longer live
window, rather than a route 1 test it cannot pass.

## Per-league decisions

Where a model is decided league by league (for example C2), each league is judged alone,
by the route its acceptance names. The pooled result is reported and decides nothing.
Picking leagues after seeing a pooled result is not allowed.

## Live windows (owner choice 3)

A live judging window is fixed in advance: a start date, an end date and a minimum match
count. It is read once, at its end, with no interim looks that could stop it early.
Default: one half-season (for 2026/27, the matches from approval to the winter break).
The acceptance may set a longer window.

## Owner choices

1. **Margin m:**
   - (a) +0.10% (recommended): about a fifth of 1b's whole gain over 1a;
   - (b) +0.05%;
   - (c) +0.25%.
2. **Route 2T for totals:**
   - (a) yes (recommended): weather, tempo and referee terms act on totals and cards, not
     1X2;
   - (b) 1X2 only.
3. **Live window default:**
   - (a) a half-season with a minimum count set per acceptance (recommended);
   - (b) a full season.
4. **If the floor check fails:**
   - (a) raise to the owner, nothing changes automatically (recommended);
   - (b) revert serving to Phase 1a automatically.

## On approval

- `docs/DECISIONS.md` D59 records the choices.
- The Phase 1b gate line in `CLAUDE.md` gains the routes, with the owner's sign-off on that
  edit.
- The gate code gains route 2, 2T and 3 checks with tests, before any challenger uses them.

## A register of every test (D70, approved 2026-11-28: "approve d70", the recommended choices)

**Why.** Each gate is sound alone: a route 1 pass on a 95% interval is wrong about 2.5% of
the time when the challenger is truly no better. Across a long backlog those chances add
up, and the risk is highest when several challengers are judged on the same matches (the
2026/27 live period now holds P3-j, P3-h2, C1 v2, Stage B and the C2 leagues). Nothing
records how many tests have been run, so nobody can say how many passes to expect by luck.

**Proposed:**
1. **A register**, `docs/TEST_REGISTER.md`: one row per judged test, opened when its
   acceptance is pre-registered and closed when it is read. The register is append-only;
   a closed row is never edited.
   - Columns: item, decision row, window and its dates, route, n, the headline difference
     and interval, the detectable gain, pass or fail, and what it changed.
   - Every judged test gets a row: dev gates that decide go or stop, holdout and live
     verdicts. Fails and abandoned tests count too.
   - The rows to date are backfilled from the task docs (counts and verdicts only, no
     match-level data).
2. **A family rule for shared windows.**
   - Tests that promote into the same served market and are read on the same window form
     a family: for example two challengers to the 1X2 forecast judged on 2026/27.
   - A family's k tests each use a 1 − 0.05/k interval (Bonferroni; 97.5% for two), stated
     in each acceptance before the window is read.
   - Tests on different windows, or for different markets, stay at 95%.
3. **A running count** in the register's header: the tests read, the passes, and the
   expected false passes if every challenger were no better (the sum of each test's
   one-sided error).

**What it costs:** the register and backfill are a documentation task (about half a day);
the family rule changes no gate already pre-registered. P3-j's live gate is alone in its
family (the 1X2 forecast, lineups known) unless another 1X2 challenger is registered on
2026/27, which would then share it at 97.5% each.

**Owner choices:**
1. **The register:**
   - (a) as above (recommended);
   - (b) not kept.
2. **Stricter intervals:**
   - (a) the family rule for shared windows only (recommended): it targets the real
     multiplicity risk and leaves independent tests alone;
   - (b) every promotion at 99% from now on;
   - (c) none: the register only.

## A quick check before a full test (improvement 3, built 2026-11-28)

`python -m adatta.eval.quick_check --signal <parquet> --expect +|-|0` checks a candidate
team-match signal on the dev seasons in about a minute: no PyMC, no CI, 0 API requests.
- **Input:** a parquet of match_id, team_id, value and known_at. A value known after
  kickoff is refused and counted.
- **Method:** the Phase 1a point fit on the 28-day grid. It reports the out-of-sample
  Poisson slope of goals on the signal beyond strength, and the paired 1X2 log loss and
  RPS gain with b fitted per refit on training rows.
- **It is guidance only.** It decides nothing, and every pre-registered test stays as it is.

Checked against P3-j's known results:

| signal | quick check (57 s / 44 s) | full test |
|---|---|---|
| S_miss | worth a full test: log loss −0.061% [−0.101%, −0.021%], RPS −0.106% | step 2 (J, with S_rat): −0.07%, −0.12% |
| S_apm | unlikely: slope interval spans 0, log loss +0.000% | step 1: fail, dropped |

A new signal goes through it before its acceptance is drafted. Its result is stated in
the draft and recorded as context, not as a test, so it takes no register row.

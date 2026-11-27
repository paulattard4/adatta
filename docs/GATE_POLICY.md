# Gate policy (D59): how a challenger is promoted

**Status: drafted 2026-11-27, not approved.**

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

# Test register (D70)

Every judged test, one row each: opened when its acceptance is pre-registered, closed when
it is read. Append-only; a closed row is never edited (a correction is a new row citing
it). Counts and verdicts only, no match-level data.

**What counts as a test:** a dev gate that decides go or stop, a holdout verdict, a live
window verdict. Build checks (tests, coverage, "(a)-(f) pass" of a pipeline) and the quick
check (GATE_POLICY, improvement 3) are not tests.

**Family rule (D70):** tests that promote into the same served market and are read on the
same window form a family. Each of a family's k tests uses a 1 − 0.05/k interval, stated
in its acceptance before the window is read. Tests on different windows, or for
different markets, use 95%.

## Running count

As of 2026-11-28, from the rows below (the backfill covers the result lines recorded in
the task docs; a test found later is added as a new row).

- Tests read: 43 (45 rows: two tests were read again under an approved amendment).
- Passed: 20; failed or rejected: 23.
- **Expected lucky passes if every challenger had been no better:** about 1.1 (2.5%
  one-sided × 43). That is a rough guide, since not every gate is a single interval test.
  So about one of the 20 passes could be luck; the live windows and the floor check are
  the backstop.
- Open, pre-registered, not yet read: 7. Of these, the 2026/27 live families are:
  - **1X2, lineups known:** P3-j alone, so 95%. P3-k (drafted, D71) would join it at
    97.5% each if approved before P3-j is read.
  - **Cup P(advance):** Stage B live alone.
  - **UEFA matches:** C1 v2 alone.
  - **The C2 leagues:** judged league by league (GATE_POLICY "Per-league decisions"),
    one family per league.

## Open (pre-registered, not read)

| # | item | decision | window | route / gate | read |
|---|---|---|---|---|---|
| O1 | P3-j J live shadow vs P | D65 | 2026/27 bundle leagues from the first shadow forecast | D59 route 1 or 2, `config/lineup_j_live_gate.yml` | after 2027-06-15, once |
| O2 | P3-h2 replaced pitch vs last lineup | D64 | 2026-11-27 to the winter break, ≥ 1,000 team-matches | P3-h2 (d) | once eligible |
| O3 | C2 step 5, eight leagues | D61 | 2026/27-2027/28 | `config/c2_league_gate.yml`, two looks at 97.5% | June 2027, June 2028 |
| O4 | C1 v2 UEFA draw term | D62 | 2026/27 | `config/uefa_live_gate.yml` | June 2027 |
| O5 | Stage B live, P(advance) | D63 | 2026/27 cup ties | `config/cups_live_gate.yml` | after 2027-06-15 |
| O6 | P3b-3 cards (D69 b), dev gate | D66, D69 | dev 2019-2023 | P3b-3 (e) | after the dev rerun |
| O7 | P3-f status-informed availability | D39 | live, about seven weeks of captures | P3-f (d) | 2027-01-04 |

## Closed (backfilled from the task docs, 2026-11-28)

| # | date | item | window | result | verdict | source |
|---|---|---|---|---|---|---|
| 1 | 2026-11-06 | Phase 1a probe: openness prior (mechanism) | dev, 18,601 | OU2.5 log loss −0.51%, slope 0.53 to 1.04 | pass | PHASE1B 68 |
| 2 | 2026-11-06 | Phase 1a probe: promoted/relegated bias | dev | relegated 11+ +0.091 ± 0.056 | fail | PHASE1B 120 |
| 3 | 2026-11-06 | Phase 1b vs 1a, 1X2 | dev, 18,591 | log loss +0.03% [−0.05, +0.11] | fail | PHASE1B 264 |
| 4 | 2026-11-07 | D16 promoted-team shifts | dev | log loss −0.02% [−0.04%, +0.00%]; mixing | fail | PHASE1B 350 |
| 5 | 2026-11-07 | xG auxiliary signal | dev, 2,840 informed | log loss −0.60% [−1.00%, −0.20%] | pass | PHASE1B 415 |
| 6 | 2026-11-07 | Recent-change term | dev, 3,264 targets | log loss −0.00% [−0.07%, +0.06%] | fail | PHASE1B 476 |
| 7 | 2026-11-10 | Prior sensitivity ×0.5 (production spec) | dev | every check | pass | PHASE1B 701 |
| 8 | 2026-11-11 | Prior sensitivity ×2 | dev | every check | pass | PHASE1B 707 |
| 9 | 2026-11-11 | Phase 1b locked holdout (run 42983498620) | 2024/25-2025/26, 7,261 | log loss −0.48% [−0.76%, −0.20%], RPS −0.75% | pass (champion) | PHASE1B 741 |
| 10 | 2026-11-13 | D34 widened walk | dev | log loss +0.00%, RPS +0.00% | fail | PHASE1B 977 |
| 11 | 2026-11-14 | D33 results-triggered refits | dev, 18,682 | item 3 passes (no harm) | pass | PHASE1B 897 |
| 12 | 2026-11-12 | Phase 2 G1 rest | dev | active log loss −0.02% [−0.10%, +0.07%] | fail | PHASE2 309 |
| 13 | 2026-11-12 | Phase 2 G2 travel | dev | no gain | fail | PHASE2 322 |
| 14 | 2026-11-12 | Phase 2 G5 importance | dev | log loss +0.02%, RPS +0.06% | fail | PHASE2 336 |
| 15 | 2026-11-12 | Phase 2 G4 referee | dev | total CRPS +0.02% [−0.00%, +0.04%] | fail | PHASE2 352 |
| 16 | 2026-11-13 | Phase 2 G6 kickoff | dev | mixing and CRPS | fail | PHASE2 365 |
| 17 | 2026-11-13 | Phase 2 G7 style | dev | total CRPS −0.12% [−0.18%, −0.05%] | pass | PHASE2 380 |
| 18 | 2026-11-15 | Phase 2 G3 weather | dev | no gain | fail | PHASE2 405 |
| 19 | 2026-11-15 | Phase 2 M4 combined (G7), item 1 | dev | inference gates | fail | PHASE2 505 |
| 20 | 2026-11-15 | Phase 2 M4b G7 respecified | dev | route B, CRPS −0.15% [−0.25%, −0.05%] | pass | PHASE2 575 |
| 21 | 2026-11-16 | Phase 2 holdout (style vs 1b) | 2024/25-2025/26, 7,261 | route A and B intervals include 0 | fail (rejected) | PHASE2 691 |
| 22 | 2026-11-16 | Stage A terms (A1/A2 cup terms) | dev | no term passes | fail | BEYOND 508 |
| 23 | 2026-11-17 | P3-b availability, selection, minutes | dev | (d) checks | pass | PHASE3 367 |
| 24 | 2026-11-17 | P3-c formation and slot roles | dev | (e) checks | pass | PHASE3 534 |
| 25 | 2026-11-18 | P3-d0 player rates | dev | (f) checks | pass | PHASE3 774 |
| 26 | 2026-11-19 | P3-d lineup offset (L vs P) | dev, 18,682 | (e)1-3 | fail | PHASE3 1056 |
| 27 | 2026-11-20 | P3-e goalscorers, first verdict | dev | (e)5 gap, assists E/O | fail | PHASE3 1243 |
| 28 | 2026-11-20 | P3-e goalscorers, after amendments | dev | goals −0.79% vs B0; assists −0.64% | pass | PHASE3 1272 |
| 29 | 2026-11-20 | P3-g projected formation and XI | dev, 16,318 | (d) | fail (closed, D42) | PHASE3 1583, 1665 |
| 30 | 2026-11-20 | P3-h formation view, amendment 1 | dev | (d) | pass | PHASE3 1776 |
| 31 | 2026-11-21 | P3-i0 attack and defence offset | dev | passes by the letter, degenerate k | fail (closed, D44) | PHASE3 2063 |
| 32 | 2026-11-21 | C1 dev (European matches) | dev | gate | fail | BEYOND 1972 |
| 33 | 2026-11-22 | Stage D v0 (national teams) | dev | prior sensitivity | fail | BEYOND 2809 |
| 34 | 2026-11-24 | A3 cup strength | dev, 816 ties | log loss −0.10% [−0.66%, +0.45%] | fail | BEYOND 699 |
| 35 | 2026-11-24 | Stage D v2, check 2 as first stated | dev | one Monte Carlo comparison | fail | BEYOND 3470 |
| 36 | 2026-11-24 | Stage D v2 under amended check 2 | dev and opened window | all five checks | pass | BEYOND 3542 |
| 37 | 2026-11-25 | P3b-1 half split, dev | dev | check 2 (S0 chosen) | fail | PHASE3B 228 |
| 38 | 2026-11-25 | P3b-1 half split, window (i) | 2026/27 to 2026-11-25 | gating checks | pass | PHASE3B 295 |
| 39 | 2026-11-25 | C2 step 1 | dev | (a)-(e) | pass | BEYOND 1222 |
| 40 | 2026-11-25 | C2 step 2, D28 segments per top flight | dev | all eight | pass | BEYOND 1655 |
| 41 | 2026-11-27 | UEFA holdout (C1) | holdout | check 4, draw ECE 0.043 vs 0.040 | fail | BEYOND 2239 |
| 42 | 2026-11-27 | C1 v2 dev check (c), rerun | dev | (c) | pass | BEYOND 2488 |
| 43 | 2026-11-27 | P3-j step 1, three signals | dev, 38,508 team-matches | S_miss, S_rat pass; S_apm fails | pass (2 of 3) | PHASE3 2206 |
| 44 | 2026-11-27 | P3-j step 2, J vs P | dev, 18,682 | log loss −0.07% [−0.11%, −0.04%], RPS −0.12% | pass (to shadow) | PHASE3 "Step 2 run" |
| 45 | 2026-11-28 | P3b-3 cards, dev | dev, 17,721 | refits passing (c) 73.9% vs 97% | fail | PHASE3B "Dev run, 2026-11-28" |

Rows 27 and 28, and 35 and 36, are a first verdict and the later verdict under the
amendment the owner approved; the count treats each pair as one test read twice (43 tests,
45 rows). Rows 37 and 38 are two tests (dev, then window (i)).

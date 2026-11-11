# Phase 1b locked holdout — result

**Verdict: champion.** `phase1b_strength_open_xg` (strength + openness D15, xG auxiliary
likelihood, per-league rho D21, per-league draw inflation D22/D23, prior scale 1) beats the
Phase 1a static Dixon-Coles on the locked holdout and passes every pre-registered check.

- Opened once: 2026-11-11, GitHub run 42983498620, code `f35b32c`; claim marker
  `backtest/holdout/opened.json` in R2. Full report (match-level, not in git):
  `backtest/reports/20261111T143819Z/` in R2. `model_run`: status `champion`, window `holdout`.
- Seasons 2024/25 and 2025/26, five bundles (top flight + second tier), weekly refits
  (D11), Phase 1a on the identical grid (checked). 7,261 warm matches; 253 cold-start
  matches reported apart.
- Gate (pre-registered in `config/evaluation.yml`, D18, D28): 1X2 log loss and RPS better
  than 1a with the paired 95% interval below 0; no league worse than +1.0% on 1X2 log loss,
  1X2 RPS, OU2.5 log loss, BTTS log loss or total CRPS. Inference gates: audit attempt 5
  (25/25) and the x0.5 / x2 sensitivity backtests (+0.009%, +0.012%).
- Refits: 373, R-hat over 1.01 in 0, ESS under 400 in 0, divergences in 0 (worst R-hat
  1.008). 2 Spain jobs hit the 6-hour job limit and were rerun from the cache.

## Overall (warm matches, relative change vs Phase 1a; negative = 1b better)

| metric | 1a | 1b | change | paired 95% interval |
|---|---|---|---|---|
| 1X2 log loss | 1.0227 | 1.0179 | -0.48% | [-0.76%, -0.20%] |
| 1X2 RPS | 0.2100 | 0.2084 | -0.75% | [-1.19%, -0.32%] |
| 1X2 Brier | 0.6133 | 0.6101 | -0.51% | [-0.84%, -0.19%] |
| OU2.5 log loss | 0.6881 | 0.6841 | -0.59% | [-0.93%, -0.24%] |
| BTTS log loss | 0.6911 | 0.6893 | -0.26% | [-0.53%, +0.02%] |
| total CRPS | 0.8912 | 0.8863 | -0.56% | [-0.88%, -0.24%] |

## By bundle, 1X2 log loss

| bundle | n | change | 95% interval |
|---|---|---|---|
| England | 1,825 | -1.06% | [-1.74%, -0.39%] |
| Germany | 1,194 | -0.59% | [-1.35%, +0.18%] |
| Spain | 1,625 | -0.25% | [-0.73%, +0.23%] |
| France | 1,172 | -0.18% | [-0.79%, +0.42%] |
| Italy | 1,445 | -0.13% | [-0.70%, +0.43%] |

## Segment rule (D28): worst league per gate metric

All 50 league x metric checks are within +1.0%. The largest adverse moves: Segunda
(141) BTTS log loss +0.70%, Serie A (135) 1X2 RPS +0.38% and 1X2 log loss +0.32%, La Liga
(140) BTTS log loss +0.34%. Spain's bundle-level BTTS log loss is +0.53% [+0.00%, +1.07%],
the one bundle x market where 1b is (marginally) worse; a watch item, not a gate failure.

## Calibration (acceptance item 4)

1X2 reliability by predicted-probability decile is in `reliability.csv` of the R2 report.
Well-populated bins are within about 0.02 of the observed frequency for home, draw and
away; the sparse extreme bins (n < 60) deviate more, as expected from their size.

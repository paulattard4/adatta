# Phase 1b inference gate — england as of 2023-08-07 (strength_open)

**FAIL** (20/22 checks). Code 858d50875cec833f71b978116f5eaf412c4e4c8b-dirty, seed 20261105, 4 x 1000 draws.

| check | pass | value | detail |
|---|---|---|---|
| rhat | **no** | 1.011 | worst open_z[91, 17]; 1 of 16280 over 1.01 |
| ess_bulk | yes | 407.2 | worst sigma_strength |
| ess_tail | yes | 404.1 |  |
| divergences | yes | 0 |  |
| score-matrix truncation | **no** | 0.8506 | share of predictions with tail mass < 0.0001 |
| ppc 39 total_mean | yes | 0.5031 | observed 2.789, PPC median 2.788 |
| ppc 39 home_mean | yes | 0.486 | observed 1.500, PPC median 1.502 |
| ppc 39 away_mean | yes | 0.5286 | observed 1.289, PPC median 1.285 |
| ppc 39 draw_rate | yes | 0.426 | observed 0.226, PPC median 0.229 |
| ppc 39 total_dispersion | yes | 0.7312 | observed 1.067, PPC median 1.040 |
| identifiability 39 | yes | -0.1341 | posterior corr(league level, mean team effect) |
| ppc 40 total_mean | yes | 0.5031 | observed 2.421, PPC median 2.420 |
| ppc 40 home_mean | yes | 0.5005 | observed 1.335, PPC median 1.335 |
| ppc 40 away_mean | yes | 0.4996 | observed 1.086, PPC median 1.086 |
| ppc 40 draw_rate | yes | 0.4084 | observed 0.258, PPC median 0.261 |
| ppc 40 total_dispersion | yes | 0.088 | observed 0.971, PPC median 1.018 |
| identifiability 40 | yes | -0.02295 | posterior corr(league level, mean team effect) |
| prior sensitivity x0.5 | yes | 0.2203 | worst sigma_strength (in posterior sds) |
| predictive sensitivity x0.5 | yes | 0.001318 | 1X2 log loss 1.0115 -> 1.0128 |
| prior sensitivity x2 | yes | 0.1766 | worst reset_weeks (in posterior sds) |
| predictive sensitivity x2 | yes | 0.0007738 | 1X2 log loss 1.0115 -> 1.0107 |
| reproducible seed | yes | 1 | identical draws from the same seed |

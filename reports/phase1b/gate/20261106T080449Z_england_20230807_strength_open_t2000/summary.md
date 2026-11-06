# Phase 1b inference gate — england as of 2023-08-07 (strength_open)

**FAIL** (19/22 checks). Code 7cac32f3be10aec42eeaa9261e4b455116c20b7c-dirty, seed 20261105, 4 x 1000 draws, 2000 tuning.

| check | pass | value | detail |
|---|---|---|---|
| rhat | **no** | 1.015 | worst sigma_walk_open; 3 of 16280 over 1.01 |
| ess_bulk | **no** | 399 | worst sigma_strength |
| ess_tail | **no** | 345.7 |  |
| divergences | yes | 0 |  |
| score-matrix truncation | yes | 1 | share of predictions with tail mass < 0.0001 |
| ppc 39 total_mean | yes | 0.5045 | observed 2.789, PPC median 2.788 |
| ppc 39 home_mean | yes | 0.4891 | observed 1.500, PPC median 1.501 |
| ppc 39 away_mean | yes | 0.5245 | observed 1.289, PPC median 1.286 |
| ppc 39 draw_rate | yes | 0.4206 | observed 0.226, PPC median 0.229 |
| ppc 39 total_dispersion | yes | 0.7228 | observed 1.067, PPC median 1.041 |
| identifiability 39 | yes | -0.1263 | posterior corr(league level, mean team effect) |
| ppc 40 total_mean | yes | 0.4871 | observed 2.421, PPC median 2.423 |
| ppc 40 home_mean | yes | 0.499 | observed 1.335, PPC median 1.335 |
| ppc 40 away_mean | yes | 0.4859 | observed 1.086, PPC median 1.088 |
| ppc 40 draw_rate | yes | 0.4151 | observed 0.258, PPC median 0.260 |
| ppc 40 total_dispersion | yes | 0.084 | observed 0.971, PPC median 1.020 |
| identifiability 40 | yes | -0.07284 | posterior corr(league level, mean team effect) |
| prior sensitivity x0.5 | yes | 0.2193 | worst sigma_strength (in posterior sds) |
| predictive sensitivity x0.5 | yes | 0.001096 | 1X2 log loss 1.0068 -> 1.0079 |
| prior sensitivity x2 | yes | 0.2157 | worst reset_weeks (in posterior sds) |
| predictive sensitivity x2 | yes | 0.0009429 | 1X2 log loss 1.0068 -> 1.0077 |
| reproducible seed | yes | 1 | identical draws from the same seed |

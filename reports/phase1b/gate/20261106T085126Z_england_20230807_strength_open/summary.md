# Phase 1b inference gate — england as of 2023-08-07 (strength_open)

**FAIL** (20/22 checks). Code 13657991a1c96b60baaa7995795a49c4820c6e24-dirty, seed 20261105, 4 x 1000 draws, 1000 tuning.

| check | pass | value | detail |
|---|---|---|---|
| rhat | yes | 1.008 | worst strength_z[104, 121]; 0 of 8221 over 1.01 |
| ess_bulk | yes | 586.3 | worst sigma_strength |
| ess_tail | yes | 623.5 |  |
| divergences | yes | 0 |  |
| score-matrix truncation | yes | 1 | share of predictions with tail mass < 0.0001 |
| ppc 39 total_mean | yes | 0.5022 | observed 2.789, PPC median 2.788 |
| ppc 39 home_mean | yes | 0.4859 | observed 1.500, PPC median 1.502 |
| ppc 39 away_mean | yes | 0.5062 | observed 1.289, PPC median 1.288 |
| ppc 39 draw_rate | yes | 0.4422 | observed 0.226, PPC median 0.228 |
| ppc 39 total_dispersion | yes | 0.7398 | observed 1.067, PPC median 1.038 |
| identifiability 39 | yes | -0.1524 | posterior corr(league level, mean team effect) |
| ppc 40 total_mean | yes | 0.5012 | observed 2.421, PPC median 2.421 |
| ppc 40 home_mean | yes | 0.491 | observed 1.335, PPC median 1.335 |
| ppc 40 away_mean | yes | 0.5064 | observed 1.086, PPC median 1.086 |
| ppc 40 draw_rate | yes | 0.4114 | observed 0.258, PPC median 0.260 |
| ppc 40 total_dispersion | yes | 0.0945 | observed 0.971, PPC median 1.016 |
| identifiability 40 | yes | -0.03887 | posterior corr(league level, mean team effect) |
| prior sensitivity x0.5 | yes | 0.2109 | worst sigma_strength (in posterior sds) |
| predictive sensitivity x0.5 | **no** | 0.002528 | 1X2 log loss 1.0076 -> 1.0101 |
| prior sensitivity x2 | yes | 0.3509 | worst reset_weeks (in posterior sds) |
| predictive sensitivity x2 | **no** | 0.003294 | 1X2 log loss 1.0076 -> 1.0109 |
| reproducible seed | yes | 1 | identical draws from the same seed |

# Phase 1b inference gate — england as of 2023-08-07

**FAIL** (19/22 checks). Code 858d50875cec833f71b978116f5eaf412c4e4c8b-dirty, seed 20261105, 4 x 1000 draws.

| check | pass | value | detail |
|---|---|---|---|
| rhat | **no** | 1.01 | worst attack_z[144, 12]; 1 of 16279 over 1.01 |
| ess_bulk | yes | 718 | worst sigma_defence |
| ess_tail | yes | 1085 |  |
| divergences | yes | 0 |  |
| score-matrix truncation | **no** | 0.8391 | share of predictions with tail mass < 0.0001 |
| ppc 39 total_mean | yes | 0.501 | observed 2.789, PPC median 2.789 |
| ppc 39 home_mean | yes | 0.4946 | observed 1.500, PPC median 1.501 |
| ppc 39 away_mean | yes | 0.5094 | observed 1.289, PPC median 1.288 |
| ppc 39 draw_rate | yes | 0.2333 | observed 0.226, PPC median 0.236 |
| ppc 39 total_dispersion | yes | 0.2562 | observed 1.067, PPC median 1.100 |
| identifiability 39 | yes | -0.586 | posterior corr(league level, mean team effect) |
| ppc 40 total_mean | yes | 0.5002 | observed 2.421, PPC median 2.421 |
| ppc 40 home_mean | yes | 0.5001 | observed 1.335, PPC median 1.335 |
| ppc 40 away_mean | yes | 0.502 | observed 1.086, PPC median 1.086 |
| ppc 40 draw_rate | yes | 0.2385 | observed 0.258, PPC median 0.266 |
| ppc 40 total_dispersion | **no** | 0.00625 | observed 0.971, PPC median 1.057 |
| identifiability 40 | yes | -0.3558 | posterior corr(league level, mean team effect) |
| prior sensitivity x0.5 | yes | 0.2556 | worst sigma_attack (in posterior sds) |
| predictive sensitivity x0.5 | yes | 0.0002075 | 1X2 log loss 1.0157 -> 1.0159 |
| prior sensitivity x2 | yes | 0.1202 | worst sigma_attack (in posterior sds) |
| predictive sensitivity x2 | yes | 0.001051 | 1X2 log loss 1.0157 -> 1.0147 |
| reproducible seed | yes | 1 | identical draws from the same seed |

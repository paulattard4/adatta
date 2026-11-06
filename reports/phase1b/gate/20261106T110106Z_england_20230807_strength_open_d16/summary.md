# Phase 1b inference gate — england as of 2023-08-07 (strength_open)

**FAIL** (18/20 gating checks; warnings don't block). Code 826a4e2ccafc9b183666b899744e0b8be3e45990-dirty, seed 20261105, 4 x 1000 draws, 1000 tuning.

| check | pass | value | detail |
|---|---|---|---|
| rhat | yes | 1.009 | worst strength_z[104, 121]; 0 of 8223 over 1.01 |
| ess_bulk | yes | 509.1 | worst sigma_strength |
| ess_tail | yes | 555.1 |  |
| divergences | yes | 0 |  |
| score-matrix truncation | yes | 1 | share of predictions with tail mass < 0.0001 |
| ppc 39 total_mean | yes | 0.5059 | observed 2.789, PPC median 2.788 |
| ppc 39 home_mean | yes | 0.4897 | observed 1.500, PPC median 1.502 |
| ppc 39 away_mean | yes | 0.5125 | observed 1.289, PPC median 1.287 |
| ppc 39 draw_rate | yes | 0.4116 | observed 0.226, PPC median 0.229 |
| ppc 39 total_dispersion | yes | 0.749 | observed 1.067, PPC median 1.035 |
| identifiability 39 | yes | -0.1435 | posterior corr(league level, mean team effect) |
| ppc 40 total_mean | yes | 0.4957 | observed 2.421, PPC median 2.421 |
| ppc 40 home_mean | yes | 0.495 | observed 1.335, PPC median 1.335 |
| ppc 40 away_mean | yes | 0.4978 | observed 1.086, PPC median 1.086 |
| ppc 40 draw_rate | yes | 0.4014 | observed 0.258, PPC median 0.261 |
| ppc 40 total_dispersion | yes | 0.09475 | observed 0.971, PPC median 1.016 |
| identifiability 40 | yes | -0.05534 | posterior corr(league level, mean team effect) |
| prior sensitivity x0.5 | **no** | 2.264 | worst delta[1] (in posterior sds) |
| predictive sensitivity x0.5 (4-week proxy) | warn | 0.005355 | 1X2 log loss 1.0046 -> 1.0100 |
| prior sensitivity x2 | **no** | 1.433 | worst delta[0] (in posterior sds) |
| predictive sensitivity x2 (4-week proxy) | warn | 0.004006 | 1X2 log loss 1.0046 -> 1.0006 |
| reproducible seed | yes | 1 | identical draws from the same seed |

# Phase 1b inference gate — england as of 2023-08-07 (strength_open)

**PASS** (20/20 gating checks; warnings don't block). Code 7a574d56c9ecc5c79d011030f5db282d702d1cd0, seed 20261105, 4 x 2000 draws, 2000 tuning.

| check | pass | value | detail |
|---|---|---|---|
| rhat | yes | 1.004 | worst strength_z[29, 139]; 0 of 8224 over 1.01 |
| ess_bulk | yes | 961.1 | worst sigma_strength |
| ess_tail | yes | 868.3 |  |
| divergences | yes | 0 |  |
| score-matrix truncation | yes | 1 | share of predictions with tail mass < 0.0001 |
| ppc 39 total_mean | yes | 0.4574 | observed 2.789, PPC median 2.796 |
| ppc 39 home_mean | yes | 0.2734 | observed 1.500, PPC median 1.529 |
| ppc 39 away_mean | yes | 0.6917 | observed 1.289, PPC median 1.266 |
| ppc 39 draw_rate | yes | 0.4177 | observed 0.226, PPC median 0.229 |
| ppc 39 total_dispersion | yes | 0.7596 | observed 1.067, PPC median 1.036 |
| identifiability 39 | yes | -0.1475 | posterior corr(league level, mean team effect) |
| ppc 40 total_mean | yes | 0.4568 | observed 2.421, PPC median 2.427 |
| ppc 40 home_mean | yes | 0.4681 | observed 1.335, PPC median 1.338 |
| ppc 40 away_mean | yes | 0.4725 | observed 1.086, PPC median 1.089 |
| ppc 40 draw_rate | yes | 0.4203 | observed 0.258, PPC median 0.260 |
| ppc 40 total_dispersion | yes | 0.09888 | observed 0.971, PPC median 1.018 |
| identifiability 40 | yes | -0.06704 | posterior corr(league level, mean team effect) |
| prior sensitivity x0.5 | yes | 0.7228 | worst xg_bias[0] (in posterior sds) |
| predictive sensitivity x0.5 (4-week proxy) | warn | 0.002519 | 1X2 log loss 1.0229 -> 1.0255 |
| prior sensitivity x2 | yes | 0.5425 | worst xg_bias[1] (in posterior sds) |
| predictive sensitivity x2 (4-week proxy) | yes | 0.00144 | 1X2 log loss 1.0229 -> 1.0244 |
| reproducible seed | yes | 1 | identical draws from the same seed |

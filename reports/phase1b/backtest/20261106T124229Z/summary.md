# Phase 1b dev backtest — 20261106T124229Z

Refit every 28 days on the shared plan; grid identical to 1a: **yes**. Matches with either team < 10 prior matches are cold start, reported apart.

## Refit diagnostics

| model | refits | R-hat > 1.01 | ESS < 400 | with divergences | worst R-hat |
|---|---|---|---|---|---|
| phase1b_strength_open | 266 | 160 | 22 | 4 | 1.025 |

## phase1b_strength_open vs Phase 1a (relative change; negative = 1b better)

| segment | metric | n | 1a | 1b | rel | 95% interval |
|---|---|---|---|---|---|---|
| all all | 1X2:brier | 18591 | 0.6159 | 0.6161 | +0.03% | [-0.06%, +0.13%] |
| all all | 1X2:log_loss | 18591 | 1.0266 | 1.0269 | +0.03% | [-0.05%, +0.11%] |
| all all | 1X2:rps | 18591 | 0.2100 | 0.2101 | +0.06% | [-0.06%, +0.17%] |
| all all | BTTS:brier | 18591 | 0.4968 | 0.4959 | -0.18% | [-0.41%, +0.05%] |
| all all | BTTS:log_loss | 18591 | 0.6900 | 0.6890 | -0.14% | [-0.31%, +0.03%] |
| all all | OU2.5:brier | 18591 | 0.4886 | 0.4866 | -0.41% | [-0.70%, -0.13%] |
| all all | OU2.5:log_loss | 18591 | 0.6819 | 0.6796 | -0.34% | [-0.55%, -0.12%] |
| all all | total:crps | 18591 | 0.8851 | 0.8815 | -0.41% | [-0.60%, -0.21%] |
| bundle england | 1X2:brier | 4547 | 0.6126 | 0.6126 | +0.00% | [-0.22%, +0.23%] |
| bundle england | 1X2:log_loss | 4547 | 1.0228 | 1.0226 | -0.02% | [-0.22%, +0.18%] |
| bundle england | 1X2:rps | 4547 | 0.2145 | 0.2147 | +0.06% | [-0.23%, +0.34%] |
| bundle england | BTTS:brier | 4547 | 0.5006 | 0.4997 | -0.17% | [-0.66%, +0.32%] |
| bundle england | BTTS:log_loss | 4547 | 0.6938 | 0.6929 | -0.14% | [-0.50%, +0.22%] |
| bundle england | OU2.5:brier | 4547 | 0.4943 | 0.4923 | -0.41% | [-1.00%, +0.19%] |
| bundle england | OU2.5:log_loss | 4547 | 0.6876 | 0.6854 | -0.32% | [-0.76%, +0.12%] |
| bundle england | total:crps | 4547 | 0.8865 | 0.8814 | -0.58% | [-0.98%, -0.17%] |
| bundle france | 1X2:brier | 3421 | 0.6240 | 0.6248 | +0.12% | [-0.09%, +0.33%] |
| bundle france | 1X2:log_loss | 3421 | 1.0382 | 1.0393 | +0.11% | [-0.08%, +0.29%] |
| bundle france | 1X2:rps | 3421 | 0.2126 | 0.2130 | +0.19% | [-0.07%, +0.45%] |
| bundle france | BTTS:brier | 3421 | 0.5019 | 0.4998 | -0.42% | [-0.95%, +0.12%] |
| bundle france | BTTS:log_loss | 3421 | 0.6952 | 0.6930 | -0.33% | [-0.72%, +0.07%] |
| bundle france | OU2.5:brier | 3421 | 0.4954 | 0.4908 | -0.92% | [-1.57%, -0.27%] |
| bundle france | OU2.5:log_loss | 3421 | 0.6890 | 0.6839 | -0.74% | [-1.23%, -0.24%] |
| bundle france | total:crps | 3421 | 0.8873 | 0.8816 | -0.64% | [-1.07%, -0.20%] |
| bundle germany | 1X2:brier | 2968 | 0.6116 | 0.6119 | +0.05% | [-0.18%, +0.28%] |
| bundle germany | 1X2:log_loss | 2968 | 1.0206 | 1.0210 | +0.04% | [-0.16%, +0.25%] |
| bundle germany | 1X2:rps | 2968 | 0.2114 | 0.2116 | +0.13% | [-0.16%, +0.42%] |
| bundle germany | BTTS:brier | 2968 | 0.4817 | 0.4823 | +0.14% | [-0.45%, +0.72%] |
| bundle germany | BTTS:log_loss | 2968 | 0.6746 | 0.6753 | +0.10% | [-0.34%, +0.54%] |
| bundle germany | OU2.5:brier | 2968 | 0.4801 | 0.4774 | -0.57% | [-1.29%, +0.15%] |
| bundle germany | OU2.5:log_loss | 2968 | 0.6731 | 0.6701 | -0.45% | [-0.99%, +0.10%] |
| bundle germany | total:crps | 2968 | 0.9410 | 0.9363 | -0.50% | [-1.00%, +0.00%] |
| bundle italy | 1X2:brier | 3625 | 0.6131 | 0.6120 | -0.18% | [-0.37%, +0.01%] |
| bundle italy | 1X2:log_loss | 3625 | 1.0221 | 1.0207 | -0.14% | [-0.31%, +0.03%] |
| bundle italy | 1X2:rps | 3625 | 0.2053 | 0.2046 | -0.34% | [-0.56%, -0.11%] |
| bundle italy | BTTS:brier | 3625 | 0.4983 | 0.4969 | -0.28% | [-0.82%, +0.26%] |
| bundle italy | BTTS:log_loss | 3625 | 0.6915 | 0.6901 | -0.21% | [-0.61%, +0.19%] |
| bundle italy | OU2.5:brier | 3625 | 0.4939 | 0.4936 | -0.06% | [-0.73%, +0.61%] |
| bundle italy | OU2.5:log_loss | 3625 | 0.6873 | 0.6868 | -0.07% | [-0.58%, +0.43%] |
| bundle italy | total:crps | 3625 | 0.8783 | 0.8769 | -0.15% | [-0.62%, +0.31%] |
| bundle spain | 1X2:brier | 4030 | 0.6185 | 0.6196 | +0.17% | [-0.01%, +0.35%] |
| bundle spain | 1X2:log_loss | 4030 | 1.0293 | 1.0309 | +0.16% | [-0.01%, +0.32%] |
| bundle spain | 1X2:rps | 4030 | 0.2059 | 0.2064 | +0.23% | [+0.01%, +0.45%] |
| bundle spain | BTTS:brier | 4030 | 0.4979 | 0.4972 | -0.13% | [-0.57%, +0.30%] |
| bundle spain | BTTS:log_loss | 4030 | 0.6912 | 0.6904 | -0.12% | [-0.44%, +0.21%] |
| bundle spain | OU2.5:brier | 4030 | 0.4780 | 0.4771 | -0.18% | [-0.73%, +0.37%] |
| bundle spain | OU2.5:log_loss | 4030 | 0.6712 | 0.6701 | -0.17% | [-0.60%, +0.25%] |
| bundle spain | total:crps | 4030 | 0.8468 | 0.8453 | -0.17% | [-0.55%, +0.20%] |
| cold_start all | 1X2:brier | 663 | 0.6675 | 0.6603 | -1.07% | [-1.78%, -0.36%] |
| cold_start all | 1X2:log_loss | 663 | 1.1006 | 1.0889 | -1.06% | [-1.73%, -0.39%] |
| cold_start all | 1X2:rps | 663 | 0.2359 | 0.2333 | -1.11% | [-1.98%, -0.25%] |
| cold_start all | BTTS:brier | 663 | 0.5079 | 0.4980 | -1.94% | [-3.98%, +0.09%] |
| cold_start all | BTTS:log_loss | 663 | 0.7019 | 0.6912 | -1.53% | [-3.08%, +0.01%] |
| cold_start all | OU2.5:brier | 663 | 0.4989 | 0.4888 | -2.04% | [-4.52%, +0.45%] |
| cold_start all | OU2.5:log_loss | 663 | 0.6949 | 0.6820 | -1.86% | [-3.86%, +0.14%] |
| cold_start all | total:crps | 663 | 0.8672 | 0.8454 | -2.52% | [-4.31%, -0.74%] |
| mover promoted | 1X2:brier | 2357 | 0.5800 | 0.5805 | +0.08% | [-0.22%, +0.38%] |
| mover promoted | 1X2:log_loss | 2357 | 0.9755 | 0.9762 | +0.07% | [-0.19%, +0.33%] |
| mover promoted | 1X2:rps | 2357 | 0.1972 | 0.1975 | +0.14% | [-0.24%, +0.51%] |
| mover promoted | BTTS:brier | 2357 | 0.4928 | 0.4937 | +0.20% | [-0.46%, +0.86%] |
| mover promoted | BTTS:log_loss | 2357 | 0.6859 | 0.6868 | +0.14% | [-0.35%, +0.63%] |
| mover promoted | OU2.5:brier | 2357 | 0.4848 | 0.4869 | +0.43% | [-0.40%, +1.26%] |
| mover promoted | OU2.5:log_loss | 2357 | 0.6777 | 0.6798 | +0.31% | [-0.31%, +0.93%] |
| mover promoted | total:crps | 2357 | 0.9068 | 0.9081 | +0.15% | [-0.42%, +0.71%] |
| mover relegated | 1X2:brier | 2513 | 0.6275 | 0.6274 | -0.01% | [-0.27%, +0.24%] |
| mover relegated | 1X2:log_loss | 2513 | 1.0428 | 1.0428 | +0.01% | [-0.22%, +0.23%] |
| mover relegated | 1X2:rps | 2513 | 0.2119 | 0.2119 | +0.02% | [-0.30%, +0.34%] |
| mover relegated | BTTS:brier | 2513 | 0.4963 | 0.4947 | -0.32% | [-0.96%, +0.32%] |
| mover relegated | BTTS:log_loss | 2513 | 0.6895 | 0.6879 | -0.24% | [-0.72%, +0.23%] |
| mover relegated | OU2.5:brier | 2513 | 0.4898 | 0.4861 | -0.75% | [-1.52%, +0.02%] |
| mover relegated | OU2.5:log_loss | 2513 | 0.6834 | 0.6792 | -0.61% | [-1.19%, -0.02%] |
| mover relegated | total:crps | 2513 | 0.8933 | 0.8856 | -0.86% | [-1.38%, -0.33%] |

## Promoted / relegated teams: actual − expected points per team-match

| status | matches | model | n | gap | 95% ± |
|---|---|---|---|---|---|
| other | 1-10 | dixon_coles | 8720 | -0.005 | 0.026 |
| other | 1-10 | phase1b_strength_open | 8720 | -0.011 | 0.026 |
| other | 11+ | dixon_coles | 23348 | -0.002 | 0.016 |
| other | 11+ | phase1b_strength_open | 23348 | -0.005 | 0.016 |
| promoted | 1-10 | dixon_coles | 670 | -0.167 | 0.087 |
| promoted | 1-10 | phase1b_strength_open | 670 | -0.161 | 0.088 |
| promoted | 11+ | dixon_coles | 1804 | -0.088 | 0.053 |
| promoted | 11+ | phase1b_strength_open | 1804 | -0.077 | 0.053 |
| relegated | 1-10 | dixon_coles | 690 | +0.046 | 0.092 |
| relegated | 1-10 | phase1b_strength_open | 690 | +0.044 | 0.092 |
| relegated | 11+ | dixon_coles | 1950 | +0.121 | 0.056 |
| relegated | 11+ | phase1b_strength_open | 1950 | +0.118 | 0.056 |

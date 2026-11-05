# adatta — working rules

Probabilistic football forecasting platform. Reference spec: `docs/adatta_architecture_v2.pdf` (Revision 2). If code and spec disagree, stop and raise it; do not silently diverge.

## Principles (non-negotiable)
1. **Odds are never model features.** Odds are stored for Edge/CLV evaluation only, downstream of the model.
2. **Temporal contract on every fact:** `event_time`, `published_at`, `observed_at`, `ingested_at`, `valid_from/to`, `superseded_at`. No training or evaluation row may use information not observable at its as-of time (no leakage).
3. **Every row carries `source_id`.** Adapters are the only code that knows a provider's shape (API-Football, Open-Meteo, Transfermarkt for values/contracts only).
4. **Keep raw payloads** (R2/object store, >= 1 year) so any normalised table can be rebuilt.
5. **Baseline first:** results-only Dixon-Coles is the benchmark every later layer must beat on the walk-forward harness.
6. **Full-time score matrix is primary;** halves are conditional binomial-thinning splits within each draw.
7. **Lineup offset enters Layer 6 exactly once** and is in the training likelihood.
8. **Degrade gracefully:** missing capability for a competition/season (see capability table) falls back to the simpler layer, never to imputed fiction.
9. **Schema is sport-agnostic** where cheap (team_level, is_national, parent_team_id, PlayerRegistration).
10. Minors' data: treat with GDPR/legal caution; no betting-facing output for youth levels without review.

## Conventions
- Python 3.12, `uv` for env/deps, Polars for dataframes, PyMC + ArviZ for inference, SQLAlchemy + Alembic for the DB, FastAPI for the API.
- Type hints everywhere; `ruff` + `mypy` + `pytest` must pass before commit.
- UTC timestamps only. Money/odds as decimal odds.
- Migrations are forward-only Alembic revisions; never edit an applied one.
- Seeds fixed and logged; every model run writes a reproducibility manifest (code SHA, data snapshot, seed, library versions).
- No secrets in the repo. `.env` is git-ignored; provide `.env.example`.

## Working rules
- Small, reviewable steps; one Phase 0 task at a time (see `docs/PHASE0_TASKS.md`).
- Before building a layer, state its acceptance test (metric, segment, gate) first.
- Evaluation: dev window vs locked holdout vs live period. Never tune on the holdout.
- Phase 1b gate: >= 1.0% relative log-loss gain on 1X2 and 0.5% RPS vs Phase 1a, paired 95% interval excludes zero, no segment worse than 1.0%.
- Inference gates: R-hat, ESS, divergences, PPC (incl. dispersion), prior sensitivity, identifiability.
- Respect API-Football quota (Pro: ~7,500 req/day, ~300/min — verify). Every adapter goes through the quota planner; upgrade trigger ~70% sustained use.
- Ask before: spending money, adding a paid service, destructive DB operations, anything touching minors' data.

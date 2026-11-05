# Phase 0 — foundations (ordered per spec §19.2)

Tick each item only when its acceptance check passes.

## 0. Decisions (see DECISIONS.md) — settle first
- [ ] D1–D4 resolved

## 1. Accounts & access
- [ ] API-Football key (Pro) stored in `.env`; one test call succeeds; plan limits recorded
- [ ] Postgres host created (per D1); connection string in `.env`
- [ ] Object store bucket for raw payloads (Cloudflare R2 or equivalent)
- [ ] GitHub repo + Actions enabled
- [ ] Open-Meteo reachable (no key needed)

## 2. Repo skeleton
- [ ] `uv init`, ruff/mypy/pytest configured, pre-commit
- [ ] Layout per D3; `.env.example`, `.gitignore`
- [ ] CI runs lint + tests on push

## 3. Minimal schema (Alembic)
- [ ] source, competition, season, team (team_level, is_national, parent_team_id), player, match
- [ ] match_result with temporal columns; raw_payload index table
- [ ] capability table: competition x season x source x capability, coverage_ratio
- [ ] Migration applies cleanly on empty DB; rollback tested

## 4. API-Football adapter + data audit
- [ ] Quota planner + rate limiter + retry/backoff
- [ ] Adapter: leagues, teams, fixtures, results, lineups, events, stats; raw payload saved, normalised rows with source_id
- [ ] Audit notebook/script: coverage by competition x season x capability; fill capability table
- [ ] Source-feasibility register: mark unavailable items (PPDA, directness, tracking data, event coordinates, injury return dates)

## 5. Snapshot capture — start running from day one
- [ ] Scheduler (per D2) captures fixtures, lineups/status, and odds snapshots (initial, T-7d, T-24h, T-60m, close)
- [ ] Odds stored in separate tables; never joined into feature builds
- [ ] Alert if a scheduled capture is missed

## 6. Historical backfill
- [ ] Prioritised competitions/seasons within quota budget
- [ ] Backfill resumable and idempotent; completeness report

## 7. Phase 1a baseline + harness
- [ ] Results-only Dixon-Coles (rho, time decay) fit
- [ ] Walk-forward harness: Brier, log loss, RPS, CRPS; segments incl. team_level and confederation
- [ ] Dev window / locked holdout defined and recorded
- [ ] Baseline report committed; reproducibility manifest written

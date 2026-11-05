# Phase 0 — foundations (ordered per spec §19.2)

Tick each item only when its acceptance check passes.

## 0. Decisions (see DECISIONS.md) — settle first
- [x] D1–D4 resolved (as built; see DECISIONS.md). D5 resolved; D6 open until task 6

## 1. Accounts & access
- [x] API-Football key (Pro) stored in `.env`; one test call succeeds; plan limits recorded (7,500/day; see SOURCE_FEASIBILITY.md)
- [x] Postgres host created (per D1); connection string in `.env` (Neon, psycopg3)
- [x] Object store bucket for raw payloads (Cloudflare R2 or equivalent) (R2 `adatta-raw`)
- [x] GitHub repo + Actions enabled (public, for free Actions minutes)
- [x] Open-Meteo reachable (no key needed) (archive API returned 200 on 2026-11-05; no adapter yet)

## 2. Repo skeleton
- [ ] `uv init`, ruff/mypy/pytest configured, pre-commit — done except pre-commit (not configured; CI enforces the gate)
- [x] Layout per D3; `.env.example`, `.gitignore`
- [x] CI runs lint + tests on push

## 3. Minimal schema (Alembic)
- [x] source, competition, season, team (team_level, is_national, parent_team_id), player, match
- [x] match_result with temporal columns; raw_payload index table
- [x] capability table: competition x season x source x capability, coverage_ratio (spec §3.4 shape, migration 0002)
- [x] Migration applies cleanly on empty DB; rollback tested (`tests/test_migrations.py`)

## 4. API-Football adapter + data audit
- [x] Quota planner + rate limiter + retry/backoff
- [ ] Adapter: leagues, teams, fixtures, results, lineups, events, stats; raw payload saved, normalised rows with source_id — done except events and stats: kept in raw payloads, not yet normalised
- [x] Audit notebook/script: coverage by competition x season x capability; fill capability table (`adatta audit`; 2026-11-05: 51 seasons, `reports/capability_audit/`)
- [x] Source-feasibility register: mark unavailable items (PPDA, directness, tracking data, event coordinates, injury return dates) (`docs/SOURCE_FEASIBILITY.md`; open items listed there)

## 5. Snapshot capture — start running from day one
- [x] Scheduler (per D2) captures fixtures, lineups/status, and odds snapshots (initial, T-7d, T-24h, T-60m, close) (GitHub Actions, every 15 min; first scheduled-workflow run green 2026-11-05)
- [x] Odds stored in separate tables; never joined into feature builds
- [x] Alert if a scheduled capture is missed

## 6. Historical backfill
- [ ] Prioritised competitions/seasons within quota budget
- [ ] Backfill resumable and idempotent; completeness report

## 7. Phase 1a baseline + harness
- [ ] Results-only Dixon-Coles (rho, time decay) fit
- [ ] Walk-forward harness: Brier, log loss, RPS, CRPS; segments incl. team_level and confederation
- [ ] Dev window / locked holdout defined and recorded
- [ ] Baseline report committed; reproducibility manifest written

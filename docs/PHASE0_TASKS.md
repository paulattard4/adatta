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
- [x] Prioritised competitions/seasons within quota budget (2026-11-05: 150 seasons, 2,648 requests in 45 min, inside the 70% backfill budget)
- [x] Backfill resumable and idempotent; completeness report (`adatta backfill-report`; `reports/backfill/20261105T150239Z.csv`)

Acceptance test (stated before building, per CLAUDE.md):
- Scope: every (competition, season) in `config/competitions.yml` (D6: 15 competitions, 2016–2025) has a backfill job that ends `done`, or `failed` with the recorded reason.
- Completeness report (`adatta backfill-report`, CSV under `reports/backfill/`), per competition-season:
  listed fixtures; finished; not played, by status; for leagues, regular-season matches vs
  expected `teams × (teams − 1)`, with any gap explained by listed non-played statuses;
  finished matches with a normal-time score; finished matches with stored detail (lineups,
  events, statistics, player stats). Anything short of 100% is flagged, never imputed.
- Idempotent: re-running a finished backfill makes 0 requests and adds 0 rows.
- Resumable: a run stopped mid-season (quota or crash) continues where it stopped without
  re-fetching stored detail batches.
- Quota: runs only inside the backfill budget (70% of daily quota; 30% reserved for live
  capture); requests recorded per job.
- Temporal contract: backfilled rows carry their true `observed_at` (ingest time); the match
  `kickoff_time` is the event time. Nothing is back-dated.

Result (2026-11-05): all 150 jobs done; 49,476 finished matches, all with stored detail
(lineups 44,240, player stats 39,560); every league regular season complete (Ligue 1 and
Ligue 2 2019/20 listed with their COVID cancellations); re-run = 0 requests. Flags left
as gaps, not filled: 90' score unknown for 3 league play-off matches and for cup AET/PEN
matches without an extra-time breakdown; Copa del Rey 2016–2017 not in API-Football.

## 7. Phase 1a baseline + harness
- [ ] Results-only Dixon-Coles (rho, time decay) fit
- [ ] Walk-forward harness: Brier, log loss, RPS, CRPS; segments incl. team_level and confederation
- [ ] Dev window / locked holdout defined and recorded
- [ ] Baseline report committed; reproducibility manifest written

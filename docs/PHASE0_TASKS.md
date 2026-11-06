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
- [x] `uv init`, ruff/mypy/pytest configured, pre-commit (`.pre-commit-config.yaml`: local hooks for ruff, mypy, pytest and a .env guard; `uv run pre-commit install`)
- [x] Layout per D3; `.env.example`, `.gitignore`
- [x] CI runs lint + tests on push

## 3. Minimal schema (Alembic)
- [x] source, competition, season, team (team_level, is_national, parent_team_id), player, match
- [x] match_result with temporal columns; raw_payload index table
- [x] capability table: competition x season x source x capability, coverage_ratio (spec §3.4 shape, migration 0002)
- [x] Migration applies cleanly on empty DB; rollback tested (`tests/test_migrations.py`)
- [x] Rest of the spec §3.5 Phase 0 subset (migration 0005): competition_bundle (+ members, synced from `config/evaluation.yml`), player_registration, override_parametric / override_structural (§8.5), model_run, projection_run, market_projection (§14.8). The baseline run is model_run 1

## 4. API-Football adapter + data audit
- [x] Quota planner + rate limiter + retry/backoff
- [x] Adapter: leagues, teams, fixtures, results, lineups, events, stats; raw payload saved, normalised rows with source_id. Events and stats are fetched and kept raw (R2, indexed by `match_detail`); normalising them is deferred to Phase 1b by D8, since spec §3.5 doesn't list those tables for Phase 0
- [x] Audit notebook/script: coverage by competition x season x capability; fill capability table (`adatta audit`; 2026-11-05: 51 seasons, `reports/capability_audit/`)
- [x] Source-feasibility register: mark unavailable items (PPDA, directness, tracking data, event coordinates, injury return dates) (`docs/SOURCE_FEASIBILITY.md`; open items listed there)

## 5. Snapshot capture — start running from day one
- [x] Scheduler (per D2) captures fixtures, lineups/status, and odds snapshots (initial, T-7d, T-24h, T-60m, close) (GitHub Actions, every 15 min; first scheduled-workflow run green 2026-11-05); 2026-11-06: scheduler fired far below 15 min, reliable trigger open under D17
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
- [x] Results-only Dixon-Coles (rho, time decay) fit (`adatta/model/dixon_coles.py`)
- [x] Walk-forward harness: Brier, log loss, RPS, CRPS; segments incl. team_level and confederation (`adatta/eval/harness.py`)
- [x] Dev window / locked holdout defined and recorded (`config/evaluation.yml`, D7)
- [x] Baseline report committed; reproducibility manifest written (`reports/baseline/20261105T162900Z/`)

Acceptance test (stated before building, per CLAUDE.md; windows in `config/evaluation.yml`):
- End to end (§19.4 gate): weekly walk-forward over the dev window (2019/20–2023/24) for all
  5 bundles; every finished regular-season match in a dev season gets a prediction, or is
  listed as unscorable with the reason.
- No leakage: each refit trains only on results with kickoff + 3h <= as-of and within the
  lookback; enforced by an assertion in the harness and a test that planting future results
  leaves every prediction unchanged.
- Holdout locked: the harness refuses to score holdout seasons (test).
- Model correctness: the Dixon-Coles fit recovers simulated parameters; the analytic
  gradient matches finite differences; each score matrix sums to 1 with truncated tail mass
  reported.
- Metrics: 1X2 Brier, log loss, RPS; OU 2.5 and BTTS Brier and log loss; total-goals CRPS;
  segmented by bundle, league, season, market, team_level and confederation; cold-start
  matches reported separately.
- Baselines on the same harness (§14.4): league base rates and a league-only Poisson. Dixon-
  Coles must beat both on 1X2 log loss in every league over the dev window; a league where
  it doesn't is flagged as a probable bug, not shipped.
- Half-life chosen from the grid on the dev window only, and recorded.
- Report and per-match predictions committed under `reports/baseline/`, with a
  reproducibility manifest (code SHA, data snapshot hash, seed, library versions, config).

Result (2026-11-05, dev window only; holdout not opened): 19,254 regular-season matches in 5
bundles, 0 unscorable. Tuned on dev: half-life 365 days, team prior sd 0.5. Warm matches:
Dixon-Coles 1X2 log loss 1.0261 vs base rate 1.0777 (paired diff -0.052, 95% [-0.056,
-0.047]); RPS 0.2099 vs 0.2277; beats both baselines on 1X2 log loss in all 10 leagues.
Totals markets are close to the league-only Poisson (OU 2.5 slightly better, BTTS level):
static team strengths add little there, which is Phase 1b's job. Cold start (572 matches)
is reported apart and stays slightly worse than the league baselines.

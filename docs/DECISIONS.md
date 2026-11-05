# Decisions to settle first

| # | Decision | Options | Leaning | Status |
|---|----------|---------|---------|--------|
| D1 | Database host | Neon Postgres (spec default) / local Postgres first / Supabase | Neon; local Postgres in Docker for dev | resolved: Neon (psycopg3); SQLite in tests |
| D2 | Orchestration | Cron / GitHub Actions schedule at first, Prefect later / Prefect from day one | Cron-style first (simple, snapshot capture only needs a scheduler); move to Prefect when DAGs appear | resolved: GitHub Actions cron (`capture.yml`, 15 min; repo public for free minutes) |
| D3 | Repo layout | Monorepo: `adatta/` (python pkg: adapters, db, model, eval, sim), `api/`, `web/`, `docs/`, `migrations/` / multi-repo | Monorepo | resolved: monorepo |
| D4 | Fresh repo or reuse earlier code | Fresh (folder was cleared; earlier repo facts in notes) | Fresh start, port ideas not code | resolved: fresh |
| D5 | Object store for raw payloads | Cloudflare R2 / local disk first | Local disk in dev, R2 in prod | resolved: R2 `adatta-raw`; `STORAGE_BACKEND=local` for offline dev |
| D6 | Initial competition scope for backfill | Top-5 leagues + 1–2 more / wider | Small, quota-friendly set first | resolved 2026-11-05: all 15 competitions in `config/competitions.yml` (10 leagues + 5 cups), seasons 2016/17–2025/26. Wider than the spec's one-bundle start (§19.1): loading is cheap (fixtures 1 call/season, detail 20 fixtures/call); modelling still starts with the PL + Championship bundle, and cups stay out of modelling until knockout logic (§9) exists |
| D7 | Evaluation windows and baseline scope | see `config/evaluation.yml` | — | resolved 2026-11-05: warm-up 2016/17–2018/19, dev 2019/20–2023/24, locked holdout 2024/25–2025/26, live from 2026/27. Phase 1a covers all 5 country bundles (top flight + second tier, fitted jointly); England is the headline |
| D8 | Normalise events and match statistics in Phase 0? | now (task list) / when first used (spec §3.5) | spec | resolved 2026-11-05: deferred to the start of Phase 1b, which is the first user (team stats incl. xG). Raw payloads for all backfilled matches are in R2 and indexed by `match_detail`, so nothing is lost. Phase 0 instead completes the §3.5 tables (migration 0005) |

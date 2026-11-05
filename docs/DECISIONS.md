# Decisions to settle first

| # | Decision | Options | Leaning | Status |
|---|----------|---------|---------|--------|
| D1 | Database host | Neon Postgres (spec default) / local Postgres first / Supabase | Neon; local Postgres in Docker for dev | resolved: Neon (psycopg3); SQLite in tests |
| D2 | Orchestration | Cron / GitHub Actions schedule at first, Prefect later / Prefect from day one | Cron-style first (simple, snapshot capture only needs a scheduler); move to Prefect when DAGs appear | resolved: GitHub Actions cron (`capture.yml`, 15 min; repo public for free minutes) |
| D3 | Repo layout | Monorepo: `adatta/` (python pkg: adapters, db, model, eval, sim), `api/`, `web/`, `docs/`, `migrations/` / multi-repo | Monorepo | resolved: monorepo |
| D4 | Fresh repo or reuse earlier code | Fresh (folder was cleared; earlier repo facts in notes) | Fresh start, port ideas not code | resolved: fresh |
| D5 | Object store for raw payloads | Cloudflare R2 / local disk first | Local disk in dev, R2 in prod | resolved: R2 `adatta-raw`; `STORAGE_BACKEND=local` for offline dev |
| D6 | Initial competition scope for backfill | Top-5 leagues + 1–2 more / wider | Small, quota-friendly set first | resolved 2026-11-05: all 15 competitions in `config/competitions.yml` (10 leagues + 5 cups), seasons 2016/17–2025/26. Wider than the spec's one-bundle start (§19.1): loading is cheap (fixtures 1 call/season, detail 20 fixtures/call); modelling still starts with the PL + Championship bundle, and cups stay out of modelling until knockout logic (§9) exists |

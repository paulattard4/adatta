# Decisions to settle first

| # | Decision | Options | Leaning | Status |
|---|----------|---------|---------|--------|
| D1 | Database host | Neon Postgres (spec default) / local Postgres first / Supabase | Neon; local Postgres in Docker for dev | resolved: Neon (psycopg3); SQLite in tests |
| D2 | Orchestration | Cron / GitHub Actions schedule at first, Prefect later / Prefect from day one | Cron-style first (simple, snapshot capture only needs a scheduler); move to Prefect when DAGs appear | resolved: GitHub Actions cron (`capture.yml`, 15 min; repo public for free minutes) |
| D3 | Repo layout | Monorepo: `adatta/` (python pkg: adapters, db, model, eval, sim), `api/`, `web/`, `docs/`, `migrations/` / multi-repo | Monorepo | resolved: monorepo |
| D4 | Fresh repo or reuse earlier code | Fresh (folder was cleared; earlier repo facts in notes) | Fresh start, port ideas not code | resolved: fresh |
| D5 | Object store for raw payloads | Cloudflare R2 / local disk first | Local disk in dev, R2 in prod | resolved: R2 `adatta-raw`; `STORAGE_BACKEND=local` for offline dev |
| D6 | Initial competition scope for backfill | Top-5 leagues + 1–2 more / wider | Small, quota-friendly set first | open |

# Decisions to settle first

| # | Decision | Options | Leaning | Status |
|---|----------|---------|---------|--------|
| D1 | Database host | Neon Postgres (spec default) / local Postgres first / Supabase | Neon; local Postgres in Docker for dev | open |
| D2 | Orchestration | Cron / GitHub Actions schedule at first, Prefect later / Prefect from day one | Cron-style first (simple, snapshot capture only needs a scheduler); move to Prefect when DAGs appear | open |
| D3 | Repo layout | Monorepo: `adatta/` (python pkg: adapters, db, model, eval, sim), `api/`, `web/`, `docs/`, `migrations/` / multi-repo | Monorepo | open |
| D4 | Fresh repo or reuse earlier code | Fresh (folder was cleared; earlier repo facts in notes) | Fresh start, port ideas not code | open (you said recreate the folder: assuming fresh) |
| D5 | Object store for raw payloads | Cloudflare R2 / local disk first | Local disk in dev, R2 in prod | open |
| D6 | Initial competition scope for backfill | Top-5 leagues + 1–2 more / wider | Small, quota-friendly set first | open |

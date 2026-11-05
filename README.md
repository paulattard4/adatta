# adatta

Probabilistic football forecasting platform. Spec: `docs/adatta_architecture_v2.pdf`.
Working rules: `CLAUDE.md`. Tasks: `docs/PHASE0_TASKS.md`. Decisions: `docs/DECISIONS.md`.

## First-time setup
```bash
cd ~/Documents/adatta
git init -b main
uv sync                      # install deps (install uv first: https://docs.astral.sh/uv/)
cp .env.example .env         # then fill in the values (never commit .env)
uv run pytest                # should pass
git add -A && git commit -m "Initial skeleton"
```
Then create an empty GitHub repo and: `git remote add origin <url> && git push -u origin main`.

## Database
```bash
uv run alembic upgrade head   # applies migrations to DATABASE_URL (Neon)
```
Tip: create a Neon branch and point DATABASE_URL at it to test migrations safely.

## Commands
```bash
uv run adatta status            # API-Football connectivity (1 request)
uv run adatta backfill-enqueue  # queue jobs from config/competitions.yml
uv run adatta backfill-run --limit 5
```
Verify each `league_id` in `config/competitions.yml` against API-Football `/leagues` first.

## Quality gate
`uv run ruff check . && uv run ruff format --check . && uv run mypy adatta && uv run pytest`

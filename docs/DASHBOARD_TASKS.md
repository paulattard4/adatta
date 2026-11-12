# Dashboard, wave 1: fixture browser and match projection view

Spec §16.2 (fixture and competition browser) and §16.3 (match projection dashboard), with
the stack of §17.1: a read-only FastAPI service over Postgres and the stored refit draws, and
a Next.js frontend. Owner decisions 2026-11-11: it runs **locally only** for now (no hosting
cost, no sign-in; deploying behind Supabase or Clerk auth is a later step, priced first), and
the frontend is **Next.js per the spec** (Node 22 LTS, the official nodejs.org build).

## What the first version can show, and what it says is not there yet

Shown from data that exists today: the champion's milestone projections (1X2, OU 1.5/2.5/3.5,
BTTS, each with its 90% interval), the correct-score matrix and the log-rate decomposition from
the same stored refit, kickoff, venue, latest weather forecast, referee, competition capability
flags, refit freshness and inference diagnostics.

Not yet available, shown as an explicit "not available" panel with the reason, never filled
with placeholders (principle 8): squad and lineup panel and player projections (lineup layer,
Phase 3), half-split toggle (§8.2, deferred), knockout panel (regular season only), edge strip
(§16.4 edge analytics, wave 2), override entry point (§16.4 override interface, built with the
§8.5 override mechanisms), a derived headline confidence label and the confidence-tier filter
(§13.2 requires the combining formula to be backtested first), the "has edge" filter.

## Acceptance test (stated 2026-11-11, before building)

1. **Coherent with what was published (§11.2).** For every match, the API's correct-score
   matrix comes from the refit named in the latest milestone's `projection_run`; the 1X2,
   OU and BTTS probabilities read off that matrix's draws equal the stored `market_projection`
   rows to 1e-9; the matrix is renormalised (sums to one) and the mass clipped by the
   truncation is reported. A recomputation that does not reproduce the published rows is
   not shown. Unit-tested on a synthetic world.
2. **Decomposition is exact (§13.1).** The waterfall terms (league baseline, home advantage
   (league + team), attack incl. form, minus opponent defence) reproduce `rates_from_draws`
   per draw to 1e-9, and their posterior means sum to the posterior mean of log lambda.
   Unit-tested, including an unseen (cold-start) team, which is labelled as such.
3. **Uncertainty is never dropped (§16.1).** Every probability the API returns carries its
   interval (schema-enforced), and every view that renders a probability renders its interval.
4. **Confidence as named components (§13.2).** Each fixture carries the six components
   (historical data strength, source capability, lineup certainty, cross-league prior
   dependence, model stability, data freshness), each with its value and a plain status;
   components the model does not have yet say so. No collapsed score.
5. **Browser (§16.2).** Fixtures grouped by competition; each competition header shows its
   capability badge and refit freshness (tier, refit as_of, age); each fixture shows teams,
   kickoff in the viewer's local time, the 1X2 headline with intervals and the confidence
   components; filters by competition, date range and team level.
6. **Match view (§16.3), in the spec's order:** header strip, confidence components, headline
   markets with intervals and the milestone history, correct-score heatmap, decomposition
   waterfall, then the not-available panels. Numbers in monospace, dark instrument-panel
   styling.
7. **Read-only, local, no odds.** The API opens read-only transactions, binds to 127.0.0.1,
   and never reads odds tables (test). Tests use synthetic data only; no match data in git.
8. **Fast enough on live data.** Against Neon with the current 128 fixtures: fixture list
   under 1 s, match view under 2 s once the refit's draws are cached in the process.
9. **Checks pass:** ruff, mypy, pytest; `npm run lint`, `tsc --noEmit` and `npm run build`.

## Tasks
- [x] `adatta/api/`: read-only FastAPI app (`/api/fixtures`, `/api/matches/{id}`), draws
      cache per model_run, decomposition, confidence components;
      `tests/test_dashboard_api.py` (8 tests: items 1–4, 7).
- [x] `web/`: Next.js 15 app, browser and match views (items 3, 5, 6); `npm run lint`,
      `tsc --noEmit` and `npm run build` pass.
- [x] Live check against Neon (item 8) and a walk through both views in the browser.
      **Pass, 2026-11-12.** All 132 projected fixtures (10 competitions): every match view
      recomputes the published initial milestone, max difference 2.2e-16. Fixture list
      0.67 s warm (4.4 s on a cold process, loading five refits from R2); match view median
      0.69 s, max 1.03 s. Both views checked at desktop and phone width (no sideways scroll).

## Running it (local only)

```bash
uv run python -m adatta.api                 # API on 127.0.0.1:8000 (reads .env)
```

```bash
PATH="$HOME/.local/node22/bin:$PATH" npm --prefix web run dev   # UI on 127.0.0.1:3000
```

Node 22 LTS lives in `~/.local/node22` (the official nodejs.org build, checksum verified):
Homebrew has no bottle for this macOS and was compiling OpenSSL and Node from source.

## Notes from the build
- **Capability audit misses the current season.** No `competition_source_capability` rows
  exist for any 2026/27 bundle season (2024 and 2025 have 160 each), so every fixture shows
  "capabilities not audited" and the source-capability component reads "xG unknown". The
  dashboard is reporting the gap correctly; the fix is to run the capability audit for the
  current seasons (API quota, so not run here).
- `npm audit`: the PostCSS advisory inside Next 15 is closed with an override to PostCSS
  8.5.32; six moderate advisories remain in the ESLint toolchain (minimatch ReDoS), dev-only
  and fixed only by major upgrades.
- The decomposition leaves out the form term because the champion has none (no zero rows).
- Not in CI yet: the web checks run locally; adding a Node job to `ci.yml` is a small follow-up.

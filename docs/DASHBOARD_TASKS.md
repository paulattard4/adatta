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
- **Capability audit for the current season: run 2026-11-12.** No `competition_source_capability`
  rows existed for any 2026/27 bundle season, so every fixture showed "capabilities not
  audited" and "xG unknown". `adatta audit --league L --season 2026` for the 10 leagues (40
  sampled finished fixtures each, seed 20261105; 40 requests, details already in the raw store),
  reports `reports/capability_audit/20261112T0546*`–`T0548*`. Same pattern as 2025/26: xG
  available in 8 leagues, partial in Ligue 2 (22.5%) and Segunda (87.5%); the four player-prop
  capabilities stay unknown, as in every season. **Effect on the model:** `harness.load_xg`
  admits xG only where `has_xg` is available or partial, so the refits up to 2026-11-09 fitted
  2026/27 matches on goals only (graceful fallback, principle 8). The next scheduled weekly
  refit (Monday 2026-11-16) includes 2026/27 xG. Re-audit each new season before its first refit.
- `npm audit`: the PostCSS advisory inside Next 15 is closed with an override to PostCSS
  8.5.32; six moderate advisories remain in the ESLint toolchain (minimatch ReDoS), dev-only
  and fixed only by major upgrades.
- The decomposition leaves out the form term because the champion has none (no zero rows).
- CI: `ci.yml` has a `web` job (Node 22: `npm ci`, `npm audit --audit-level=high`, lint, build,
  typecheck), added 2026-11-12.

## Played view: forecasts against results (requested 2026-11-12)

Live forecasting began 2026-11-11, so the first forecast matches are played from
2026-11-21. The view shows only forecasts actually published before kickoff: no
reconstruction (the owner chose to wait for real results over a retrospective backtest).

Acceptance test (stated 2026-11-12, before building):
1. **Only real pre-kickoff forecasts.** A match appears when it is finished with a 90'
   result and has a projection whose run was generated before the match's kickoff; the
   forecast shown is the latest such milestone (initial, T-7d, T-24h, T-60m, close). A
   projection generated at or after kickoff is never used. Unit-tested, including a
   post-kickoff run that must be ignored.
2. **The stored numbers.** Probabilities and intervals are the `market_projection` rows of
   that milestone, unchanged (1X2, OU 2.5, BTTS).
3. **Scored like the backtest.** Per match: 1X2 log loss, RPS and Brier, OU 2.5 and BTTS
   log loss, computed with `adatta.eval.metrics` (the backtest's functions); tested against
   hand-computed values.
4. **Summary with its uncertainty.** Overall and per competition: the number of matches and
   each mean score with a 95% interval, and a note that a few dozen matches say little.
   A uniform 1X2 forecast (log loss ln 3) is shown as the only fixed yardstick: no odds
   (principle 1; market comparison is the wave 2 edge view).
5. **Filters and links.** Competition and date range; each row links to its match view.
6. **Empty state** says forecasts began 2026-11-11 and when the first results are due.
7. Read-only and no odds (the existing AST test covers the new code); ruff, mypy, pytest,
   lint, typecheck and build pass.

- [x] Played view built 2026-11-12: `GET /api/played` (`views.played`), page `/played`
      (nav: Fixtures · Played). **Pass on synthetic data** (`tests/test_played_view.py`,
      4 tests: items 1–3 and the endpoint, incl. a post-kickoff run with a better-looking
      forecast that must be ignored, and played-but-never-forecast matches left out);
      checked in the browser on a six-match synthetic world at desktop and phone width (no
      sideways scroll). Live it shows the empty state (forecasting began 2026-11-11,
      0 forecast matches played). **To confirm after 21 November:** the first real rows
      match their match views and the stored projections.

## Fair odds (requested 2026-11-14)

The model's own price beside each probability: decimal odds of 1/p, with no margin. Derived
from the published probabilities in the browser; no bookmaker odds are read (principle 1;
market comparison stays with the wave 2 edge view).

Acceptance test (stated 2026-11-14, before building):
1. **Exact transform of what is shown.** Fair odds are 1/p of the posterior-mean probability,
   and their 90% interval is 1/hi to 1/lo of the probability's interval (1/p is monotone, so
   it maps the interval exactly). Decimal, two places; a bound with p below 0.001 shows as
   ">1000". Not the posterior mean of 1/p, which is larger and not the break-even price.
2. **Where.** Every probability cell in the fixture browser (1X2 headline) and the match view
   markets (1X2, OU, BTTS). The milestone history and correct-score heatmap stay as
   probabilities.
3. **Senior only (principle 10).** Shown only when the fixture's team level is `senior`; the
   match header carries `team_level` for this. Other levels show probabilities only until
   reviewed.
4. **Read-only and no odds** (the AST test still passes); ruff, mypy, pytest, lint,
   typecheck and build pass; checked in the browser at desktop and phone width.

- [x] Built 2026-11-14: `fairOdds`/`oddsInterval`/`showsOdds` in `web/lib/format.ts`,
      `ProbCell odds`, `Header.team_level` (asserted in `test_dashboard_api.py`). Checked live:
      fixture list and match view (1X2, OU 1.5/2.5/3.5, BTTS) at desktop and phone width, no
      sideways scroll; 68.7% shows @1.46. ruff, mypy, API tests, lint and typecheck pass;
      `npm run build` not run while the dev server was up (CI runs it). Only senior fixtures
      exist today, so the youth path (no odds) is untested in the browser.

## Competition tier beside league names (requested 2026-11-14)

Every league name in the dashboard carries its tier from `competition.tier`
(config/competitions.yml): "tier 1", "tier 2", "cup" or "national teams" (`tierLabel` in
`web/lib/format.ts`). The API adds `tier` to `CompetitionGroup` and `ScoreSummary`, and
`competition_tier` to the match `Header` and `PlayedMatch` (asserted in
`test_dashboard_api.py` and `test_played_view.py`). Shown as a small chip in the fixture
headers, the match header and the played view, and in brackets in the competition filters.
Checked live on all three pages.
Competition lists (fixture sections, both filters, the played summary) are ordered by
country, then tier 1 before tier 2 (then cups, then national teams), by
`views.competition_order` (tested).

## In play: live matches with their pre-match projection (requested 2026-11-14)

Owner decision 2026-11-14: option 1 only. In-play **recalculation** stays out of scope (spec
§1.1, §12.2, §20.1); this shows the live score beside the last projection published before
kickoff, labelled as such. No new model output.

Design: `ApiFootballClient.live_fixtures` reads `/fixtures?live=<bundle league ids>` (one
request covers every live match) through the quota planner (live budget). It is a display
feed only: nothing is normalised from it, so no raw payload is kept and the dashboard API
stays read-only (principle 4 covers payloads that build tables). `adatta/api/live.py`
caches it for 60 s, calls the provider only when a bundle match kicked off in the last
4 hours or kicks off within 5 minutes, and stops at 500 dashboard requests per UTC day
(under 7% of the 7,500 quota). Pre-match fair odds are hidden for live matches: a stale
pre-kickoff price must not look like an in-play price.

Acceptance test (stated 2026-11-14, before building):
1. **Pre-match only.** A live match shows the latest milestone generated before kickoff
   (the played view's rule and function), never a run generated at or after kickoff; the
   label says it is the pre-match projection and not updated in play.
2. **Live state from the feed.** Score, status (1H, HT, 2H, ET, P, ...) and minute come from
   the provider's live response, matched on the fixture id; matches not in the feed are not
   shown as live. Parsing lives in the adapter (principle 3). Unit-tested on a synthetic
   payload.
3. **Quota-bounded.** At most one provider request per 60 s however many viewers poll; no
   request when no bundle match is in the live window; none after the daily cap; every
   request goes through `QuotaPlanner.acquire(live=True)`. Unit-tested with a fake clock.
4. **Degrades, never invents.** A failed or capped feed returns the last good state with
   its time and a note, or an empty list with the reason; the page says so.
5. **Shown** in an "In play" section at the top of the fixture browser (refreshing every
   60 s) and in the match header; no fair odds while live. Checked in the browser.
6. Read-only and no odds (existing tests); ruff, mypy, pytest, lint and typecheck pass.

- [x] Built 2026-11-14: `ApiFootballClient.live_fixtures` + `parse_live`, `adatta/api/live.py`
      (`LiveFeed`), `views.live`, `GET /api/live`, `web/components/Live.tsx`, match header
      live state. **Pass:** `tests/test_live_view.py` (7 tests: items 1-4 and the endpoint);
      full suite, ruff, mypy, lint and typecheck pass. Checked live on 2026-11-14 against a
      real Segunda Division match (Valladolid v Castellón, HT 0-0) on the fixture browser and
      the match view, desktop and phone width (no sideways scroll); fair odds hidden while live.
      Found while checking: the provider rejects `live=<one id>` (only `all` or `id-id...`),
      reporting it in a 200 body, so the feed asks for `all` and filters to the bundle
      leagues, and a body `errors` field now raises (`ProviderError`) instead of reading as
      "nothing in play".

### Follow-up: provider errors on the main fetch path
The same 200-with-`errors` body on `ApiFootballClient.fetch` (capture, backfill, audit) would
read as an empty answer: detail backfill would record every fixture in the batch as "not
returned", a fake gap.
- **Acceptance:** (1) a body with a non-empty `errors` field raises `ProviderError` from
  `fetch`, stores no raw payload and no index row, and the quota still syncs from the header;
  (2) a request-limit error (`requests` / `rateLimit` keys) raises `ProviderQuotaExceeded`, a
  `QuotaExceeded`, so backfill stops as on its own budget and capture reports it as quota;
  (3) in backfill, a detail batch answered with an error records no `match_detail` rows and
  the season job is `failed` with the provider's message, retried next run; (4) an empty
  `errors` (`[]` or `{}`) is a normal answer.
- **Built, passes (1–4):** `check_errors` in the adapter, shared by `fetch` and the live feed;
  the message names the endpoint and parameter names only (no ids, since capture errors reach
  CI logs and email). Tests: `test_backfill.py` (error body is not a gap; request limit stops
  like the quota), `test_live_view.py` (fetch raises, keeps nothing, syncs quota).

## Played matches: side markets and the result on the match page (requested 2026-11-14)
Owner: "show props for played matches too". The Played rows show 1X2 only, although the
API already carries the pre-kickoff Over/Under 2.5 and BTTS projections; a finished match's
page still shows a countdown and no result.

Design: on each Played row, one cell per side market showing the selection that came in
(Over or Under 2.5; BTTS Yes or No) with the probability the forecast gave it and its 90%
interval, and that market's log loss in the tooltip. The match page header shows the
90-minute score of a finished match in place of the countdown, and the markets mark the
selections that came in. Scores are 90-minute (the model's target), from the current
`match_result` row; nothing is recalculated.

Acceptance (stated 2026-11-14, before building):
1. API: `/api/matches/{id}` carries `result` (home and away 90-minute goals) for a finished
   match with a current result, and `null` otherwise (scheduled, in play, or a finished
   match without a result row); a superseded result is never used.
2. Played rows show the side-market cells: under 2.5 for a 0–1, BTTS Yes for a 2–2, the
   probability equal to the API's (1 − p for Under/No, interval flipped); a match whose
   forecast lacks the market shows "—".
3. A finished match's page shows "FT h–a" instead of the countdown, and exactly one
   selection per market is marked as came in, matching the score.
4. Phone width (375 px): no sideways scroll on the Played page or the match page.
- **Built, passes (1–4):** `Header.result` from the current `match_result` row of a
  finished match (tests in `test_played_view.py`: shown, corrected result replaces it, none
  while in play, before kickoff or without a row). Played rows add "Goals 2.5" and "Both
  score" cells; checked on the three Segunda matches of 13–14 Nov against the API (Under 2.5
  = 1 − P(over), BTTS No = 1 − P(yes)). Match 3960 (0–1) shows "FT 0–1" and exactly one
  selection per market outlined (Away, Under 1.5/2.5/3.5, BTTS No). 375 px: no sideways
  scroll on either page.

### Follow-up: fair odds on played matches (requested 2026-11-14)
The owner meant fair odds for played matches (the side markets stay).
- **Acceptance:** (1) `/api/played` rows carry `team_level`; (2) on a senior match every
  probability cell of a Played row (Home, Draw, Away, Goals 2.5, Both score) shows fair
  decimal odds 1/p of the probability shown (for Under/No that is 1/(1 − p)), with the
  interval in the tooltip, as on the fixtures page; (3) youth matches show none
  (principle 10); (4) 375 px: no sideways scroll.
- **Built, passes (1–4):** `PlayedMatch.team_level`; every cell passes `showsOdds(team_level)`,
  the same senior-only rule as the fixtures and match pages. On the three Segunda matches
  all 15 prices equal 1/p from the API (e.g. Valladolid–Castellón 3.80 / 3.91 / 2.08, Under
  2.5 2.08, BTTS No 2.18). 375 px: no sideways scroll.

## Fair odds everywhere a probability is shown; spacing pass (requested 2026-11-14)
Owner: show fair odds wherever the dashboard shows a probability, and make spacing and
borders consistent. Audit before building: odds were missing from the In play rows, the
match page markets while live (hidden on purpose earlier, now the owner's call: every price
there is labelled pre-match), the milestone history, the correct-score heatmap and the
Played row's "p" figure. Spacing: the probability bar's track was the same colour as the
market boxes (invisible), the match page's came-in outline sat outside its cell, last rows
kept a bottom border in some tables and not others, and list rows used different padding.

Acceptance (stated 2026-11-14, before building):
1. Every probability shown for a senior match has its fair odds 1/p beside it or one toggle
   away: fixtures rows, In play rows (`/api/live` rows carry `team_level`), match page markets
   (also while live, labelled pre-match), milestone history, correct-score heatmap (a
   % / odds switch; "Any side above" line too), Played rows including the "p" figure.
2. Youth: no odds anywhere (one rule, `showsOdds`).
3. Spacing: list rows (fixtures, in play, played) share padding and gaps, and their headers
   line up with the cell content; no table or list ends on a bottom border inside its panel;
   came-in and hit marks are drawn inside their cell without shifting it; the probability
   bar track is visible on both panel backgrounds.
4. 375 px: no sideways scroll on any page (the heatmap and score tables scroll inside their
   own box, as before).
- **Built, passes (1–4):** odds added to In play rows (`LiveMatch.team_level`, test in
  `test_live_view.py`), match page markets while live (heading says pre-match), milestone
  history, the heatmap (% / Fair odds switch; tooltips carry both; "Any side above" line) and
  the Played "p" figure; all through `showsOdds`. Spacing: one row padding, column gap and
  marked-cell padding (CSS variables); headers line up with cell content; no trailing border
  in any list or table; came-in / hit marks are borders on every cell (transparent when
  unmarked), so nothing shifts; bar track visible on both backgrounds; three-way markets span
  two columns so their odds ranges fit; Played cells all labelled so values line up. Widths:
  rows that need more room than the window now stack (fixtures and in play below 960 px,
  played below 1040 px) — before this, 761–960 px windows scrolled sideways on the fixtures
  and match pages. Checked at 375, 800, 960, 1024 and 1280 px on the fixtures, played and
  three match pages (scheduled, live, finished): page width equals window width and no cell
  overflows.

## Squad and availability panel (requested 2026-11-20)

Owner: "Squad panel first". The match page's "Squad and lineup" entry is still in the
not-available list, although the capture has been storing the injury feed daily since
2026-11-05 (`player_status_snapshot`) and announced lineups from 75 minutes before kickoff
(`lineup_snapshot`). This shows that captured data as it stood, with when it was seen. No
model output: P3-b's selection model and P3-e's scorer forecasts are not served (P3-e (f)).

Acceptance (stated 2026-11-20, before building):
1. **Availability as of the match.** For each side, the status spells of this match valid at
   t = min(now, kickoff): observed by t, not superseded by t, and not closed by t. Shown with
   the player's name, "Out" (missing) or "Doubtful" (questionable), the reason, and since
   when (the spell's start). A spell first seen after kickoff never shows; a spell the feed
   cleared before t never shows. Unit-tested on a synthetic world, including both.
2. **When the feed was checked.** The time of the last successful status capture of the
   competition at or before t. Without one the panel says availability isn't captured for
   this competition, so an empty list never reads as "no absences".
3. **Lineups as announced.** Each side's `lineup_snapshot` observed by now: formation, coach,
   starters and bench (number, name, position), with when it was first seen. Before one
   exists: "not announced yet" with the polling start (75 minutes before kickoff) for a match
   ahead, "not captured" for a match that has started.
4. **Senior only (principle 10).** For any other team level the panel lists no players and
   says why.
5. The "Squad and lineup" entry leaves the not-available list. "Player projections" stays,
   with its reason updated: P3-d failed (D40) and P3-e's scorer forecasts are not shown before
   their live verdict.
6. Read-only and no odds (the AST test still passes); ruff, mypy, pytest, lint and typecheck
   pass; checked in the browser on a match with a lineup and one with absences, at desktop and
   phone width (no sideways scroll).
- [x] **Built 2026-11-20, passes (1–6):** `views.squad` and `MatchView.squad` (schemas
      `Squad`, `TeamSquad`, `Lineup`, `Absence`, `SquadPlayer`), `web/components/Squad.tsx`,
      a "Squad and availability" panel after the decomposition. `tests/test_squad_view.py`
      (6 tests): spells as of min(now, kickoff), including one cleared before and one first
      seen after kickoff; no capture is not "no absences"; lineups not shown before they were
      seen; a started match without one says not captured; youth levels list no players; the
      not-available list. `test_dashboard_api.py` now asserts the panel in place of the old
      entry. Checked live: Bayern München v 1. FC Köln (5 absences a side, feed checked
      03:37 UTC, lineup not announced yet) and Albacete v Celta de Vigo II on 16 Nov (both
      lineups, first seen 23 minutes before kickoff), at desktop and phone width: no sideways
      scroll, the two sides stack on a phone.

## Forecast freshness (requested 2026-11-20)

Owner: "data freshness is over 1 week now". The match pages for the 21-22 Nov fixtures
showed "refit 11.3 d old, stale": their latest forecast (T-7d, written 15 Nov) came from the
9 Nov refit, while the 16 Nov refit had passed a day later. Forecasts are only written at the
milestones, so they kept the old refit until T-24h. The fixture list meanwhile said "4.3 d
old" for the same forecasts: it reported the newest refit, not the one behind the
probabilities on the row. Owner chose both fixes ("proceed"), the second as D41.

Acceptance, display (stated 2026-11-20, before building):
1. **The age shown is the age of what is shown.** A fixture row's data freshness component
   reports the refit behind the forecast on that row (the latest milestone's projection run),
   as the match page already does. A fixture with no forecast yet reports the serving refit.
   The competition header keeps the serving refit (that competition's current refit).
2. **A newer refit is named.** When the bundle's serving refit is newer than the one behind
   the forecast, the component says so and gives its date, on the row and the match page.
3. Tested on a synthetic world: a forecast from an older refit with a newer one serving.

Acceptance, refit milestone (D41, stated 2026-11-20, before building):
1. **When.** In a capture tick, a fixture gets a refit milestone when it already has a
   forecast, no other milestone is due for it in that tick, and its bundle's latest passing
   refit (as_of at or before now) is newer than every refit its forecasts came from.
2. **What.** The same projection as any milestone (same function, same markets and
   intervals), labelled `refit MM-DD HHh` from that refit's as_of, written once per fixture
   and refit (idempotent like the others). Timed milestones and their windows are unchanged.
3. **Order.** Milestones are ordered by when they were published, everywhere: the fixture
   headline, the match page's latest milestone and history, the in-play and played views
   (the latest published before kickoff). A refit milestone between T-7d and T-24h sorts
   between them.
4. **No leakage.** A refit with as_of after now is never used (as now); a refit milestone
   written at or after kickoff is never shown or scored (the existing played-view rule).
5. Tested on a synthetic world: T-7d from refit A, then refit B passes: the next tick writes
   one refit milestone from B, the tick after writes nothing; when T-24h opens it uses B and
   no refit milestone is added; the headline, match page and played view pick the latest
   published.
6. Read-only API and no odds (existing tests); ruff, mypy, pytest, lint, typecheck pass;
   checked live on a 21-22 Nov fixture.
- [x] **Built 2026-11-20, passes (display 1-3; refit milestone 1-5; 6 once pushed):**
      `serve.refit_label`, `_refits_used` and the D41 rule in `run_projection`;
      `views._published_order` (fixture headline, match page, in-play and played views order
      by publication), `_run_behind` (a row's freshness from the refit behind its forecast),
      and the "newer refit" detail. `tests/test_refit_milestone.py` (4 tests): one refit
      milestone per newer refit, none for a fixture already projected from it or when T-24h
      opens; none from a failed or future refit; match page, headline and played view take
      the latest published; row and page freshness name the newer refit until the tick, then
      not. Checked on live data before the push: of 223 fixtures on the list, 168 show
      forecasts 10-11 days old with the 16 Nov refit named (the list said 4.3 days for all of
      them before); match 108 page and row agree. The first capture tick after the push writes
      the refit milestones.

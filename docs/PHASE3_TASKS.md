# Phase 3 — squad and player (spec §7, §8.1, §19.3)

Goal: let the team model see who plays. Per-player availability, selection and minutes
produce a lineup offset Δ_lineup that enters log λ exactly once (Layer 6, §8.1; CLAUDE.md
rule 7), fitted inside the training likelihood (§7.5, adopted design) so the strength walk
tracks a team's baseline with a typical lineup instead of absorbing injuries as strength
changes. The model it builds on is the production `phase1b_strength_open_xg`; Phase 2
closed on 2026-11-16 without a promotion (D37).

Status (2026-11-16): **stated, not started.** Order per §19.3, each step validated before
the next: (a) status feed and squad lists; (b) availability and selection, validated
standalone; (c) formation and slot-code roles, Manager entity; (d) lineup offset in the
likelihood vs serving-only; (e) the feedback loop wired into Layer 6. Phase 3b (goals
decomposition, discipline, half split) and 3d (knockouts, now Stage B) follow later.

Tick each item only when its acceptance check passes.

## Data position (measured 2026-11-16, dev seasons, no requests)

| What | Where | Coverage | As-of |
|---|---|---|---|
| Lineups: starting XI, bench, formation, grid, position code, coach | stored `/fixtures?ids` payloads (R2, `match_detail.flags.has_lineups`) | tier 1-2 dev league matches 2016-2023: 99.4-100% per league | post-match fact for history; live, first seen a median 22.7 min before kickoff (register 3c.1) |
| Player match stats: minutes, position, rating, goals, assists, shots, key passes, tackles, cards | same payloads, `players[]` (`has_player_match_stats`) | five top flights 100%; Championship 99.8%, Segunda 99.2%, 2. Bundesliga 95.9%, Ligue 2 95.7%; **Serie B 60.2%, only from 2019** | post-match fact |
| Substitution events with minute | same payloads (`has_substitution_timestamps`) | as lineups | post-match fact |
| Pre-match status (injured, suspended, doubtful) | `player_status_snapshot`, daily capture | **from 2026-11-05 only**: 632 rows on 294 matches; empty during the international break (the feed lists absences only for fixtures ahead) | as-of, cannot be reconstructed (§7.1) |
| Pre-match lineups | `lineup_snapshot`, capture from T-75 min | from 2026-11-07: 198 matches | as-of |
| Squad lists, registrations | `/players/squads` (not captured); `player_registration` | **0 rows** | would be live snapshots only |
| Players | `player` | 146 rows (from status capture only); payload players not normalised yet | n/a |
| Market values (cold-start prior, §7.4) | Transfermarkt | not loaded; no official API, terms not reviewed (register B.5) | current value only, past values not as-of |

Consequences:
- The **lineups-known horizon** (the confirmed XI, about 20-60 min before kickoff) can be
  backtested on every dev season from stored payloads, with no status history needed.
  The stored XI is the final one, so a late change after release is missed (rare; the live
  period measures it).
- **Earlier horizons** (initial, T-7d, T-24h) need the availability and selection model.
  Selection can be learned from realised appearances in dev, but its status inputs exist
  only from 2026-11-05, so a status-informed model can only be judged live (D39).
- The **cold-start prior** from market values is blocked on a Transfermarkt licensing
  review (owner, CLAUDE.md: ask before a new source). Until then, new and sparse players
  get the position x league pooled rate (§7.4 pooling) and are flagged in the confidence
  profile.
- **Minors.** Senior squads include under-18 players. Their appearances load like everyone
  else's (owner, 2026-11-15: the project is private and personal, so loads are not
  filtered). Per-player outputs for them stay internal: none is betting-facing without the
  owner's review (CLAUDE.md rule 10).

## Spec points raised (CLAUDE.md: raise, don't silently diverge)

1. **§7.5 and §8: "must show on the locked holdout"** that the in-likelihood offset beats
   serving-only. The 2024/25-2025/26 holdout has been opened twice already (1b, Phase 2).
   Proposed: the live 2026/27 period judges the promotion (D38, open).
2. **§7.1 status snapshots** exist only from 2026-11-05, so availability has no dev
   history. Proposed: dev backtests at the lineups-known horizon, and the availability
   model judged live; optionally the provider's past `/injuries` lists as realised-absence
   labels only (D39, open).
3. **§19.3 Phase 1b gate "every Phase 2/3c covariate has a verified entry"**: the register
   covers 3c (formation, slots, manager) but not 3a, 3b or the Layer 5 rates. P3-0 adds them.

## Decisions

- **D37 (resolved 2026-11-16):** Phase 3 next; Stage A finishes A1 and A2; Stages B-D wait.
- **D38 (open):** promotion window for step (d). Leaning: live 2026/27.
- **D39 (open):** pre-match availability before live capture. Leaning: both, the
  `/injuries` probe priced in requests first.

## Tasks

- [x] **P3-0 Player data from stored payloads (no requests).** Acceptance (stated
      2026-11-16, before building):
      (a) **Register.** `docs/FEASIBILITY_REGISTER.md` gains rows for the 3a/3b inputs and
      the Layer 5 rates: status feed, squads, appearances, minutes, each per-90 statistic.
      Each row records its coverage per bundle league-season 2019-2025, its as-of
      availability and what it degrades to.
      (b) **Tables.** A forward-only migration adds `player_appearance`, one row per player
      per team-match. Columns: started, bench, minute on and off, minutes, position code,
      grid, slot code (from formation + grid, §7.3), the per-90 statistics the payload
      gives, `source_id` and the temporal fields. Players are keyed by provider id, so one
      player is one row in `player` across clubs. The payload's shape is known only to the
      adapter (`api_football_stats`).
      *Amended 2026-11-16 while building, before any result was read: (1) storage is one
      Parquet file per competition-season in the object store
      (`players/appearances/v1/<league>/<season>.parquet`), not a Postgres table. About
      3.7M rows would take roughly 2.5 GB at `team_match_stat`'s size per row, against
      a 281 MB database today on the free tier. The model reads appearances in bulk, and
      every file is rebuilt from the raw payloads. `player` stays in Postgres. (2) The slot
      code moves to P3-c, which defines the formation vocabulary; the grid is stored, so
      nothing is lost.*
      (c) **Checks.**
      - Each team-match with a lineup has 11 starters.
      - A starter's minutes equal the match length or their substitution minute. A
        substitute's minutes come from their minute on. The payload's `games.minutes` is
        compared with this, and disagreements are reported, not fixed silently.
      - Own goals are never credited to a scorer (§7.4).
      - Rows exist only for matches whose detail is stored; a missing statistic stays
        null, never 0 unless the provider lists it.
      (d) **Report.** Coverage by league-season: appearances, minutes agreement, statistics
      present. Dev and warm-up seasons in the report; holdout seasons are loaded (needed
      for the live fits) but not summarised for modelling choices.
      (e) **Tests.** Parsing on synthetic payloads (starter subbed, substitute subbed off,
      red card, extra time, missing minutes, own goal, a player with two clubs in a
      season).
      (f) **Leakage test.** A loader at as-of t returns only appearances from matches final
      by t.
      **Done 2026-11-16; (a)-(f) pass.**
      - Code: `api_football_stats.player_appearances`, `adatta/features/appearances.py`
        (`build`, `load`, `AppearanceHistory.as_of`, `report`), the CLI commands
        `appearances-build` and `appearances-report`, and `tests/test_appearances.py`
        (5 tests).
      - Build: 362 competition-season files, 3,695,388 rows from 91,726 matches; players
        are registered in `player`. 8,345 finished matches have no lineup.
      - Counted and skipped:
        - 5,222 lineup entries with no player id;
        - 4,419 unclear substitutions (0.6%);
        - 12,345 statistics entries for a player not in the lineup;
        - 2,541 rows whose team is neither side of the match (0.07%).
      - Bundle leagues, dev seasons 2019–2023:
        - 11 starters in at least 99.9% of team-matches;
        - event goals agree with the provider's per-player total in 99.8–100%;
        - per-90 counts are collected in at least 99.2% of team-matches;
        - event minutes are within 3 min of the provider's for a mean of 89.8% to 96.7%
          per league. Lowest: Serie B 2019/20 at 66.6%, where the provider counts stoppage
          time.
      - Register section 4b added. Holdout seasons are built (the live fits need them) but
        not summarised.
- [ ] **P3-a Status feed and squad lists.** Daily status capture runs (since 2026-11-05).
      To add: squad lists per bundle team into `player_registration` (versioned,
      `valid_from/to`), through the quota planner, with the request cost stated first.
      Acceptance stated before building.
- [ ] **P3-b Availability, selection and minutes, validated standalone (§7.1-§7.2).**
      `P(start | available)` and an expected-minutes distribution per player. Acceptance
      stated before building; the shape proposed now:
      - on dev, at the T-24h horizon from realised history only, against two baselines
        ("the previous match's XI" and "season selection frequency");
      - log loss and Brier of start per player, CRPS of minutes, by league and position;
      - the status-informed version judged on the live period (D39).
- [ ] **P3-c Formation, slot-code roles, Manager entity (§7.3).** Register 3c rows are
      verified (formation 99.65%, grid 99.65%, coach 99.59%). Acceptance stated before
      building.
- [ ] **P3-d0 Player rates, Layer 5 (§7.4).** Per-90 rates pooled player → position →
      league, with a dispersion test by position and statistic (§7.4). Market-value prior
      only after the licensing review. Acceptance stated before building.
- [ ] **P3-d Lineup offset: in the likelihood vs serving-only (§7.5).** Both variants
      against the production 1b. Dev walk-forward at the lineups-known horizon, then the
      earlier horizons with P3-b's projected XI. Gate in the D18 form (1X2 log loss and
      RPS, paired 95% interval excluding 0, no league x market segment worse than 1.0%
      per D28, §14.5 inference gates, feasibility). σ_walk is reported for both variants.
      Promotion window per D38. Acceptance stated in full before building.
- [ ] **P3-e Feedback loop into Layer 6 (§7.5 steps 1-4).** Player goal shares sum to the
      team's non-own-goal rate by construction; own goals stay outside the allocation.

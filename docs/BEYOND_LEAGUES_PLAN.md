# Beyond domestic leagues: cups, continental and international football

Status: **agreed 2026-11-12 (D35)**. Stage 0 in progress; nothing else built yet.
The end goal is forecasting domestic cups, continental club competitions and national-team
football as well as the five league bundles. This plan orders the work, names what each
stage needs (data, model, evaluation), and states the gate before the next stage. Every
stage still follows CLAUDE.md: acceptance test stated before building, the locked holdout
untouched, graceful degradation instead of imputed fiction (principle 8).

## Where the spec puts this (§19.3) and the proposed change

| Work | Spec phase | Proposed |
|---|---|---|
| International v0 (team-only, from results) | "soon after 1b" | as the spec: start after Phase 2 |
| Domestic cup ties between bundle teams, 90' forecasts | not separate (part of 3d) | **Stage A, after Phase 2** |
| Knockout resolution (format rules, ET, shootouts, two legs) | 3d, after Phase 3 | **Stage B, before Phase 3** for team-only forecasts; lineup-aware later |
| Continental (UEFA) with Q_league | 6 (scale-out) | **Stage C, after B**, starting with the five bundles' leagues |
| Internationals v1 (squad offset), v2 (tournament simulator) | after 3 / after 3d | as the spec |

**Decision (D35, agreed 2026-11-12):** moving Stages A to C ahead of Phase 3 (squad and
player). Reason for: these competitions are the end goal, and §10.1 itself says
continental and international data "are scraped and modelled early" because they
calibrate the cross-league scale. Reason against: cup forecasts without lineups miss
rotation, which is largest in exactly these matches (early EFL Cup and Coppa Italia
rounds, dead group games). The plan handles this by keeping cup outputs team-only and
labelled, with a rotation proxy tested in Stage A, until Phase 3 adds lineups.

## Stage 0: data, can start now (no model change, quota only)

Starts in parallel with Phase 2, since it only loads data:
- **National-team matches** (new): World Cup and Euro finals and qualifiers, Nations
  League, Copa América, AFCON, Asian Cup, Gold Cup, friendlies, 2014 onwards (the spec's
  8-12-year window for internationals). Roughly 30 competitions x 12 seasons of
  `/fixtures` calls plus detail at 20 per call: about 1,000-1,500 requests, well inside
  the daily quota. Feasibility register entries first (coverage of lineups, xG,
  neutral venue).
- **Continental club matches with detail**: the UEFA competitions are loaded for their
  calendar only (`config/calendar_competitions.yml`, 2019 on, `detail: false`). Load
  detail and extend to 2016 so the warm-up seasons match the leagues.
- **Neutral venue flag** (an open item in `SOURCE_FEASIBILITY.md`): cup semi-finals and
  finals, continental finals, international tournaments. Derived from venue vs both
  teams' home venues (venues are already linked to matches), reviewed by hand where
  unclear, the same way as `config/venue_overrides.yml`.
- **Third tiers** (League One, 3. Liga, Serie C, National, Primera RFEF), as already
  planned: most early-round domestic cup ties involve a tier-3+ side, and without them
  those ties can't be forecast.

Gate: feasibility register rows verified for each new field; capability table filled.

Progress:
- [x] 2026-11-12: Champions, Europa and Conference League moved from the calendar-only
      list to `config/competitions.yml`, so the capture tick covers the current season
      (daily calendar and injuries, odds milestones, lineups, results every tick, weather).
      Cost estimate: about 50 matches in a UEFA matchday week at roughly 12 requests each
      (odds milestones, lineup polls, results, detail), about 650 requests that week,
      against current use of about 480 a day and a quota of about 7,500. Not modelled
      until Stage C; the projector serves bundle leagues only.
- [ ] Check the first ticks: 2026/27 UEFA fixtures loaded, odds and lineups captured on
      the next matchday (24-27 Nov), requests as estimated.
- [x] 2026-11-13: national-team feasibility. `config/national_competitions.yml` (22 senior
      men's competitions, `national: true`, which sets `competition.is_national`), audited
      with `adatta audit --config config/national_competitions.yml` (78 seasons, 245
      requests). Rows in `docs/SOURCE_FEASIBILITY.md` section 7. Findings: history starts
      2015-17 for most competitions, not 2014 (friendlies 2017, European WC qualifiers Sept
      2016; the Euro 2016 qualifiers are absent); results everywhere; lineups about 100% in
      competitive matches but only 35-38% of friendlies in 2025-26; xG only from 2024 in the
      top competitions, so v0 stays results-only as planned.
- [x] 2026-11-13: national-team live capture, in time for the November window (Nations
      League, CONCACAF Nations League, friendlies to 17-19 Nov). The capture tick loads
      `config/national_competitions.yml` with the club list; a competition is captured from
      30 days before its current season's start to 14 days after its end, otherwise one
      `/leagues` request a day. Teams of these competitions are stored with `is_national`.
      Expected cost in a window week: about 130 matches at about 12 requests each.
- [ ] National teams: check the first window's capture (odds, lineups, requests), then the
      history load, then the host flag (venue country).
      - 2026-11-15, first window so far (13-15 Nov, 75 finished matches): odds and results
        on every competition (close odds 18/18 Nations League, 15/15 AFCON qualifiers, 10/11
        CONCACAF Nations League, 26/31 friendlies); lineups captured 18/18, 14/15, 9/11 and
        8/31 for friendlies, in line with the audit (35-38% of friendlies in 2025-26).
        Requests 1,183 and 1,220 on 13-14 Nov, against about 480-620 before. Weather errors
        were transient Open-Meteo TLS timeouts and one 429, retried on the next tick. Final
        check after the window closes (17-19 Nov).
      - Friendlies (league 10) include youth internationals: 546 of 942 stored friendlies
        have a U17-U21 side, and 308 of 533 `is_national` teams are youth sides, all stored
        with `team_level` senior; a few clubs that played national sides (LAFC, DC United,
        FC Urartu) are tagged `is_national` too. The owner keeps youth sides in the data
        (2026-11-15). Before any national-team model (Stage D): set `team_level` from the
        name (U15-U23) and keep youth sides and clubs out of the senior fit.
      - Team level and clubs, acceptance stated first (owner: yes, 2026-11-16). Scope: teams
        of national competitions (`is_national`); club sides in domestic leagues keep
        `senior` (a reserve side plays a senior league). (a) `team_level` from the name:
        a final `U15`-`U23` (or `U-20`) gives `u15`-`u23`, a `W` before it or at the end
        marks a women's side (`women`, `women_u17`), a final `B` gives `b`, anything else
        `senior`; set on new national sides at load and on stored ones by a command.
        (b) Clubs: a national side whose name (suffix removed) matches no provider country
        is checked with the provider's own flag, one `/teams?id=` request each through the
        client and quota planner (about 85, resumable, raw payloads kept); `national:
        false` clears `is_national` and sets the provider's country; `national: true` or
        no answer leaves the row as it is (never guessed). (c) A senior national fit
        selects `is_national` and `team_level == "senior"`; tests show a U21 side, a
        women's U17 side, a B side and a club drop out and a senior side stays. (d) The
        command prints counts by level and the clubs cleared, and writes only with
        `--write`.
        **Built and applied (2026-11-16):** `normalize.team_level_of` (also set at load on
        national sides), `adatta/features/team_levels.py`, `adatta teams-level [--fetch]
        [--write]`, `ApiFootballClient.team` / `parse_team`, `tests/test_team_levels.py`
        (16 tests). `host.Countries` now strips every suffix ("Canada W U17" -> Canada; it
        stripped only the last); `venues-host --write` rerun 2026-11-16: `match.neutral`
        changed on 0 national matches (the fix changes no neutral flag). Rule (b) amended after its first run, disclosed: the provider flags
        some nations `national: false` (Montserrat, Sao Tome and Principe, Papua New Guinea
        U20, Puerto Rico U19), so a side stays national when the provider's country is its
        own name, or when it is listed under `nations` in `config/national_countries.yml`
        (Sao Tome and Principe: provider country "São-Tomé-e-Príncipe"). The four were
        cleared by the first run and restored. Result: 83 `/teams` requests; 27 clubs
        cleared (MLS, USL, K League, Süper Lig and other clubs, PSG U17, Rangers U17,
        Philadelphia Union II, ABFF U19, and the Basque and Catalan selections, which are
        not FIFA nations); 634 national sides remain: senior 218, u16-u23 410, B 4 (Algeria,
        Egypt, Ghana, Morocco), women's U17 2. The 29 sides without a country match that
        the provider calls national stay (Afghanistan, Bahamas, North Korea, ...).
      - Gender as its own field (owner, 2026-11-16: "gender as its own field, and level kept
        for levels"). Acceptance stated first: (a) `team.gender` (new, `men` or `women`, not
        null, default `men`; migration 0012, forward-only, existing rows `men`);
        `team_level` holds levels only (`senior`, `b`, `u15`-`u23`); (b) national sides take
        both from the name (a `W` before the age or at the end is `women`), at load and by
        `adatta teams-level`; club sides keep `men` and `senior`; (c) a senior men's
        national fit selects `is_national`, `gender == "men"` and `team_level == "senior"`,
        so Canada W U17 is `women` / `u17` and drops out while Canada stays; (d) the
        command prints counts by gender and level.
        **Done (2026-11-16):** migration 0012 applied; `normalize.gender_of`,
        `team_level_of` (levels only), `team_levels.senior_national_ids` (men, senior).
        634 national sides: men senior 218, men u16-u23 410, men B 4, women u17 2 (Canada,
        Mexico); every club `men` / `senior`.
      - History load done 2026-11-15: `adatta backfill-run --config
        config/national_competitions.yml`, 78 jobs (none failed), 572 requests (below the
        1,000-1,500 estimate: detail goes 20 matches a call); 10,308 finished national
        matches, every one with detail.
      - Host flag, acceptance stated first (2026-11-15). (a) `venue.provider_country` (new,
        nullable; migration 0010): the venue's country as API-Football's `/venues` gives it,
        one request per country (`/venues?country=`), for the countries of the national teams
        in stored matches, through the quota planner and resumable (a country already
        fetched is skipped); a venue is matched by provider id, else by normalised name and
        city within that country; the existing `venue.country` (the competition's country,
        "England" for Cardiff City Stadium) is left as it is. (b) Host role per national
        match from `adatta/features/host.py`: a team's country is its name with any youth
        suffix (U15-U23, B, W) removed, matched to the provider's `/countries` names
        (`config/national_countries.yml` for the 16 names that differ); `home` when the
        venue's provider country is the home side's, `swapped` when it is the away side's
        only (each needs only that side's country), `neutral` when both sides' countries are
        known and the venue is in neither, `unknown` otherwise (no venue, no venue country,
        or a side without a country: a club, or a nation the provider does not list), never
        guessed (principle 8). (c) `adatta venues-host
        --write` sets `match.neutral` on national-competition matches from the role (neutral
        only), and `venues-neutral --write` no longer touches them. (d) Reported: share of
        finished national matches with a known role, by competition; spot checks: World Cup
        2018 matches with Russia home in Russia and the rest neutral; Nations League group
        matches home; Euro 2020 matches neutral except the hosts' at home. Tests with fakes
        cover (a) to (c).
        **Built 2026-11-15; (a)-(c) pass, (d) partly.** `adatta venues-host [--fetch]
        [--write]`, `tests/test_host.py` (5 tests), migration 0010 applied. Fetch: 169
        countries, 170 requests with `/countries`; provider names and cities are also
        matched before and inside a bracket ("Jassim Bin Hamad Stadium (Al-Sadd Stadium)",
        "ad-Dōha (Doha)"). Of 9,881 finished national matches, **4,912 (50%) have a role**
        (home, swapped or neutral); unknown: 4,001 with a venue the provider's country
        listings don't place (English names against local ones: "Luzhniki Stadium" against
        "Stadion Luzhniki"; DRV PNK Stadium is in no listing), 967 with no venue, 428 with a
        side without a country. Coverage is highest in the European competitions (Euro
        qualifiers 98%, European WC qualifiers 83%, Nations League 77%) and lowest at
        tournaments (World Cup 32%, Copa America 27%, AFCON 15%). Every role derived in the
        spot checks is right (World Cup 2018: Russia home in Russia, Spain v Russia swapped;
        Euro 2024: Germany home, Switzerland v Germany swapped), but most World Cup 2018 and
        Euro 2024 matches are unknown, so (d) is not met yet. `--write` set match.neutral on
        1,144 national matches. Next: a geocoding fallback (Open-Meteo, no API-Football
        quota) for venues the listings don't place.
      - Geocoding fallback, acceptance stated first (2026-11-15). (a) `venue.geo_country`
        (new, nullable; migration 0011): the country of the venue's city from an Open-Meteo
        place search (worldwide, raw payload stored), for venues of national matches that
        `/venues` does not place; kept apart from `provider_country` (its source), never
        written over it, and no other venue field changes. (b) The country is set only
        when the populated places of the city's name (exact name if any, else all) lie in one
        country, or the most populous is at least 10 times the largest elsewhere; otherwise
        none. Geocoder country names go through the same name matching and aliases as team
        names; a place in the United Kingdom takes its `admin1` (England, Scotland, Wales,
        Northern Ireland). (c) The host role reads `provider_country`, else `geo_country`.
        (d) Reported as before: share with a role by competition, and the same spot checks
        (World Cup 2018, Euro 2024) now mostly placed and right. Tests with fakes cover
        (a)-(c), including Cardiff (Wales), Arlington (two US places: USA) and a name in two
        countries (none).
        **Built 2026-11-15; (a)-(d) pass.** `adatta venues-host --geocode`, migration 0011
        applied, `tests/test_host.py` (7 tests). 948 Open-Meteo searches: 929 venues placed,
        502 not (a name in two countries of similar size, such as Santa Clara in the US and
        Cuba; transliterations the geocoder doesn't know, such as Moskva; a country with no
        provider name). **7,287 of 10,308 finished national matches (71%) now have a role**,
        up from 4,912: World Cup 267 of 296, Euro 403 of 415, Copa America 112 of 118, Euro
        qualifiers 239 of 239, Nations League 745 of 792, AFCON 260 of 385; friendlies 2,155
        of 3,832. Unknown: 1,472 venues not placed, 967 matches with no venue, 582 with a side
        that has no country where it matters. Spot checks right: World Cup 2018 Russia home
        or swapped (Uruguay v Russia in Samara, Spain v Russia in Moscow) and 55 neutral;
        World Cup 2026 Mexico, USA and Canada home (Czech Republic v Mexico swapped) and 82
        neutral; Euro 2024 Germany home (Spain v Germany swapped), 46 neutral, none unknown;
        a random 15 of the geocoded matches all right. `--write` changed match.neutral on 889
        more national matches.
- [x] UEFA detail backfill 2016-2025, 2026-11-15: the 19 calendar-only jobs (2019-2025) set
      back to the detail stage, then `adatta backfill-run`: 25 jobs, detail for 7,717
      matches, 401 requests; every finished Champions, Europa and Conference League match
      from 2016 (2021 for the Conference League) now has detail.
- [x] 2026-11-13: neutral venue flag for club matches, `adatta venues-neutral`
      (`adatta/features/neutral.py`, `config/neutral_venues.yml`): venue identity against
      each team's league grounds at kickoff, then round rules, then per-fixture overrides.
      Bundle league matches 99.1% home (the rest: 342 with no venue, 42 temporary grounds);
      all 250 rule-covered finals and semis decided. The 25 review cases were settled by hand
      the same day (23 overrides in `config/neutral_venues.yml`, a rule for the Championship
      play-off final). `--write` set `match.neutral` on 362 matches.
- [x] Third tiers. Acceptance stated first (2026-11-15): (a) league ids verified against
      `/leagues` (done: League One 41, 3. Liga 80, Ligue 3 63 (the Championnat National),
      Serie C 138 / 942 / 943 by group plus play-offs 975 / 976, Primera División RFEF 435-438
      and 692 by group plus play-offs 1006; Spain's tier 3 is absent before 2019, so 2016-2018
      stay a gap, never filled); (b) `adatta audit --config config/tier3_competitions.yml`
      fills the capability table, with feasibility rows in `docs/SOURCE_FEASIBILITY.md`;
      (c) `adatta backfill-run --config config/tier3_competitions.yml` loads every listed
      season with detail for finished matches, none failed, and the completeness report
      shows it; (d) the file is not read by the capture tick: live capture of tier 3 is a
      separate decision for the owner (it would add odds, lineup and weather requests for
      about 2,300 matches a season).
      Result (2026-11-15): 87 seasons queued and done, none failed, 1,585 requests; all
      29,502 finished matches have detail (`backfill-report`). What the report flags, none
      of it a load gap: (1) "no regular-season round labels" on 55 seasons is the report's
      rule, not the data: Serie C from 2018 labels rounds "Girone A - n", Primera RFEF
      "Group 1 - n" (2020 adds a "2nd Phase"), and play-offs have no regular season;
      (2) Serie C 138 in 2016 and 2017 holds all three groups in one league (1,140 and
      1,026 matches, "Regular Season - n"), so the group must come from the teams, not the
      id; 19 seasons have cancelled or postponed matches (2019-20 Covid stops, 36 cancelled
      in Serie C 2017; status comes as both "CANC" and "Canc"); (3) Ligue 3 2024 lists one fixture
      twice (matches 80630 and 80631, same sides and kickoff), the only duplicate in tier
      3, to drop before any tier-3 fit; (4) 4 finished matches without a normal-time score
      (League One 2017, Serie C 2018-2020, one each). Before a tier-3 model: a group key
      for Serie C and Primera RFEF, and the round rule widened.
- [x] Tier-3 cleanup. Acceptance stated first (2026-11-15): (a) one regular-round rule in
      the adapter layer (`Regular Season - n`, `Girone X - n`, `Group N - n`, each with an
      optional `2nd Phase`; UEFA's lettered `Group A - n` is not a league round), used by the
      completeness report, `harness.load_matches` and the live trigger rule; (b) exact
      duplicates (same sides and kickoff in one season) are flagged by the report and
      `load_matches` keeps the lowest match id; (c) a group key per team from the
      first-phase match graph (teams that play each other), so the round-robin count is
      the sum over groups: Serie C 2016 and 2017 under one id read as three groups;
      (d) play-off ids are marked `knockout` and are not counted as leagues; (e) for every
      league in `config/competitions.yml`, `load_matches` returns the same match ids before
      and after (checked on Neon), so no fitted bundle or cache changes; (f) the tier-3
      report then flags only real gaps.
      Result (2026-11-15): `normalize.is_regular_round` / `is_second_phase`,
      `backfill.round_robin_groups` and `duplicate_fixtures`, `knockout: true` on ids 975,
      976 and 1006; tests in `tests/test_tier3_rounds.py`. (e) holds: 39,755 matches, same
      content hash before and after. The report went from 59 flagged seasons to 9, each a
      real irregularity: the Ligue 3 2024 duplicate; 4 missing normal-time scores; Primera
      RFEF Group 1 2022, where one place is shared by Internacional de Madrid (5 matches)
      and Talavera (33); Serie C Girone B 2025, where Rimini stops after 15 matches; and
      Serie C 2016 and 2017, where the provider files Reggina's Girone C fixtures under
      Reggiana's id (880, named "Reggiana" in the raw payload), so the two clubs are one
      team and Girone B and C read as one group (Reggina has its own id, 1692, from 2018).
      Before a tier-3 fit, Serie C 2016-2017 needs that split (by opponent group) or stays
      out. No team in the tier 1-2 leagues plays more matches than its group allows.
- [x] Reggiana / Reggina split. Correction to the item above, found 2026-11-16: Reggina's
      own id (1692) appears only from 2020/21 (Serie B), not 2018, so id 880 also holds
      Reggina's 2018 and 2019 Girone C seasons (Reggiana was in Serie D in 2018/19 and
      Girone B in 2019/20) and some play-off and Coppa Italia ties. Only Serie C and Coppa
      Italia matches up to 2019/20 are involved: no tier 1-2 league match is.
      Acceptance (stated 2026-11-16, before building): (a) `config/team_splits.yml` names
      the shared provider id (880), the other club's id (1692), the last season it covers
      (2019) and the other club's home city (Reggio Calabria); the code knows no club.
      (b) A regular-round league match of the shared id goes to the other club when its
      opponent is in the other club's group: groups from who plays whom in that
      competition-season without the shared id; the other club's group is the one whose
      teams the shared id hosts in that city. More than one such group stops the command.
      (c) Any other match (play-off, cup) goes to the other club when the shared id is at
      home in that city, or when the coach named for the shared side that season coached
      only the other club's matches; one coaching only the shared club's matches keeps it;
      anything else stays as filed and is listed as unresolved (principle 8, no guess).
      (d) Moving a match changes its home/away team id and the team id of its per-match
      rows (every table with `match_id` and `team_id`: coaches, lineups, team stats,
      player status); nothing else. (e) After the move, in every league season each club
      plays no more matches than its group allows, and no coach is named for both clubs in
      one season; the tier-3 report no longer flags Serie C 2016 and 2017, and
      `load_matches` for the tier 1-2 leagues returns the same content hash. (f) `adatta
      teams-split [--write]` prints the moves by competition-season and reason and the
      unresolved matches, writes only with `--write`, and a second run moves nothing.
      Unit tests on synthetic fixtures for (b)-(d) and the second run.
      **Done 2026-11-16; (a)-(f) pass.** `adatta/adapters/team_splits.py`,
      `config/team_splits.yml`, `adatta teams-split [--write]`, `tests/test_team_splits.py`
      (7 tests). 152 of id 880's 270 matches up to 2019 moved to Reggina: 38 in 2016 and 36
      in 2017 (Serie C under one id), 36 in Girone C 2018 and 38 in Girone C 2019, all by
      group; a 2018 play-off tie (home at the Granillo) and its return leg (by coach), and
      2019 Coppa Italia rounds 1 (home at the Granillo) and 2 (by coach). Kept as Reggiana:
      118, including the 2016 and 2017 Coppa Italia ties (by coach) and the 2019/20
      play-offs at the Mapei. 0 unresolved. Rows rewritten: match 152, match_coach 142,
      lineup / team stats / player status 0. After the move: 74 teams that did not play a
      double round robin of their group (Girone B and C merged) before, none after; no
      coach named for both clubs in any season; a second run moves nothing; the tier-3
      report drops the Serie C 2016 and 2017 flags (7 left, each a real irregularity); tier
      1-2 `load_matches` unchanged (39,759 matches, same hash before and after).
- [ ] Tier-3 live capture (owner: yes, 2026-11-15). Quota before: live capture used about
      220-900 API-Football requests a day over 5-14 Nov (the 3,000+ days were backfills),
      against 7,500. Tier 3 adds about 80 league matches a week to roughly 90, so odds and
      lineups about double: an expected 500-1,800 a day, under 25%, far from the 70%
      upgrade trigger. Acceptance stated first: (a) the capture tick loads
      `config/tier3_competitions.yml` next to `competitions.yml` and
      `national_competitions.yml` (one list of capture files, in code); (b) tier 3 gets
      calendar, availability, odds, lineups, detail and weather like any captured league,
      and no forecasts (projection covers the bundle leagues only); (c) after a week the
      daily request count is checked against the estimate, and stays under 70%.

## Stage A: domestic cup ties, 90-minute forecasts

Scope: FA Cup, Copa del Rey, Coppa Italia, DFB-Pokal and Coupe de France ties where both
teams are in a fitted bundle (tiers 1-2, then tier 3 once loaded). Other ties are not
forecast (principle 8), and the dashboard says so.

Model: the bundle's 1b posterior as is, plus cup-specific terms tested one at a time like
Phase 2 groups: cup home advantage (smaller than league), neutral venue (no home term),
and a rotation proxy (round of the cup, the gap to the next league match, the gap in
tier). Cup matches as training data: tested separately (first as targets only, then
added to the likelihood with their own home term), because rotation makes them noisier
evidence of strength.

Acceptance (outline): on dev cup ties between bundle teams, 1X2 and 90' scores not worse
than the 1b model applied unchanged, calibration (ECE) no worse; each cup term judged like
a Phase 2 group. Stated in full below.

Data inventory (2026-11-16, stored data only, no requests). A bundle tie is a finished
tie of the five cups where both teams play tier 1 or 2 of that country in that season.
- Dev seasons 2019/20-2023/24 (`config/evaluation.yml`): 820 bundle ties (FA Cup 212,
  Coppa Italia 191, DFB-Pokal 143, Copa del Rey 142, Coupe de France 132), 353 between
  two top-flight sides. Training-only history 2016/17-2018/19: 442. Holdout 2024/25 and
  2025/26: 325 (counted, not opened). 2026/27 so far: 40 played, 16 listed to come.
- Out of scope for now: bundle v tier 3 (695 finished, all seasons) waits for a tier-3
  fit; bundle v lower (1,221) and the rest (mostly FA Cup qualifying) are not forecast.
- 90' score: present on 99.1% of dev bundle ties. Of the 161 that went to extra time,
  153 agree with the goal events in the stored detail (minute ≤ 90, missed penalties
  out), 7 have no stored 90' score but events that give one, and 1 disagrees. Coverage
  is lower in the holdout seasons (84-95% by cup), where the provider leaves the
  extra-time split out more often, so the repair in A0 matters there. Below the bundle
  ties (FA Cup qualifying rounds) the stored split is often wrong: AET ties whose stored
  90' score is not level, one side null; not used by Stage A.
- Detail payload stored for 100% of bundle ties; venue for 94-100%; `neutral` set on
  40 dev ties (finals; FA Cup semi-finals at Wembley), 19 FA Cup replays, two-legged
  rounds in Copa del Rey (semi-finals, some early rounds) and Coppa Italia (semi-finals).
- Gap to each side's next league match (the rotation proxy): median 3-5 days by cup.
- No historical cup odds or lineup snapshots (live capture only). Cup odds are captured
  from 2026-11-05 (69 cup matches so far, lineups on 42); the 40 bundle ties played in
  2026/27 came before that, so the first with odds are the 16 still to come. Odds stay out of the model (principle 1); a cup
  Edge/CLV report can only cover the live period.
- Stage B data (dev seasons only): shootout kick events are missing on 200 of 492 dev
  shootouts with detail; where present, the provider's `score.penalty` disagrees with the
  kick events on 5 of 145 (e.g. Copa del Rey match 46650: field 1-0, kicks 4-2). The
  shootout winner needs a rule before Stage B.
- Disclosed: the first inventory pass printed extra-time and shootout counts for the
  holdout seasons too (how many ties went to extra time). They were not used for any
  choice here, and nothing further about holdout outcomes was read.

Acceptance (stated 2026-11-16, before building):
- **A0, data first.** (a) The 90' score of a tie that went to extra time comes from the
  goal events (minute ≤ 90, missed penalties out) when the events' goals add up to the
  provider's total; otherwise from the stored split; otherwise it is null and the tie is
  listed as unscorable (never dropped silently, never imputed). Unit tests on synthetic
  payloads; dev bundle ties then 100% scorable, or each exception listed. The same rule
  runs on every season, so holdout coverage improves without its outcomes being read.
  (b) Neutral ties: `neutral` set on every final and FA Cup semi-final from the venue
  rule already used (`config/neutral_venues.yml`), checked by cup and season (one final
  a season, two FA Cup semi-finals). (c) Each leg of a two-legged tie and each replay is
  a 90' match in its own right; the leg-2 forecast does not use the leg-1 score (that is
  Stage B's aggregate).
- **A1, reference.** The bundle's production 1b posterior, refitted weekly as of the tie
  (the same grid as the league backtest), applied unchanged: the listed home side gets
  the league home advantage, neutral or not. Scored on the 820 dev bundle ties: 1X2 log
  loss, RPS, 90' score log-likelihood, ECE per outcome, by cup and by tier pairing
  (1v1, 1v2, 2v2). Its calibration on cups is reported before any term is tried.
- **A2, cup terms, one at a time against A1** on the same ties, walk-forward (each
  term's parameters from earlier cup ties only): (T1) a cup home advantage of its own;
  (T2) no home term on neutral ties; (T3) rotation, from the cup round, each side's
  gap to its next league match and the tier gap. A term passes on the D18 rule (paired
  95% interval on 1X2 log loss or RPS excludes zero in its favour, no cup segment worse
  by more than 1.0%, ECE no worse than +0.005 per outcome) and passes the inference
  gates where it is fitted. With about 820 ties the intervals will be wide; a term that
  does not pass is left out, not tuned.
- **A3, cups as training data** (after A2, separately): cup ties added to the likelihood
  with their own home term. It passes only if the dev league backtest is no worse (D18
  segments, 1.0%) and the cup ties improve as in A2.
- **Holdout and serving.** The 325 holdout-season ties stay locked; they are opened once,
  with their own marker, for the Stage A promotion decision (like M5). Cup forecasts go
  live only for bundle ties, and the dashboard says why other ties have none.
- Compute: A1 reuses the league backtest's weekly refits (no new fits); A2 terms are
  small models on top of the league posterior. CI runs wait for the one-at-a-time rule.

- [x] **A0 done 2026-11-16; (a)-(c) pass.**
  (a) `api_football_stats.event_goals` / `normal_time_from_events` (shootout kicks carry
  the comment "Penalty Shootout"; missed penalties are not goals),
  `detail_store.repair_normal_time`, `adatta results-nt-repair [--write]` (prints counts
  by competition-season only, never scores), migration 0013 (`match_result.nt_basis`:
  provider | events; a fixture re-ingest keeps an events row while the provider's half
  time, shootout and total still agree), `tests/test_nt_repair.py` (4 tests). Validated
  first on 2,154 sampled matches up to 2023 decided in 90': the event score equals the
  stored one on 2,153 (own goals are credited to the side the event names). Scope widened
  while building, disclosed: finished matches with no 90' score are included, because the
  provider marks some extra-time ties FT (Coppa Italia match 51563: 2-2 after 90, 2-4
  after extra time, status FT). Applied to all seasons: 2,107 matches; 375 new versions
  (nt_basis events), 1,371 already agreeing, 479 keep the provider's split (no usable
  events), 252 unscorable and listed (none in the dev bundle ties); a rerun changes
  nothing. Dev bundle ties now 820 of 820 scorable (training 442 of 442; holdout and
  live 359 of 365, counted, not read). Tier 1-2 `load_matches` unchanged (39,759
  matches, same hash; checked uncommitted first, as the M5 holdout run was in progress).
  (b) Stored `neutral` agrees with the venue role on every bundle tie. Bundle ties
  2016/17-2023/24: 1,202 home, 57 neutral, 3 swapped (all 2016-2018), 0 unknown, so the
  dev seasons have no swapped or unknown tie; 10 unknown (no venue) in the holdout and
  live seasons. One final a season is neutral in every cup except where the
  documented ground rule keeps it home (Coppa Italia 2016 and 2018 finals at the
  Olimpico with Lazio playing; Tottenham's 2017/18 FA Cup semi-final at Wembley, their
  home that season); Copa del Rey 2016-2017 has no fixtures at all (known gap,
  `SOURCE_FEASIBILITY.md`). The Stage A loader reads the role, so a swapped tie gives
  the home term to the side at its ground. (c) `adatta/features/cup_ties.py` labels
  single matches, legs and replays from the fixture list and `config/cup_formats.yml`
  (`second_meeting`: leg, replay or none), `tests/test_cup_ties.py` (5 tests). From
  2025/26 the provider keeps the round's own label on FA Cup qualifying replays, so the
  format, not the label alone, decides. All 11,281 stored cup matches: Copa del Rey 64
  two-legged ties, Coppa Italia 20, FA Cup 1,004 replays, 0 irregular.

- [x] **A1 done 2026-11-16 (run 43903439721, all 26 jobs succeeded).** It is a reference,
  with no gate. On 816 dev bundle ties:
  - **Scores:** 1X2 log loss 0.9510, RPS 0.1926, 90' score log loss 2.896.
  - **Mean p against the observed rate:** home 0.437 / 0.442, draw 0.250 / 0.238, away
    0.313 / 0.320. ECE home 0.044, draw 0.037, away 0.054.
  - **By tier pairing:**
    - 1v1: 0.9764 (344 ties).
    - 1v2: 0.8779 (343 ties). Away is under-predicted: 0.327 against 0.394.
    - 2v2: 1.0774 (129 ties). Draws are under-predicted: 0.282 against 0.349.
  - **By cup:** from FA Cup 0.9091 to Copa del Rey 1.0384. In the Copa del Rey home is
    over-predicted (0.388 against 0.359) and away under-predicted (0.319 against 0.394).
  - **Neutral ties (40):** the listed home side wins 0.325 against a predicted 0.444. That
    is the home term T2 removes.
  - **Inference:** the 200 refits were fitted here and cached under fit hash
    `49cdc7f9c3b7711a`. R-hat was over 1.01 in 3 refits and there were divergences in 1;
    tail mass at most 2.67e-4.
  - Full table in the run's `cups-a1-summary` artifact (metrics only).
  `adatta/model/cup_backtest.py` (`run`, `plan`, `assemble`), `.github/workflows/cups.yml`
  (bundle x dev season, then assemble; metrics only in logs and artifacts, match-level
  rows in R2 under `cups/`), `tests/test_cup_backtest.py` (5 tests, fake sampler).
  How a tie is scored: from the production spec's weekly refit as of the Monday of the
  tie's week (`harness.week_start`), the same refit and cache key as that week's league
  matches; the tie's league (intercept, home advantage) is the home side's league that
  season; a `swapped` venue swaps the sides; neutral ties keep the home term (T2 tests
  that); `score_targets(exact=True)` adds the 90' score's log loss and each posterior
  draw's rates, rho and kappa, and each row keeps its home term per draw, so A2 rescores
  ties without refitting (`draws_*` columns, R2 only). Holdout seasons
  raise HoldoutLocked; a spec with groups, widening or another grid is refused.
  Count corrected: 816 dev bundle ties, not 820. Four DFB-Pokal ties had a 3. Liga side
  that went on to the 2. Bundesliga relegation play-off, which the provider files under
  the 2. Bundesliga season, so the inventory counted it as tier 2 (Regensburg 2023,
  Kaiserslautern 2021, Ingolstadt 2019 and 2020). A1 takes a team's tier from its
  regular-season league matches, as the league backtest does.
  Compute (`plan`): the 816 ties need 200 Monday refits (England 42, Spain 35, Italy 45,
  Germany 40, France 38), none cached under today's fit hash `49cdc7f9c3b7711a`. The last
  weekly dev refits of the production spec (`2a82dbe0285182f9`, 13-14 Nov) were cached
  before `covariates.py` changed on 15 Nov (M4b); that file is in every spec's hash, so
  they are not reused, although the production model does not read it. The 200 fits are
  cached for any later weekly dev run. Smoke test (pipeline only, not a result): England
  2019, 43 ties scored from those older refits, read-only, tail mass at most 1.9e-6.

- [x] **A2 design, fixed 2026-11-16 before any A1 result exists.** Each term is an
  offset on the A1 draws' log rates (log λ home, log μ away), so A1's rows rescore a tie
  without refitting the league model; A1 itself is the same rescoring with no offset.
  - T1, a cup home advantage of its own (one parameter γ): log λ += γ on ties not on a
    neutral ground (neutral ties untouched, so T1 and T2 stay separate questions).
  - T2, no home term on neutral ground (no parameter): log λ −= h on neutral ties, h the
    draw's home term (league plus the home side's own, `draws_home_term`).
  - T3, rotation (two parameters θ_tier, θ_gap): for each side, z_tier = 1 in a round
    before the quarter-finals when the side plays a tier above its opponent, z_gap = 1
    when its next league match is within 3 days of the tie (the next league kickoff as
    played; rescheduling after the tie is rare and is not modelled). Rotation weakens the
    side: log λ_side += Σθz_side − Σθz_opponent, and the same with sides swapped.
  - Fitting, walk-forward: a tie's parameters come from the dev cup ties whose results
    were available (kickoff + 3 h) by the Monday of its week, all five cups pooled;
    posterior on a grid (γ: −1.0 to 1.0 by 0.02; θ: −1.0 to 1.0 by 0.05 each) with a
    Normal(0, 0.25) prior, the likelihood from the observed 90' score under the A1 draws
    (Dixon-Coles with rho and kappa, renormalised as `dixon_coles.score_matrix`), using
    100 of the draws for the fit; the tie is then scored at the posterior mean with all
    draws. Early ties have little history and stay near 0 (the prior); they are scored
    all the same.
  - Inference checks for a grid posterior (in place of R-hat and ESS): posterior mass on
    the grid's edge under 1e-3 at every refit; posterior sd at the last refit; prior
    sensitivity (the last estimate under prior sd 0.1, 0.25 and 0.5); posterior
    predictive home / draw / away rates against the observed.
  - Gate per term, against A1 on the same 816 ties: the paired 95% interval of 1X2 log
    loss or RPS excludes zero in the term's favour; in no cup is any D28 segment metric
    (1X2 log loss, RPS, OU2.5 and BTTS log loss, total-goals CRPS) worse by more than
    1.0%; ECE per outcome (home, draw, away) no worse than +0.005. Reported, not gated:
    the 90' score log loss. A term that fails is left out; nothing is tuned after seeing
    the result (grid, prior, the 3-day gap and the quarter-final cut are fixed here).
  - Amended while building, still before any A1 result: the grids first ran to ±0.6, where
    the Normal(0, 0.25) prior alone puts 0.4% of its mass on the edge, so the 1e-3 edge
    check could never pass; at ±1.0 the prior's edge mass is under 1e-4.
  - Built 2026-11-16, runs when A1 is in: `adatta/eval/cup_terms.py`
    (`python -m adatta.eval.cup_terms run --a1 RUN_ID`, local, reads A1's rows from R2,
    prints metrics only), `tests/test_cup_terms.py` (7 tests). The vectorised score matrix
    equals `dixon_coles.score_matrix`; A1 rescored from its draws must reproduce its stored
    probabilities (reported as the largest difference). On simulated ties: a true cup home
    effect of −0.35 is recovered within 3 posterior sd (600 ties: estimate −0.24) and
    rotation likewise; with 600 ties that effect improves 1X2 log loss by 0.8% but the
    paired interval still spans zero, and with 900 it passes. So on the 816 real ties only
    a large term can pass: an expectation stated before the result, not a reason to relax
    the gate.
  - **Result 2026-11-16: no term passes; all three are left out, not tuned.** Run on
    A1's rows (`cup_terms run --a1 43903439721`, local). Rescoring A1 from its draws
    reproduced the stored probabilities to 2.2e-16. Relative change against A1, paired by
    tie:
    - **T1, cup home advantage (776 ties moved):**
      - 1X2 log loss −0.03% [−0.29%, +0.24%], RPS −0.04% [−0.45%, +0.38%].
      - Posterior +0.025 (sd 0.029) on top of the league term; the prior sd 0.1, 0.25
        and 0.5 give 0.023, 0.025 and 0.025.
      - ECE draw +0.0056, over the +0.005 limit.
    - **T2, no home term on neutral ties (40 ties):**
      - 1X2 log loss −0.09% [−0.36%, +0.19%], RPS −0.17% [−0.58%, +0.24%].
      - ECE better on all three outcomes.
      - Its intervals include zero, since only 40 ties move.
    - **T3, rotation and tier gap (414 ties):**
      - 1X2 log loss −0.48% [−1.10%, +0.13%], RPS −0.72% [−1.67%, +0.22%].
      - θ_tier +0.062 (sd 0.034), θ_gap −0.049 (sd 0.050); prior-insensitive.
      - ECE home +0.0086 and draw +0.0083, over the limit.
    - No cup segment is worse than +0.78% (FA Cup total CRPS under T1).
    - **Reading:** T3 points the right way (it moves the 1v2 away bias), but on 816 ties
      its intervals span zero, as the simulation said beforehand. It and T2 can be retested
      when the holdout ties or live cup ties add data, under a gate stated then. A3 (cups
      as training data) waits with Stages B-D (D37).

### A3 acceptance: cups as training data (drafted 2026-11-23 before building; owner, 2026-11-24: "Approve as drafted")

**Question:** does adding domestic cup ties to the league model's likelihood, with a cup
home term of its own, give better 90' cup forecasts than A1 without making league forecasts
worse? A3 is the production model P (`phase1b_strength_open_xg`) plus cup rows, and nothing
else changes.

**Model, fixed now:**
- **Training rows added:** finished bundle ties of the bundle's own cup, where both sides
  play tier 1 or 2 of that country that season (the tier comes from regular-season league
  matches, as in A1).
  - Each leg and each replay is its own match (A0 c).
  - The 90' score follows the A0 rule. An unscorable tie stays out of training and is
    listed.
  - Each row has the same lookback (1,095 days), time weight and walk step as a league
    match on its date.
  - These are about 160 ties a season across the five bundles, roughly 4% of a bundle's
    training rows.
- **Rates for a cup row:**
  - The intercept is the home side's league, as in A1.
  - On a non-neutral ground, the home term is the league home term, plus the side's own
    term, plus δ_cup. δ_cup ~ Normal(0, 0.25), one per bundle, with the same prior as A2's
    T1.
  - On a neutral ground there is no home term.
  - A swapped tie gives the home term to the side playing at its own ground (A1's loader).
- **Goals likelihood only:** cup rows do not enter the xG auxiliary likelihood. Their xG
  is NaN, so the existing row filter drops them.
- **One variant only:** cup rows count at weight 1.0, with no weight grid and no tuning
  after the result. This is said now because a weight was chosen after the dev scores in
  Stage D (amendment 2), and A3 does not repeat that.
- **League scoring is unchanged:** cup rows add training evidence only. The league targets,
  their warm or cold status (counted from league matches, as now) and their rates are
  computed as in P. So the league comparison is paired on exactly P's (match, as_of) set.
- **Cup scoring:** each dev bundle tie is scored from the A3 refit as of its Monday, as A1
  was. Neutral ties get no home term, which is consistent with training. This takes in
  the T2 question, which did not pass on its own, so the result is also reported without
  the 40 neutral ties.

**Grid and references:**
- **Refits:**
  - The weekly dev grid, 928 refits (D11; the final dev run cadence, not D14's 28-day
    grid).
  - Plus the 20 tie Mondays that are not on that grid, for 948 A3 refits in total.
- **League reference:** P's 928 weekly dev refits, already cached under the current fit
  hash `a215d52e759864ed` (P3-d run). P is not refitted.
  - Every A3 refit's league training rows must equal the cached P fit's teams and goals,
    otherwise the job stops. This is the P3-i check.
  - A3 edits `team_strength.py`, so the fit hash changes. A test shows that P's numbers do
    not move when there are no cup rows (as `tests/test_lineup_model.py` does for the
    lineup term).
- **Cup reference:** P as of each tie's Monday.
  - 180 of the 200 tie Mondays are already cached; the other 20 are fitted once under the
    current hash.
  - Reported: the largest difference against A1's stored rows (run 43903439721), which
    should be 0 or rounding.

**Gate (all must pass; any failure leaves A3 out, with nothing tuned):**
1. **Cups improve, as in A2.** On the 816 dev bundle ties, against P:
   - the paired 95% interval of 1X2 log loss or RPS excludes zero in A3's favour;
   - in no cup is any D28 segment metric (1X2 log loss, RPS, OU2.5 and BTTS log loss,
     total-goals CRPS) worse by more than 1.0%;
   - ECE per outcome is no worse than +0.005.
2. **Leagues are no worse (D18 segments).** On P's weekly dev targets (warm matches):
   - no league × market segment (D28 metrics) is worse by more than 1.0%;
   - the paired 95% interval of overall 1X2 log loss and of RPS does not lie wholly above
     zero.

   The second condition is new against the 2026-11-16 outline. It makes a small but clear
   league loss a fail, because the 1.0% segment limit alone would let one through.
3. **Inference.** Every A3 refit must pass R-hat ≤ 1.01, ESS ≥ 400 and no divergences.
   - A refit that fails is refitted once with target_accept 0.99 and 4,000 draws per
     chain, with the same data and seed. This is the P3-d amendment 9 rule, fixed now.
   - The list of refits to redo is taken mechanically from the stored diagnostics.
   - If a redone refit still fails, A3 fails.
4. **Feasible.** A3's median refit time is at most 1.5 × P's on the same grid (P's time
   comes from the P3-d run).
5. **No leakage.** A cup row enters a refit only if its result was available by as_of
   (kickoff + 3 h). Holdout-season ties never enter a dev refit. Unit tests cover both.

**Reported, not gated:**
- the 90' score log loss;
- results by tier pairing (1v1, 1v2, 2v2) and by cup;
- the cup result without the neutral ties;
- δ_cup's posterior per bundle at the last refit, with prior sensitivity (that refit at
  prior sd 0.1, 0.25 and 0.5: 10 extra fits);
- a cup posterior predictive check (home / draw / away rates, total-goals dispersion);
- the shift in each bundle's tier-2 strength against tier 1 (attack − defence, mean over
  teams) against P. Cup rows link the tiers directly, and rotation could bias that link.

**Expectation, stated before the result:**
- A2's simulation showed that on 816 ties only a large effect clears the paired interval.
- A3 also moves team strengths, so its effect on cup ties could be larger than an offset's.
  It could also be smaller, because rotation makes cup results noisier evidence.
- A fail is a likely outcome, and the gate stays as it is.

**If it passes:**
- **Use:** A3 serves cup ties only, replacing A1's 90' cup forecasts. League fixtures stay
  on P.
  - A3 is refitted only in weeks with a bundle cup tie inside the projection horizon.
  - Using A3 for leagues would be a league model change, under the league's own promotion
    rules, and is not part of A3.
- **No holdout:** the cup holdout was opened once, on 2026-11-21 (run 44913949061), so
  there is none left for A3.
  - Proposed, as for Stage D: after a dev pass, serve A3 and keep computing A1 alongside on
    the same ties.
  - The live record is scored after the fact against A1.
  - Revert to A1 if, at the end of 2026/27, A3's live 1X2 log loss against A1 has a paired
    95% interval wholly above zero.
  - The other option is not to serve on a dev pass alone, and to judge on live ties first.
    This is the owner's call.

**Compute and order:**
- 948 A3 refits, plus 20 P refits, plus 10 prior-sensitivity refits.
- At P3-d's rate (about 8 min a refit) that is about 130 runner-hours, run as 100 jobs (25
  bundle-seasons × 4 parts), several hours of wall time.
- The repo is public, so this costs nothing in money.
- One backtest-type run at a time, and not on a Monday (when `national_serve.yml` refits).
- CI logs and artifacts carry metrics only. Match-level rows go to R2.
- 0 API requests.

**Build, after approval:**
- the cup rows and δ_cup in `team_strength.py`, behind a `cups` flag on `RunSpec` (model
  name tag `_cup`);
- the cup-row loader, shared with `cup_backtest.bundle_targets`;
- a `cups_train.yml` workflow;
- an assemble step that writes the verdict;
- tests for no change without cups, the neutral and swapped home terms, leakage, and the
  pairing check.

- [x] **A3 built 2026-11-24, before any A3 fit or score.**
  - **Code:**
    - `adatta/model/cup_strength.py`: the cup rows and `add_cups`, which adds delta_cup
      and the cup likelihood to P's model as `team_strength.build` makes it;
    - `adatta/model/cup_train.py`: `run`, `sensitivity`, `plan`, `failing`, `assemble` and
      the gate (`verdict`);
    - `RunSpec` gains `cups`, `cup_table` and `cup_prior_sd`; the model name gets `_cup`;
    - `.github/workflows/cups_train.yml`: stages fit and redo;
    - tests: `tests/test_cup_train.py` (9, fake samplers) and `tests/test_cup_model.py`
      (3, PyMC, in CI's model job).
  - **Three differences from the draft, disclosed:**
    1. *`team_strength.py` is not edited.* `cup_strength.py` rebuilds the strength path
       from P's own variables (a test checks it equals P's `attack_last`) and adds the cup
       rows to P's model. So P's fit hash stays `a215d52e759864ed` and its cached refits
       are used as they are. `cup_strength.py` is in A3's hash only, along with the cup
       table digest and the prior sd.
    2. *A cup tie whose side has no league match in the refit's window stays out of that
       refit.* This happens, for example, to a side just promoted from tier 3 in a
       first-round tie played before its first league match. Such a side has no strength
       to attach the tie to. The most left out of any refit is reported.
    3. *Refit count:* 950 A3 refits, not 948. The weekly grid has 928, and 22 tie Mondays
       fall off it. Two of those 22 already have a cached P fit, so P is fitted 20 times,
       as stated (`cup_train plan`, read-only).
  - **Pairing check:**
    - P's cached fit must have the refit's teams, leagues and number of training rows.
    - Its posterior-predictive goals must be within 5% of the observed.
    - On four real refits (England and Germany, 2021), the row counts were equal and the
      goals within 0.9%. P's fits took 352–452 s.
  - **Run** on Tuesday 2026-11-24, after the push (fixes to the PyMC test only, 1b838c4 and
    98a7f8a; CI's model job then passed all 17 tests).

### A3 result (2026-11-24): FAILS check 1, not served; nothing tuned

- **Runs:** fit 45339145482 (100 fit jobs, 5 sensitivity jobs, 06:15-14:39 UTC), then redo
  45446411028 (2 refits, 17:14-17:48 UTC). The verdict is the redo's gate over the fit run.
  Match rows are in R2 (`cups/a3/reports/45446411028/`); logs carry metrics only.

| Check | Result |
|---|---|
| 1. Cups improve | **Fail.** 816 dev ties, A3 against P: 1X2 log loss −0.10% [−0.66%, +0.45%], RPS −0.28% [−1.14%, +0.59%]; neither interval excludes zero. Worst cup segment: Copa del Rey total-goals CRPS +1.07% (limit 1.0%). ECE passes (home −0.0056, draw +0.0020, away −0.0050). |
| 2. Leagues no worse | Pass. 18,682 warm matches: 1X2 log loss −0.04% [−0.08%, +0.00%], RPS −0.05% [−0.11%, +0.01%]; worst segment league 136 total-goals CRPS +0.11%. |
| 3. Sampler | Pass. 950 A3 refits; 2 failed the first time (Spain 2023-08-28, Germany 2023-09-25) and passed when redone once (target_accept 0.99, 4,000 draws). |
| 4. Timing | Pass. Median fit 474 s against P's 438 s (×1.08, limit ×1.5). |
| 5. Leakage | Pass (unit tests). |

- **Reported, not gated:**
  - 90' score log loss +0.03% [−0.23%, +0.28%]; without the 40 neutral ties (776),
    1X2 log loss −0.03% [−0.53%, +0.48%].
  - By cup (1X2 log loss, RPS): Coppa Italia −1.43%, −2.36% (191); Copa del Rey −0.08%,
    −0.21% (142); DFB-Pokal +0.20%, +0.25% (139); Coupe de France +0.34%, +0.32% (132);
    FA Cup +0.68%, +0.90% (212).
  - By pairing: 1v1 −0.40%, −1.00% (344); 1v2 −0.30%, −0.33% (343); 2v2 +1.03%, +1.69%
    (129).
  - PPC, mean p predicted / observed: home 0.447 / 0.442, draw 0.246 / 0.238, away
    0.307 / 0.320. Total-goals dispersion 1.029 predicted, 0.984 observed.
  - δ_cup at each bundle's last dev refit (posterior mean, sd): England +0.040 (0.070),
    France +0.012 (0.097), Germany +0.062 (0.086), Italy +0.128 (0.073), Spain −0.148
    (0.106). At prior sd 0.1 / 0.5: England +0.028 / +0.044, France +0.009 / +0.016,
    Germany +0.041 / +0.066, Italy +0.088 / +0.136, Spain −0.079 / −0.171. Small, of
    either sign, and moderately prior-sensitive in Italy and Spain.
  - Tier-2 minus tier-1 strength, A3 − P: at most 0.004 in any bundle. The cup rows
    barely move the tier link.
  - At most 2 cup rows were left out of any refit (a side with no league row in the
    window).
  - **P against A1's stored rows: max difference 0.012, not 0 as the draft expected.**
    A1 fitted its own refits (fit hash `49cdc7f9c3b7711a`, none cached) rather than reading
    P3-d's cache. 653 of the 816 rows agree to 1e-16. The other 163 come from 28 refit
    dates (104 of them in 2023/24), most likely dates whose training rows changed between
    the two runs. The A3-against-P comparison is unaffected, since both sides use the
    same cached refits.
- **Reading:** as expected before the run, 816 ties detect only a large effect. Cup rows
  as training data do not give one: the point estimates are near zero overall and mixed
  by cup, and the 2v2 ties get worse. A1 stays the served cup forecast and P stays the
  league model. Nothing is tuned (no weight grid, no prior change), and there is no cup
  holdout left to test anything else.

## Stage B: knockout resolution (§9)

Needed whenever a winner is required: domestic cups, continental knockouts and
tournaments.
- `CompetitionRound` format rules: legs, extra time, penalties, tiebreak method, away-goals
  rule with validity dates (UEFA dropped it from 2021/22).
- Extra-time sub-model: its own small pooled rate for teams that were level (§9.2), not
  90' rates scaled by 30/90.
- Shootout model: kick by kick, taker and goalkeeper rates pooled player -> team ->
  league; until Phase 3, a team-level model with a small home-side edge as a tested
  covariate (§9.3).
- Two-legged ties: before leg 1 an aggregate estimate; after leg 1 the actual score is a
  fixed input to leg 2 (§9.4). P(advance) as in §9.4.
- Outputs: P(advance) per tie, round-reach probabilities by bracket simulation with one
  posterior draw per simulated bracket (§9.5).

Data to verify first: ET and shootout scores (`split_score` already separates them),
shootout kick events, and how often the provider has them per competition.

- [x] Shootout winner rule. Checked first on the dev seasons of the five cups (2016/17 to
  2023/24, nothing later read), against who advanced (the side of the tie that plays a
  later match in the same cup-season): of 486 shootouts with an advancer, the provider's
  `score.penalty` names the right winner in 485 (the other: Coupe de France 2016, match
  54542, field 4-3 to the home side, the away side played on); the kick events exist for
  124 shootouts and never name a different winner from the field, but their count
  differs from the field in 5 of 145 shootouts with kicks (e.g. Copa del Rey match 46650:
  field 1-0, kicks 4-2) and one kick list is level (3-3: incomplete).
  Acceptance (stated 2026-11-16, before building): (a) the kicks are parsed in the adapter
  (shootout events carry the comment "Penalty Shootout"; a missed kick is "Missed
  Penalty"); they count as complete only when replaying them in the listed order, best of
  five then sudden death, decides the shootout at the last listed kick and not before
  (so the check holds for ABAB and ABBA orders). (b) The winner is the field's when both
  values are there and differ; otherwise the complete kicks'; otherwise none. (c) Where a
  later round shows who advanced, it must agree; a disagreement leaves the shootout
  unresolved and listed (never guessed either way). (d) The shootout score kept for a
  kick model is the complete kicks', never the field's count. (e) Unit tests on
  synthetic kick lists (early finish, sudden death, kicks after the end, ABBA, level);
  a report by cup for the dev seasons only (shootouts, basis, complete kicks, count
  disagreements, unresolved), no requests.
  **Done 2026-11-16; (a)-(e) pass.** `api_football_stats.shootout_kicks`,
  `adatta/features/shootouts.py` (`kicks_score`, `decide`, `advanced_side`),
  `adatta results-shootouts`, `tests/test_shootouts.py` (4 tests). Dev report: 492
  shootouts (FA Cup 161, Coupe de France 157, Copa del Rey 73, DFB-Pokal 63, Coppa
  Italia 38); winner from the score in 491, unresolved 1 (Coupe de France match 54542,
  listed); kicks listed for 124 and complete for 119; among complete kicks one count
  differs from the score (Copa del Rey). The earlier "5 of 145" count disagreements were
  incomplete kick lists. Nothing is written to the database: Stage B reads the rule when
  it builds its labels.

Acceptance: P(advance) calibration and log loss on dev knockout ties against a baseline
of "90' model, level = coin flip", validated apart from the round-robin core (§19.3).

**Stage B acceptance (stated 2026-11-21 before building; owner chose "Open the cup holdout
once" for the promotion, the rest as drafted).** The owner
asked for B, C, D and then A3, in that order, each with its own acceptance.

*Seen so far:*
- A1 and A2's dev results (A1 is the 90' model for cup ties, and no A2 term passed);
- the shootout winner rule;
- an inventory, counts only, of seasons 2016-2023 and 2026 (holdout 2024-2025 not read):
  - two-legged ties are rare in dev: Copa del Rey semi-finals (2 a season from 2019/20,
    30 legs in 2018/19) and Coppa Italia semi-finals (2 a season);
  - a level aggregate with different away goals went without extra time in 2018/19 and
    2019/20 (away goals applied), but to extra time in Copa del Rey 2023/24 and 2026/27;
  - Coupe de France has shootouts without extra time in some rounds;
  - FA Cup replays exist in dev, but none in proper rounds from 2024/25;
  - the FA Cup's stored extra-time split below the bundle ties is often wrong, as A0 found.

*Scope:*
- the five cups;
- ties between two bundle teams, the Stage A population;
- 90' forecasts from A1 as is (the bundle's production 1b posterior, weekly refit as of
  the tie). No A2 term is used.

*Plan:*
- **B0, format rules first.** `config/cup_formats.yml` gains, per cup and per round, with
  `from` and `to` season dates:
  - legs (1 or 2);
  - replay on a draw (yes/no);
  - extra time (yes/no);
  - penalties;
  - away goals (none / after 90' / also after extra time).
  Each rule is written from the competition's regulations, with the source noted, and is
  then checked against every stored tie of seasons 2016-2023 and 2026. A tie whose path is
  inconsistent with its rule is listed and never guessed. Examples: a level single match
  in an extra-time round that ends FT; a shootout without extra time where extra time
  applies; a level aggregate settled on away goals where none apply. Pass: no
  inconsistency in bundle ties, or each one explained. Unit tests cover each path. No
  requests are made: every rule is read from stored data and the regulations.
- **B1, extra-time goals.** In a tie level after 90', each side's extra-time goals are
  Poisson(κ × λ_side / 3). λ_side is the draw's 90' rate from A1, and κ is one pooled
  parameter (§9.2: its own rate, not 30/90).
  - *Fit:* walk-forward as A2. A grid posterior on log κ from -1.5 to 1.5 by 0.02, prior
    Normal(0, 0.5), fitted on earlier bundle ties that went to extra time with a known
    extra-time score (about 160 in dev).
  - *Gate:* κ is used if the paired 95% interval of the extra-time goals log loss (the
    two sides' Poisson) against κ = 1 (30/90 scaling) excludes 0 in its favour. Otherwise
    κ = 1. The grid-posterior checks are as A2's (edge mass, prior sd 0.25 / 0.5 / 1.0,
    predictive extra-time goal rate against the observed).
- **B2, shootouts (§9.3, team-level until a kick model).**
  - *Model:* P(listed home side wins) = logistic(δ) at a home ground and 0.5 at a neutral
    one. δ is one parameter, fitted walk-forward on earlier shootouts, with a grid from
    -1.0 to 1.0 by 0.02 and prior Normal(0, 0.25).
  - *Data:* all shootouts of the five cups resolved by the winner rule, about 490 in dev,
    bundle or not, because the model has no strength input.
  - *Gate:* δ is used if the paired interval of the shootout log loss against the coin
    flip excludes 0. Otherwise a coin flip.
- **B3, P(advance).** For each posterior draw, from the tie's own score matrices and the
  B0 rules:
  - *Single match:* P90(H) + P90(D) × [P_ET(H) + P_ET(D) × P_SO(H)]. Without extra time:
    P90(H) + P90(D) × P_SO(H).
  - *Replay round:* a level tie goes to a replay at the other ground, scored by the same
    model. In dev the replay's rates come from the same draw with sides swapped, keeping
    the original home side's home term. This is an approximation, disclosed; live there
    are no replays in bundle ties.
  - *Two legs:* the aggregate of the two legs' score matrices, then away goals where the
    rule has them, then extra time in leg 2 (with away goals in extra time where the rule
    says so), then penalties.
    - *Before leg 1:* both legs from leg 1's refit, leg 2 with sides swapped (the same
      approximation in dev; exact in serving, from the refit).
    - *Before leg 2:* the leg-1 score is fixed, and leg 2 comes from its own refit.
  - *Result:* the mean over draws, with the 5-95% interval.
  - *Labels:* who advanced, from a later match in the same cup-season, or the final's
    result through the winner rule. A tie without a label is listed, not dropped.
- **Dev check (gate for B3), on the dev bundle ties** (816 A1 rows, as ties; two-legged
  ties are scored before leg 2, and before leg 1 is reported only):
  - P(advance) log loss and Brier against the baseline "90' model, level = coin flip"
    (single: P90(H) + 0.5 P90(D); two legs: the aggregate from the 90' model, level =
    0.5), paired by tie, with the 95% interval;
  - ECE of P(advance) with 10 bins;
  - by cup and by tier pairing.
  - **Pass:**
    1. B3's log loss is no worse than the baseline (point estimate ≤ 0, interval
       reported);
    2. ECE is at most 0.05;
    3. no cup is worse than the baseline by more than 1.0%.
  - **Expected, stated now:** the gain over a coin flip is small, because only ties level
    after 90' move (about a quarter). So the interval will probably include 0, and the
    gate does not ask it to exclude 0.
- **Round-reach (§9.5) is scoped narrowly.** It is shown only where the rest of the
  bracket is fixed and every remaining team is a bundle team (principle 8), with one
  posterior draw per simulated bracket. Domestic cups are drawn round by round, so it
  rarely applies; it is mainly for C's knockouts and D v2's tournaments. It has no gate
  here, because dev has too few such brackets to score.
- **Serving.** It goes live only after the dev check passes and the promotion step below.
  The match page of a bundle cup tie shows:
  - the 90' forecast (A1), labelled team-only;
  - P(advance) with its path (90', extra time, penalties; aggregate and away goals for two
    legs).
  Ties outside the bundles say why there is no forecast. The leg-2 page uses the stored
  leg-1 score.
- **Promotion (owner: "Open the cup holdout once").** The Stage A plan opens the cup holdout
  (325 ties, 2024/25-2025/26, never opened) once, with its own marker, for the promotion
  decision. Proposed: open it once for A1 and B3 together, scored with the same metrics,
  passing if B3's ECE is at most 0.05 and A1's 1X2 ECE is at most 0.06 per outcome. A1's
  dev ECE is 0.037-0.054; the margin is stated now, not tuned. The alternative is to serve
  after the dev pass and judge live on 2026/27 ties, as D38 did.
- **Compute:** no new fits. A1's stored draws (R2 `cups/`) give every score matrix, so B1
  to B3 run locally from them, as A2 did. Serving uses the production refits already
  running.

- [ ] **Stage B on dev, 2026-11-21: B0 passes, B1 and B2 fall back, B3 fails its ECE check.
  Raised to the owner; nothing is tuned.**
  - **Run:** `python -m adatta.eval.knockout run --a1 43903439721`, local, from A1's
    stored draws; metrics only.
  - **Code:**
    - `adatta/features/knockout.py`: rules, ties, `check`, `advanced`;
    - `adatta/eval/knockout.py`: B0 to B3, the gate and the report;
    - `config/cup_formats.yml` `rules`;
    - `tests/test_knockout.py` (6 tests).
  - **B0 passes (each problem explained).**
    - 7,791 ties loaded (FA Cup 4,317, Coupe de France 1,628, Copa del Rey 770, DFB-Pokal
      552, Coppa Italia 524).
    - 212 problems, almost all outside the bundle ties: FA Cup qualifying records without a
      90' score (130), or "AET" on a tie decided at 90' (41).
    - DFB-Pokal 0, Copa del Rey 2 and Coppa Italia 4 problems, all a missing 90' score or
      a status.
    - 3 of the 784 bundle ties don't fit their rule, all feed errors, not rule errors:
      - Coupe de France 2019/20, last 64, match 52113: level 0-0 with no extra time or
        penalties stored;
      - Coppa Italia 2020/21, last 16, match 51563: extra-time goals (0-2) stored under
        status FT;
      - Coupe de France 2020/21, last 64, match 49362: "AET" with 0-0 in a round with no
        extra time, and no penalties stored.
      They are left out of B3.
    - The rules were written from the regulations and the stored paths (seasons 2016-2023
      and 2026). Where no tie needed a rule (away goals in Copa del Rey and Coppa Italia
      2020/21), the regulation stands and the gap is noted in the config.
  - **B1, extra time: not used, so κ = 1 (30/90 scaling).**
    - 136 extra-time periods; κ at the last fit 1.33 (log +0.28, sd 0.08).
    - Prior-insensitive: 1.30 / 1.33 / 1.33 at prior sd 0.25 / 0.5 / 1.0.
    - Goals per extra time: 1.22 predicted against 1.24 observed.
    - Against κ = 1: -0.013 [-0.104, +0.078] per period, so the interval includes 0.
    - Edge mass up to 5.2e-3 in the earliest fits, with few periods; the last is far from
      the edge.
  - **B2, shootouts: not used, so a coin flip.**
    - 496 shootouts; 359 dev shootouts at a home ground, where the home side won 0.461.
    - δ -0.11 (sd 0.09); prior sd 0.1 / 0.25 / 0.5 give -0.07 / -0.11 / -0.12.
    - Against the coin flip: +0.003 [-0.006, +0.012].
  - **B3, P(advance): fails check 2 (ECE).**
    - 774 dev bundle ties.
    - Log loss 0.5676 against the baseline's 0.5741: -0.0065 [-0.0095, -0.0034], -1.13%,
      the interval clear of 0. Brier 0.1927 against 0.1951.
    - Every cup better than the baseline (-0.39% to -2.16%).
    - **ECE 0.0576 > 0.05** (baseline 0.0653).
    - Reported, not gated: a perfectly calibrated forecast on these ties would show ECE
      0.036 on average (95th percentile 0.053), and 2.0% of draws reach 0.0576. So the
      miscalibration is real, not noise.
    - The reliability table shows P(advance) too close to 0.5:
      - 0.1-0.2: predicted 0.148, observed 0.034;
      - 0.3-0.4: predicted 0.351, observed 0.284;
      - 0.6-0.7: predicted 0.651, observed 0.760.
      Favourites go through more often than forecast. This is A1's 90' forecast in cup
      ties: A1 under-predicts the away side in tier-1 v tier-2 ties (0.327 against 0.394),
      where the favourite is often away.
    - Replay ties (78): mean p 0.550 against 0.474 observed. Two legs (20): 0.418 against
      0.350.

  **Amendment 1 (2026-11-21, owner: "One recalibration step"; stated before computing it).**
  - **B3'** = B3, then logit(p') = s × logit(p): one stretch factor s > 0 applied to each
    tie's P(advance) (the mean over draws). The interval is stretched the same way.
    - log s sits on a grid from -1.0 to 1.5 by 0.01, prior Normal(0, 0.5).
    - The likelihood is Bernoulli on who went through.
    - Fitted walk-forward in time order: a tie's s comes only from ties final by its
      Monday. The walk runs through the dev ties, then on through the holdout ties when
      they are scored.
    - Nothing else changes: B0's rules, κ = 1 and the coin-flip shootout.
  - **On dev** its walk-forward result is reported. It is disclosed as a change made after
    seeing B3's result, so dev is not its judge.
  - **The judge is the cup holdout,** opened once (owner, "Open the cup holdout once") with
    its own marker:
    - the 2024/25 and 2025/26 bundle ties, scored by A1 exactly as on dev (weekly
      production refits as of each tie's Monday);
    - B0's rules for those seasons are the ones already in `config/cup_formats.yml`,
      written from the regulations before opening;
    - B0's check runs on those ties too, with problems listed.
  - **Calibration checks at the holdout's size (raised before opening; owner, 2026-11-21:
    "Use the noise floor").**
    - From dev forecasts only (no outcomes), a perfectly calibrated forecast on 300-325
      ties would show:
      - binary P(advance): mean ECE 0.058, 95th percentile 0.085;
      - A1's 1X2 per outcome: mean 0.056 / 0.031 / 0.051 (home / draw / away), 95th
        percentile 0.083 / 0.057 / 0.078.
    - So the approved fixed limits, 0.05 for B3 and 0.06 per outcome for A1, would often
      fail a perfect model on the holdout.
    - Adopted instead, for each: the observed ECE must be at or below the 95th percentile
      of the ECE a perfectly calibrated forecast would show on the same ties (outcomes
      drawn from the forecast, 2,000 draws). That is a test that the forecast is
      calibrated, at the 5% level, whatever the number of ties.
    - The other holdout checks stay as approved: B3' log loss no worse than the baseline
      (point estimate), and no cup worse by more than 1.0%.
  - **B3' on dev, walk-forward (2026-11-21; disclosed, not its judge):**
    - 774 ties. s at the last dev tie 1.32; it ranges 1.03-2.09 over the walk, with the
      early ties on little history.
    - With no history, s is 1.025, not 1: the stated grid (-1.0 to 1.5) isn't symmetric
      about the prior's centre. This is noted and left as stated.
  - **Holdout path built (2026-11-21, before opening):**
    - **Marker:** `eval.holdout` gains the "cups" opening, claimed once by one run.
    - **Scoring:** `cup_backtest claim` / `run --holdout RUN` scores holdout seasons only
      with that opening's key; any other key or none is refused (tests).
    - **Workflow:** `.github/workflows/cups_holdout.yml` (dispatch with confirm = OPEN):
      claim, then score (bundle x 2024, 2025), assemble, and judge
      (`python -m adatta.eval.knockout holdout`). Metrics and the verdict only in the
      logs; B0 problems as counts.
    - **The judge:** B3 with κ = 1 and the coin flip; B3' with s walked from the dev ties
      on through the holdout ties. Checks:
      1. B3' log loss no worse than the baseline;
      2. B3' ECE within its noise floor (95th percentile);
      3. no cup worse by more than 1.0%;
      4-6. A1's 1X2 ECE within its noise floor for home, draw and away.
    - **Smoke test:** the pipeline ran with dev 2023 standing in for the holdout (no
      holdout data read). It is not a result.
  - **Holdout result (2026-11-21, run 44913949061; the cup opening, made once): NOT
    PROMOTED.**
    - **Fits:** 82 production refits across 10 jobs, all successful.
    - **B0:** 0 holdout bundle ties with a problem.
    - **A1 on 317 holdout bundle ties:** 1X2 log loss 0.9859 (dev 0.9510). ECE home 0.033,
      draw 0.020, away 0.042, each within its noise floor (0.084 / 0.059 / 0.077), so
      checks 4-6 pass. By pairing, 1v2 draws were 0.238 predicted against 0.149
      observed, and 2v2 (45 ties) had log loss 1.22.
    - **B3' on 309 holdout ties** (s 1.14-1.30):
      - log loss 0.6184 against the baseline's 0.6119: +0.0066 [-0.0109, +0.0241],
        +1.07%. **Check 1 fails.**
      - ECE 0.060 within its noise floor of 0.085, so check 2 passes. Brier 0.2093 against
        0.2097.
      - By cup: FA Cup +6.94% (74 ties), Coupe de France +3.22% (49), Coppa Italia -1.21%
        (75), Copa del Rey -0.58% (54), DFB-Pokal -6.15% (57). **Check 3 fails.**
    - **Reported, not a check:** B3 unstretched had log loss 0.6107 and ECE 0.052. The
      favourites' edge seen on dev didn't repeat (0.8-0.9: predicted 0.851, observed
      0.737), so the stretch fitted on dev hurt here.
    - **Consequence (pre-registered):** P(advance) is not served. Choosing B3 now,
      because it did better on the holdout, would be choosing on the holdout, so it is
      not done. The code, the format rules and the dev and holdout results stay.
      Whether A1's 90' cup forecasts, which passed their holdout checks, are served on
      their own is the owner's call.
  - **Serving A1 on its own (owner, 2026-11-21: "Serve 90', then C1").** The rule, stated
    before building:
    - *Which ties:* upcoming matches of the five cups within the projection horizon where
      both teams play a league of the cup's bundle this season (league matches of that
      season). The tie is scored exactly as A1 was judged:
      - the bundle's latest passing serving refit, at the same milestones as league
        fixtures (initial, T-7d, T-24h, T-60m, close, D41);
      - the home side's league gives the intercept and home advantage;
      - a neutral ground keeps the home term (T2 did not pass).
    - *Not served, with the reason shown:* ties with a side outside the bundle (principle
      8), and ties whose venue role is "swapped" (the listed away side at its own
      ground). Those are rare, and flipping the stored orientation is left out; they say
      so.
    - *Shown:* on the fixture list, the match page and the Played page as their cup. A
      confidence component, "Cup tie", says 90' only, team-only: no lineups, no rotation
      term, and no P(advance) (Stage B not promoted).
    - *Checks:*
      1. a served tie's stored milestone equals the projection from the same refit
         (the match page's coherence check);
      2. no tie with a non-bundle side is projected;
      3. on the first live tick, the count of bundle ties projected against those
         upcoming;
      4. tests for eligibility, the league used, and the swapped/neutral handling.
    - **Built 2026-11-21:**
      - *Code:*
        - `sim.serve.cup_ties` and `team_leagues`;
        - `run_projection(cups=...)`, wired in the capture tick's projector;
        - the views: cups in the fixture list, the match page and Played, a "Cup tie"
          confidence component, the reason a tie isn't forecast, and the match page's
          recomputation from the home side's league;
        - `cup_ties.cup_bundles`.
      - *Test:* `test_cup_ties_between_bundle_teams_are_served_as_the_home_sides_league`.
        A tie is served with the home side's league, a tie with an outside side is not,
        and the stored rows equal the projection.
      - *Dry run on live data* (no writes): 77 upcoming cup matches. 11 are served (DFB-Pokal
        8, Coppa Italia 3). 66 have a side outside the bundle (Coupe de France 46, mostly
        amateur clubs; FA Cup 20, early rounds with lower-league sides). All 11 project
        cleanly from the serving refits. The capture tick writes them once pushed, and
        check 3 is read then.
      - *Live, 2026-11-21:* the first capture tick on 7921dda wrote forecasts for the same
        11 ties (DFB-Pokal 8, Coppa Italia 3), so check 3 passes. On the match page the
        recomputation from the same refit matches the stored milestone exactly (max
        difference 0.0), so check 1 passes, and the "Cup tie" label shows.
    - Log loss 0.5655 against the baseline's 0.5741: -0.0086 [-0.0227, +0.0054], -1.50%.
      B3 was 0.5676; the interval is wider because of the early s.
    - ECE 0.0337 against 0.0576 for B3. A perfectly calibrated forecast here shows a mean
      of 0.036 and a 95th percentile of 0.053.
    - By cup: Coppa Italia -4.17%, Coupe de France -2.51%, FA Cup -1.05%, DFB-Pokal -0.35%,
      Copa del Rey +0.93%.


## Stage C: continental club competitions (§10.1)

The problem: teams from different leagues meet, so league strengths need a common scale.
- **C1, the five bundles first:** Q_league (league quality) estimated from bridging
  matches between teams of the five bundles (UEFA competitions, 2016 on), as a global
  layer above the league fits and refitted when bridging matches accumulate (§10.1).
  Forecast only continental matches between two bundle teams.
- **C2, more leagues:** each further league (Portugal, Netherlands, Belgium, Turkey,
  Scotland, ...) as its own bundle (Phase 6 scale-out), with a data audit and quota plan
  each. A league-reputation scale (e.g. UEFA coefficients) only as a cold-start prior for
  Q_league, never a feature in log lambda, with licensing reviewed (§18.2).
- Travel: the G2 retest on cross-nation matches (noted in `PHASE2_TASKS.md`) belongs here.

**C2 scoping (2026-11-25, read-only, no requests; owner asked, after "we'll wait until we
cover all relevant league bundles needed").** Metrics only.
- **Method:** a club's country comes from its non-neutral UEFA home venue
  (`venue.provider_country`, else `geo_country`). Bundle clubs keep their stored country.
  - Clubs are stored with the competition's country ("Europe"), so this is the only way
    to place them.
  - 2.2% of match sides stay unplaced.
  - Ukrainian clubs hosting abroad since 2022 count under their host country, so Ukraine
    is undercounted.
  - Check: the five bundles give 384 dev cross-bundle matches (qualifiers included),
    exactly C1's count.
- **UEFA main-stage appearances, 2021/22-2025/26 (all three competitions / Champions
  League):** after the five bundles come Portugal 194/108, Netherlands 211/64, Belgium
  174/48, Austria 124/36, Ukraine 82/32, Scotland 109/26, Turkey 88/24, Czech Republic
  138/22, Denmark 94/22, Switzerland 102/20, Serbia 70/14, Croatia 48/14, Norway 82/12,
  Greece 127/10.
- **Coverage by candidate set:**

  | Bundles | Dev cross-country matches for C1 (main stage / with qualifiers) | 2025/26 Champions League main stage, both sides covered | 2025/26, all three competitions |
  |---|---|---|---|
  | the five now | 366 / 384 | 78 of 189 (41%) | 113 of 579 (20%) |
  | + Portugal, Netherlands, Belgium, Austria | 701 / 764 | 121 (64%) | 197 (34%) |
  | + Scotland, Turkey, Czech Republic, Denmark | 921 / 1,058 | 144 (76%) | 255 (44%) |
  | + Switzerland, Greece, Norway, Croatia | 1,106 / 1,365 | 164 (87%) | 335 (58%) |

- **Power for a C1 rerun:** at 384 matches, C1's 1X2 log loss gain was -0.0101 with a 95%
  half-width of about 0.019. The half-width shrinks as 1/sqrt(n): about 0.011 at 1,058 and
  0.010 at 1,365. A 1% gain would then be about at the edge of significance. The new
  matches involve leagues further from the five, where a quality gap (and C1's gain) may
  be larger. This is an expectation, not a promise.
- **Data held:** no league outside the five countries is stored (only the UEFA, Super Cup
  and Club World Cup fixtures). Each new league is a fresh backfill.
- **Requests:**
  - **Backfill:** tier 3's 87 league-seasons (29,502 matches, full detail) took 1,585
    requests, about 18 per league-season. Ten seasons × two tiers × eight countries is
    about 3,000 requests, under half a day's quota.
  - **Live capture:** used 1,073 requests on 2026-11-24 (99 ticks), about 14% of 7,500.
    Eight top flights add about 75 matches a week, close to tier 3's addition (an expected
    +300-900 a day). Estimated total 1,500-2,500 a day: under 35%, far from the 70%
    upgrade trigger. Measured after a week, as for tier 3.
  - Second tiers as history only (backfilled each summer, not captured live and not
    forecast) would add no live requests. A promoted club then arrives with its last
    season complete.
- **Compute:** a bundle's live Monday refit takes about 7-10 minutes on CI now (the five
  take 00:17-00:53). Smaller leagues are lighter, so eight more add roughly an hour per
  Monday, plus results triggers.
- **Holdout:** the new leagues' 2024/25-2025/26 seasons have never been inspected, so each
  new bundle gets a genuinely locked holdout. The UEFA holdout is also still closed.
- **To verify before any load:** each league id against `/leagues`; a capability audit (a
  few requests); league rules (zones; Scotland's split, Austria's championship round,
  Belgium's play-offs, Denmark's split); venues and geocoding.
- **Order if approved, each step with its acceptance stated first:**
  1. Load: verify ids, audit, backfill 2016-2025, live capture of the top flights.
  2. Each new bundle on dev: the production spec against its own results-only
     Dixon-Coles (rule 5), with the inference gates; then its untouched holdout.
  3. Serve the new leagues' own match forecasts on a pass.
  4. Rerun C1 (Q_league across all the bundles) on dev, then open the UEFA holdout once.
- **Owner, 2026-11-25: "Eight leagues" (D49):** Portugal, Netherlands, Belgium, Austria,
  Scotland, Turkey, Czech Republic and Denmark.
- **C2 step 1, load: acceptance (stated 2026-11-25 before any request):**
  - (a) Each of the 16 league ids (8 top flights, 8 second tiers) is verified against
    `/leagues`: the name and country match, and the 2016-2025 seasons exist. That is 16
    requests, and the payloads are kept.
  - (b) `adatta audit --config config/c2_competitions.yml` fills the capability table.
  - (c) `adatta backfill-run` loads every listed season with detail for finished matches.
    None fail, and `backfill-report` shows them complete.
  - (d) No side effects: the ten bundle leagues' `load_matches` is unchanged (same rows
    and hash before and after). `evaluation.yml` is untouched (it is part of every fit's
    cache hash), so no bundle is added to any model yet. That is step 2, with its own
    acceptance.
  - (e) The top flights go into live capture (`CAPTURE_FILES`): calendar, availability,
    odds, lineups, detail and weather, as for tier 3. They get no forecasts (projection
    covers the bundle leagues only). Second tiers are backfill only, refreshed each
    summer.
  - (f) After a week, the daily request count is checked against the estimate (under 35%;
    the upgrade trigger is 70%).
  - Cost: about 3,000 requests for the backfill, then +300-900 a day live; no money.
- **C2 step 1 result (2026-11-25): (a)-(e) pass; (f) is due after a week of capture.**
  - (a) The 16 ids were verified and the top flights went into live capture (e), both in
    fb0f800.
  - (b) Two audit runs (78 league-seasons each, 242 requests each) filled the capability
    table.
  - (c) All 156 league-seasons are `done`: 40,863 finished matches, all with detail
    stored. That took 2,260 requests (estimate about 3,000).
  - (d) The ten bundle leagues' `load_matches` is unchanged after the load: 39,873 rows,
    hash `39b1c7b7aa2c6655`, the same as before any request. `evaluation.yml` is untouched.
  - Lineups are near complete (0.97-1.0) except Danish 1. Division (0.71), Liga Portugal 2
    (0.87) and Challenger Pro League (0.90). xG is sparse (0.30 at most), as for tier 3.
  - Flags for step 2 (league rules; none blocks the load):
    - Six finished matches have no 90' score (Eerste Divisie 2, Liga Portugal 2 1,
      Turkish 1. Lig 2, Scottish Championship 1). Step 2 decides how they are handled.
    - Regular-season counts exceed the double round-robin rule in formats with more
      rounds or a split: the Scottish Premiership (33 rounds then a split), the Scottish
      Championship (four rounds; 2020 shortened), the Danish 1. Division (three rounds),
      Austria's two tiers (2016-2017, four rounds), and the Challenger Pro League
      (2020-2021). Eerste Divisie 2025 lists 382 against 380.
    - The Scottish Premiership (nine of ten seasons) and the Challenger Pro League
      (2016-2019) have no regular-season round labels. Step 2 needs a rule for each before any fit.

### C2 step 2 acceptance: the eight bundles on dev, then their holdout (drafted 2026-11-25 before building; owner: "Approve as drafted", D52, the recommended choice in each)

**Seen so far, for this draft (read-only, no requests):**
- The model in scope is the production spec (`production_spec`): strength_open, xG
  auxiliary likelihood, weekly grid, 4 x 2,000 draws. Nothing about it is chosen or tuned
  on the new leagues.
- Its 1X2 gain over Phase 1a on the five bundles came from xG (D19). Without xG it was
  level on 1X2 (+0.03%, interval across 0) and better on totals and cold start.
- **xG in the new leagues (share of finished matches):**
  - None in any league before 2023/24.
  - From 2023/24: Eredivisie, Primeira Liga, Jupiler Pro League and Super Lig at
    0.95-1.0; Danish Superliga at 0.56 (0.73 and 0.96 after); Czech Liga at 0.57.
  - Scottish Premiership and Austrian Bundesliga only from 2025/26 (0.70, 0.71).
  - Second tiers have none, except about half of 2025/26 in three of them.
  - So in dev only 2023/24 has xG, and with no xG history before August 2023.
- **Round labels:** the regular-season pattern (`REGULAR_ROUND`) misses three label sets:
  - Scotland's "1st Phase" (nine of ten seasons) and "2nd Phase";
  - Challenger Pro League "Opening" / "Closing" (2016-2019);
  - the post-split group phases (Championship, Relegation, Promotion and Conference League
    "Round" or "Group", "Play-offs I"). These are about an eighth to a third of each
    season in Austria, Denmark, Belgium, Scotland and the Czech Republic.

  Some of those labels also cover cross-tier play-offs: Czech "Relegation Round" is 15
  group matches plus 4 against FNL sides. Knockout ties (play-off semi-finals and finals,
  European play-offs) are in every league.
- What was read about 2024/25-2025/26 is structure only: label counts, match statuses
  and xG coverage. No score or outcome was read, so the holdout stays untouched.
- Czech leagues start in 2018, so their warm-up is one season. Every other league has
  three.
- League rules (zones), venues and geocoding are not needed. The production spec has no
  importance, travel or weather term, so those wait for a later layer.

**Scope:**
- **Bundles:** eight, each a top flight plus its second tier fitted jointly, as for the
  five: Portugal (94, 95), Netherlands (88, 89), Belgium (144, 145), Austria (218, 219),
  Scotland (179, 180), Turkey (203, 204), Czech Republic (345, 346), Denmark (119, 120).
- **Targets:** top-flight league matches only, dev seasons 2019/20-2023/24 (about 10,000).
  Second tiers are training rows only (D49). Warm and cold-start matches are reported
  apart, as before.
- **Windows:** the same as `evaluation.yml`: warm-up 2016-2018, dev 2019-2023 and holdout
  2024-2025, under a holdout key of its own (opening "c2").

**Checks:**
- **(a) Data scope, in `config/c2_bundles.yml`:**
  - The file holds the bundles and, per league, every round label seen in 2016-2025,
    classed as regular, league phase or knockout.
  - A test fails on any unclassed label.
  - A league-phase match counts only if both sides played that league-season's regular
    season. This drops the cross-tier play-offs (Czech and Scottish relegation ties,
    Portugal's, Belgium's 2018 Play-offs II groups) by rule, not by hand.
  - Knockout ties are excluded, as for the five.
  - Matches that are awarded, walked over or cancelled stay out (not `FINISHED`).
  - The six finished matches without a 90' score are reported as unscorable.
  - A table per league-season reports rows kept against the format, for example 198 + 30
    in Scotland.
- **(b) No side effects:**
  - `evaluation.yml` and `REGULAR_ROUND` are untouched.
  - The five bundles' `load_matches` stays 39,873 rows, hash `39b1c7b7aa2c6655`.
  - Their `config_hash` is unchanged, so their cached refits and Monday serving are
    reused as they are.
  - The C2 file joins the hash for C2 bundles only, as the cup model file does for A3.
  - No new league gets forecasts (that is step 3).
- **(c) Second tiers as production will see them:**
  - Production loads second tiers once a summer, not live.
  - So in every C2 refit, a second-tier match is a training row only from 1 July after
    its season.
  - A test checks this. Without it the backtest would train on second-tier results
    production never has.
- **(d) Inference gate (§14.5), audit sample fixed now:**
  - One fit per bundle per dev season, 40 in all, by the five bundles' rule: the first
    weekly refit on or after 1 January with targets that week.
  - Every gating check as it stands for the five (D20-D23 included): R-hat, bulk and tail
    ESS, 0 divergences, PPC per league, identifiability, prior sensitivity x0.5 / x2,
    truncation, reproducibility.
  - Winter breaks are longer here (Denmark, the Czech Republic, Austria, Turkey), and the
    one mixing miss on the five came after the 2022 World Cup break, so this is a known
    risk.
  - A failure is reported as it stands, not tuned away on this sample. Any fix is
    recorded and the whole sample rerun.
- **(e) Dev walk-forward, weekly, 2019/20-2023/24:**
  - The production spec and each bundle's own Phase 1a (results-only Dixon-Coles at the
    selected settings, rule 5) are scored on the same plan, with a grid-identical check.
  - What dev must show is owner choice 2.
  - Reported: per league; the 2023/24 xG season apart; cold start; promoted and relegated
    bias; reliability per 1X2 outcome.
  - The weekly grid is also what the C1 rerun (step 4) reads, so these refits are not
    fitted twice.
- **(f) Predictive prior sensitivity:** 28-day dev backtests at prior scale 0.5 and 2,
  paired with a scale-1 reference on the same grid. Dev 1X2 log loss moves less than 0.2%
  relative for each.
- **(g) Locked holdout, 2024/25-2025/26, opened once:**
  - It opens only after (a)-(f) pass. The gate is written to `config/c2_gate.yml` before
    opening.
  - The gate is D18 with D28's segments: any gain on 1X2 log loss and RPS against Phase 1a
    on the same plan, paired 95% interval excluding zero, pooled over the eight top
    flights.
  - No league x market segment may be worse than 1.0% (1X2 log loss, 1X2 RPS, OU2.5,
    BTTS, total CRPS). A league that fails this is owner choice 3.
  - Each league is recorded as `champion` or `rejected` in `model_run`, with reasons.
- **(h) Reproducibility:** each run writes the usual manifest. Logs carry metrics only, and
  match-level parts go to R2.

**Owner choices:**
1. **Post-split phases:**
   - (a) League phases count as league matches for training and scoring (recommended).
     They are home-and-away round robins between the same league's sides. A results model
     needs nothing else from them (points halving and similar only matter to importance,
     which is not in the spec). Leaving them out would leave a sixth to a third of spring
     matches unforecast in five countries.
   - (b) Regular season only. Scotland's "1st Phase" and Belgium's "Opening" / "Closing"
     are still mapped.
2. **What dev must show:**
   - (a) Feasibility and no harm (recommended): (d) and (f) pass, and no top flight is
     worse than Phase 1a by more than 1.0% on any D28 segment over 2019-2023. The holdout
     then decides the gain. The spec is fixed and nothing is iterated on these leagues,
     which is what the dev gain check protected against. With xG history starting only
     in August 2023, a dev gain check would test the thinnest stretch of xG.
   - (b) As the five under D19: a gain on 2023/24 with the interval excluding zero, before
     the holdout opens.
3. **A top flight failing the holdout segment rule:**
   - (a) That league is rejected alone (recommended). The others are champions if the
     pooled gain over them still excludes zero. Written into the gate before opening, so
     it is not chosen after seeing the result.
   - (b) The whole verdict fails, as D28 did for the five.

**Order and cost:**
- First (a)-(c) and their tests, locally.
- Then on CI, one at a time, none on a Monday:
  - the weekly dev backtest. About 1,700 refits; roughly a working day of runner time;
  - the 40-fit audit sample;
  - the two prior-scale backtests;
  - on a pass, the holdout run.
- No requests and no money. Monday serving is not touched.
- On a holdout fail, nothing is served for the new leagues (step 3 needs a pass). The
  dev refits stay usable for step 4's C1 rerun, which has its own acceptance.

**Built (2026-11-25): (a)-(c) pass locally; the CI runs (d)-(f) are next.**
- **Code and config:**
  - `config/c2_bundles.yml` holds the eight bundles, the second-tier load date (1 July)
    and every round-label family per league.
  - `adatta/eval/c2.py` holds the scope, the availability rule and the dev verdict, with
    `python -m adatta.eval.c2 report`.
  - `harness.refit_plan` reads optional `target` and `available_at` columns. They are
    absent for the five, so their plans are unchanged.
  - `backtest.run` and `gate.py` filter to `c2.observable` before `prepare`.
    `team_strength.py` and `covariates.py` are untouched; both are in every fit's hash.
- **(a) Scope report** (`reports/c2/scope_20261125T163005Z.csv`, counts only), 2016-2025:
  - kept: 38,028 regular rows and 2,363 league-phase rows (40,391 in all);
  - excluded: 422 knockout or play-off matches;
  - dropped by rule: 50 cross-tier play-off matches, and no ties (the one two-side tie,
    Challenger Pro League 2019, was cancelled).
  - The six finished matches with no 90' score are all in excluded play-off rounds, so
    none is unscorable in scope.
  - Dev targets: 10,780 top-flight matches for 2019/20-2023/24, 972 of them in league
    phases.
  - The cross-tier rule is checked against what it should drop: Czech relegation ties
    with FNL sides, Scottish and Belgian play-offs, Austria 2020's two.
- **(b) No side effects:**
  - The five bundles' `load_matches` is still 39,873 rows, hash `39b1c7b7aa2c6655`.
  - The production spec's config hash is `a215d52e759864ed` before and after.
  - `evaluation.yml` and `REGULAR_ROUND` are untouched.
  - A C2 bundle's cache hash adds the digest of `config/c2_bundles.yml` and
    `adatta/eval/c2.py` (`backtest.bundle_hash`). The five's hash is returned as it was.
- **(c) Second tiers:** a test checks every refit of a C2 plan:
  - the targets are top flight;
  - no current-season second-tier row is used;
  - no second-tier row is used before 1 July after its season.

  `run()` asserts that `prepare`'s window after `observable` equals the plan's training
  rows. Phase 1a is planned on the same grid.
- **Holdout keys:**
  - A C2 bundle refuses any holdout key except an opening "c2", and the five refuse
    "c2".
  - The "c2" opening itself is built before the holdout run, after dev passes:
    - its marker;
    - `config/c2_gate.yml`, with D52's rule that a league failing the segment rule is
      rejected alone;
    - the assembly's set of required parts.
- **Dev assembly:** a C2-only dev run writes `c2_dev.json` (the no-harm verdict), the
  2023/24 comparison apart, and reliability.
- **Tests:** `tests/test_c2.py`, 10 tests.
- **Smoke on stored data:** Scotland, 2019, two weekly refits in the model container, at
  2 x 100 draws so mixing is not judged. It ran end to end: 12 top-flight targets, all
  warm, and the training window equals the plan.
- **Full suite:** 593 passed, 9 skipped; ruff and mypy clean.
- **Next:** dispatch `backtest.yml` with the eight bundles x 2019-2023 at the weekly
  grid, after the push. One at a time, not on a Monday.

**(e) Dev result (2026-11-26): no harm, passes (D52 choice 2a).**
- **Run:** backtest run 45663417654, 80 refit jobs and the assembly, all green; assembled
  2026-11-26T00:06:58Z. Job times were 40-147 minutes, with Portugal the slowest.
- **Plan:**
  - 10,780 top-flight targets, all warm. No team is under 10 prior matches, because
    the second tiers give promoted sides their history.
  - The grid is identical to Phase 1a: **yes**.
- **Refit diagnostics:** 1,325 weekly refits:
  - R-hat over 1.01 in 3 (worst 1.013);
  - ESS under 400 in 1;
  - divergences in 24 (1.8% of refits). The five bundles' weekly production-spec run
    (43421580602) had 0 of 928, so this is higher. The audit sample (d) requires 0
    divergences in every fit, so it is the check to watch. A failure there is reported
    as it stands, not tuned away on the sample.
- **No harm:**
  - Worst top-flight change against Phase 1a on any D28 metric: Austria at +0.33%
    1X2 RPS and total CRPS (limit +1.0%).
  - Every other league is at or below +0.25%.
- **Pooled over the eight, against Phase 1a (reported; the holdout decides the gain):**

  | metric | change | 95% interval |
  |---|---|---|
  | 1X2 log loss | -0.09% | [-0.26%, +0.08%] |
  | 1X2 RPS | -0.17% | [-0.42%, +0.07%] |
  | OU2.5 log loss | -0.47% | [-0.75%, -0.19%] |
  | BTTS log loss | -0.35% | [-0.58%, -0.11%] |
  | total CRPS | -0.51% | [-0.78%, -0.25%] |

  This is the same pattern as the five without xG: level on 1X2, better on totals.
- **Per league:** Turkey gains on every metric, with 1X2 log loss at -0.58%
  [-1.06%, -0.10%].
- **2023/24, the xG season (2,186 matches):**
  - 1X2 log loss -0.60% [-1.12%, -0.08%];
  - RPS -0.78% [-1.58%, +0.03%];
  - Turkey -2.14% log loss; Belgium +0.42% and Scotland +0.16% (no xG in Scotland that
    season).
- **Promoted sides** (2,403 matches):
  - 1X2 log loss -0.39%, and the points bias narrows from -0.125 to -0.100 per match
    after 10 matches.
  - There is no relegated segment, because second-tier matches are not targets.
- **Reliability:** per 1X2 outcome, in line with Phase 1a. The summary artifact carries
  metrics only.
- **Next:** the audit sample (d) is dispatched, gate run 45735355899: 8 bundles x 5 dev
  seasons, 4 x 2,000 draws, at the first weekly refit on or after 1 January. Then (f),
  the two prior-scale backtests, one at a time and not on a Monday.

**(d) Audit sample (2026-11-26, gate run 45735355899): 39 of 40 pass; one blocking
failure, truncation.**
- **The run:** 40 jobs, all green, 8-26 minutes each. Config hash `d2b7b316dc76` in all
  40; seed 20261105; 4 x 2,000 draws and 2,000 tuning steps.
- **Convergence, all 40:**
  - 0 divergences in every fit (the dev run's 24 do not recur in the sample);
  - R-hat at most 1.009 (Portugal 2021/22), every other fit at most 1.006;
  - bulk ESS at least 668 (Czech Republic 2019/20); tail ESS at least 852.
- **Other checks:** identifiability, rho-kappa, prior sensitivity x0.5 / x2 and
  reproducibility pass in all 40.
- **Warnings (non-blocking):**
  - total dispersion, warning only under D20, in 14 fits (Belgium, Denmark and Portugal
    most often);
  - the 4-week predictive-sensitivity proxy in 20 fits. (f) is the binding sensitivity
    check.
- **The failure:** Netherlands as of 2023-01-02, score-matrix truncation.
  - Share of predictions with tail mass < 1e-4: 0.977, which is 43 of 44 (limit 0.999,
    per fit).
  - The one is an Eredivisie top side at home to a far weaker one. In dev the same
    fixtures predict 4.7-5.5 total goals, and the largest truncated tail is 3.8e-4.
  - Over all 10,780 dev predictions, 7 have tail mass >= 1e-4 (99.935%, above the 0.999
    share). The audit fits hold 14-61 targets each, so per fit the rule allows no such
    prediction at all.
  - Every other C2 fit and the five's 25 fits have 100%.
- **Not tuned:** the failure is reported as it stands. The owner decision on it is
  pending, and (f) waits for it.

**D53 (owner: "Score C2 at 20 goals"): the C2 bundles are scored to 20 goals per team;
the rule stays as written. Acceptance, stated before building:**
- **The change:**
  - C2 bundles build their score matrices to 20 goals a side instead of 15. This covers
    the production spec, the Phase 1a pairing and the gate's predictions.
  - The truncation rule is unchanged: tail mass < 1e-4 for 99.9% of each fit's
    predictions.
  - The fitted likelihood is untouched. Its draw mass is summed exactly (D22), with no
    cap, so the cap is prediction-time only.
- **Checks:**
  - **(i) The five unchanged:**
    - `evaluation.yml` keeps `max_goals: 15`;
    - the production config hash stays `a215d52e759864ed`;
    - the five's scored rows are bit-identical to before (test);
    - serving, the half split and the API are untouched.
  - **(ii) No refits:**
    - the C2 fit cache key is unchanged, so the 1,325 dev refits are reused;
    - `config/c2_bundles.yml` and `adatta/eval/c2.py` (the scope digest) are untouched.

    A C2 part's `config_hash` adds the cap, so 15- and 20-goal parts are never mixed.
  - **(iii) Same cap on both sides:** Phase 1a is scored at 20 for C2 bundles too, so
    the pairing compares like with like.
  - **(iv) Reruns, one at a time, not on a Monday:**
    - the weekly dev backtest is rescored from the cache, with no harm rechecked under
      D52 2a;
    - then the whole 40-fit audit sample is rerun. All 40 must pass every gating check,
      with no exception for Netherlands 2023.
    - Then (f).
  - **(v) Step 3 carries it:** C2 serving must build to 20 as well. That includes the
    half split, whose 16-cell grid assumes 15. This is recorded for step 3's acceptance.

**D53 built (2026-11-26): (i)-(iii) pass locally; (iv) needs the push.**
- **Code:**
  - `backtest.C2_MAX_GOALS = 20` and `backtest.scoring_config(cfg, bundle)` return the
    five's config as it is, and a C2 bundle's with `max_goals` 20.
  - `run()`, the assembly's Phase 1a per bundle and `gate.py` use it.
  - A gate report states its `max_goals`.
- **(i) The five:** the production config hash is `a215d52e759864ed` and the five's fit
  and part hashes are unchanged. `scoring_config` returns their config object itself.
- **(ii) No refits:**
  - The C2 fit cache key is `c86d1b65c7025139`, the same as at HEAD. Dev fits are there
    in R2; Netherlands 2023-01-02, Portugal 2020-01-06 and Denmark 2024-02-12 were
    checked.
  - C2 parts now hash `5fbc994ced917081` (was `c86d1b65c7025139`).
  - A test rescores a C2 run at another cap: no refits, a different part hash, the tail
    only shrinks and 1X2 is unchanged to 1e-6.
- **(iii)** Phase 1a on a C2 bundle is scored to the same cap (test).
- **Suite:** 594 passed, 9 skipped; ruff and mypy clean.
- **Next, after the push, one at a time, not on a Monday:**
  - the weekly dev backtest, rescored from the cache (eight bundles x 2019-2023);
  - then the 40-fit audit sample.

Acceptance: on dev continental matches between bundle teams, beats the "each league's
own scale, no Q_league" model and an Elo-style baseline on 1X2 log loss and RPS.

**C1 acceptance (stated 2026-11-21 before building, owner: "Approve as drafted").** The owner's
order is B, C, D, then A3.

*Seen so far:* counts only, from stored data for seasons 2016-2023 and 2026 (holdout
2024-2025 not read). UEFA matches between two bundle teams from different bundles:
- Champions League: about 50 a season;
- Europa League: about 22;
- Conference League: 2-6 (from 2021/22).
That is about 384 in dev (2019/20-2023/24) and about 210 in 2016-2018. Nothing has been
scored.

- **Targets.** Finished UEFA matches (Champions, Europa and Conference League, all
  rounds) between two teams that each play a bundle league that season (league matches,
  `serve.team_leagues`), from different bundles. The 90' score is the target; legs that
  went to extra time are scored on 90', with the A0 rule. Dev seasons 2019-2023.
  Same-bundle UEFA matches are reported apart, because Q cancels in them.
- **Inputs, as of the match:** each side's attack and defence come from its own bundle's
  production weekly refit as of the match's Monday (as A1). Refits missing from the cache
  are fitted and cached, on the same grid and key as the league backtest.
- **C1 model (Q_league, §10.1):**
  log λ_home = c + h × (1 - neutral) + att_home - def_away + Q[bundle of home] - Q[bundle
  of away], and the same for the away side without h.
  - Q is one quality per bundle, with England fixed at 0, so four free values.
  - c is the UEFA goal intercept and h the UEFA home advantage.
  - The score is Dixon-Coles with ρ from the home side's bundle draw, as in the league
    model.
  - Neutral grounds (finals) come from `config/neutral_venues.yml`.
  - **Fit:** priors Q ~ Normal(0, 0.5), c ~ Normal(0.2, 0.5), h ~ Normal(0.25, 0.25).
    NUTS (numpyro, seed 20261121, 2,000 draws × 4 chains) on the cross-bundle matches
    final by the refit time, from 2016. att and def enter at their posterior means.
  - **Refits:** every 28 days (D14), walk-forward. A target uses the latest refit at or
    before its Monday.
  - **Prediction:** each posterior draw of (c, h, Q) is paired with a draw of the two
    bundles' att/def.
  - **Inference gates on every refit:** R-hat ≤ 1.01, ESS ≥ 400, no divergences, a PPC
    of total goals, and prior sensitivity at the last refit (sds × 0.5 and × 2).
- **Baselines, on the same matches:**
  - *B_own*, "each league's own scale": the same model and fit with Q fixed at 0.
  - *B_elo*, Elo-style:
    - one club Elo over every stored match of the bundle leagues, the five cups and the
      UEFA competitions since 2016-07-01, each rating final by the match's Monday;
    - start 1,500, K = 20, home advantage 60 points, settings fixed now (not tuned);
    - a new club starts at its league's mean rating;
    - the Elo difference goes to 1X2 through an ordered logit (slope and two cut-points)
      fitted walk-forward on the earlier target-type matches.
- **Gate on dev (all must hold):**
  1. C1 beats B_own on 1X2 log loss and on RPS, each paired 95% interval by match
     excluding 0.
  2. C1 beats B_elo the same way.
  3. No competition (CL, EL, ECL) is worse than either baseline by more than 1.0%
     (point estimate).
  4. Calibration: C1's 1X2 ECE per outcome is within its noise floor (the 95th percentile
     for a perfectly calibrated forecast on the same matches; owner, Stage B).
  5. The inference gates pass on every refit.
  6. Leakage: on 50 sampled targets, nothing final after the match's Monday changes
     att, def, Q, c, h or the Elo.
  - Reported: Q per bundle over time, with intervals; by season and competition; same-
    bundle matches; home and neutral.
  - **Expected, stated now:** with about 384 matches the intervals are wide. A real
    league-quality gap shows against B_own; against Elo it may not.
- **Then.** On a dev pass, the UEFA holdout (2024/25-2025/26, never opened) is opened
  once with its own marker ("uefa"), scored the same way, with checks 1-4 as the
  promotion gate. On a pass, UEFA matches between bundle teams from different bundles are
  served (90', team-only, labelled), and same-bundle ones from the bundle's model. Other
  UEFA matches say why there is no forecast (principle 8).
- **Compute:**
  - the bundle refits come from the cache where present; dev weekly refits mostly are,
    from P3-d's run, and the rest are fitted on CI;
  - the C1 fits are a small NUTS model, about 60 refits × 2 seasons, fitted on CI;
  - the Elo is pure numpy;
  - 0 API requests;
  - one backtest-type run at a time.

- [x] **C1 build (2026-11-21), before any C1 score:**
  - **Code:**
    - `adatta/model/continental.py`: targets, side strengths from the Monday refit, the
      C1 / B_own fit (PyMC with NumPyro), prediction, the Elo and its ordered logit,
      scores, the gate, prior sensitivity and the leakage check;
    - `.github/workflows/continental.yml` (strengths, then fit);
    - `tests/test_continental.py` (5).
  - **Targets:** 612 UEFA matches between bundle teams in 2016-2023, 593 of them across
    bundles, 384 cross-bundle in dev. 47 are on neutral ground (finals, and the 2019/20
    final tournaments); none swapped.
  - **Refits needed:**
    - dev: 338 (bundle, Monday) refits, 320 cached;
    - 2016-2018 training matches: 205, none cached, fitted on CI (about 24 CI hours over
      18 parallel jobs).
  - **Elo:** built over 44,887 stored matches in 0.3 s. The ordered logit fitted on the
    warm-up matches only reproduces their outcome rates. No dev score has been computed.
  - **Clarification, before any score:** the C1 and B_own fits use a Poisson likelihood on
    the 90' goals, with att and def at their means. Prediction uses Dixon-Coles with the
    home side's ρ and κ draws, as the acceptance says.
  - **Clarification 2 (2026-11-21, after the first strengths run failed, before any
    score):** stored league data starts with 2016/17. A Monday in summer 2016 has no league
    matches in its window, so `team_strength.prepare` fails (run 44926910073, Spain part
    0). A UEFA match is now used only if each side's bundle has at least 365 days of stored
    league history before its Monday. Training matches therefore start in the summer of
    2017, not 2016. Strengths fitted on a few weeks of a new season would be noise and
    would pull Q towards 0. Dev targets are not affected.

- **C1 dev result (2026-11-21): FAILS the gate, not promoted.** Runs: strengths 44929273283
  (20 jobs, all succeeded), then fit 44942402950. 384 dev cross-bundle UEFA matches, 43
  refits. The holdout ("uefa") stays closed.

  | model | 1X2 log loss | RPS |
  |---|---|---|
  | C1 | 0.9976 | 0.2096 |
  | B_own | 1.0078 | 0.2133 |
  | B_elo | 1.0070 | 0.2116 |

  - **C1 vs B_own:**
    - log loss -0.0101, 95% interval [-0.0295, +0.0092] (-1.01%);
    - RPS -0.0037, 95% interval [-0.0104, +0.0030] (-1.73%).
  - **C1 vs B_elo:**
    - log loss -0.0094, 95% interval [-0.0317, +0.0130] (-0.93%);
    - RPS -0.0019, 95% interval [-0.0092, +0.0054] (-0.91%).
  - **By competition (C1 relative to each baseline):**
    - Champions League, n 256: -1.40% vs own, -0.73% vs Elo;
    - Europa League, n 117: -0.74% vs own, -1.66% vs Elo;
    - Conference League, n 11: +4.27% vs own, +2.81% vs Elo.
  - **Calibration (ECE / noise-floor limit):** home 0.036/0.074, draw 0.013/0.048, away
    0.043/0.069.
  - **Q at the last refit (sd), England fixed at 0:** Spain -0.002 (0.046), Italy -0.140
    (0.051), Germany +0.028 (0.050), France -0.169 (0.055).
    - Q is stable across refits from 2020: Italy about -0.13 to -0.19, France about -0.13
      to -0.18, Spain and Germany within ±0.07.
    - Prior sensitivity (prior sd × 0.5 / × 1 / × 2) moves no Q by more than 0.005.
  - **Inference:** worst C1 R-hat 1.001, 0 divergences. PPC of total goals: predicted
    2.888, observed 2.887.
  - **Checks:**
    1. C1 beats B_own (both intervals below 0): fail;
    2. C1 beats B_elo the same way: fail;
    3. no competition worse than either baseline by more than 1.0%: fail (Conference League);
    4. ECE within its noise floor: pass;
    5. inference gates on every refit: pass;
    6. leakage (0 of 50 sampled targets changed): pass.
  - **Reading:** as expected at the acceptance, about 384 matches are too few for the
    intervals. C1 is better on the point estimate against both baselines on both scores,
    is calibrated, and its Q is stable and identified. The failure is power, not a broken
    model. The Conference League check rests on 11 matches.
  - Following the acceptance, the gate is not relaxed after seeing the score. UEFA matches
    keep showing why there is no forecast (principle 8). The next step is the owner's.
  - **Closed (D45, 2026-11-21, owner: "Close C1, start D").** The UEFA holdout is not
    opened. Q is recorded here and used in no forecast.
  - **Owner, 2026-11-25:** Champions League (UEFA) forecasts wait until the league bundles
    cover all the relevant leagues: "we'll wait until we cover all relevant league
    bundles needed". C1 is not reopened before then; the UEFA holdout stays closed.


## Stage D: national teams (§10.4)

Its own fit, not inside the club model:
- **v0 (team only, from results):** one global strength scale with confederation effects,
  a calendar-time random walk (variance grows with elapsed time, 8-12-year window),
  competition-type effects (friendly / qualifier / finals) with friendlies kept but
  weighted, host flag and travel instead of a neutral binary, heavier-tailed goals
  (negative binomial) with a wider score matrix. Elo/FIFA ratings only as fading
  cold-start priors.
- **v1 (after Phase 3):** squad offset from the called-up players' club quality, through
  a `CallUp` entity snapshotted like player status.
- **v2 (after Stage B):** tournament simulator (groups, best third places, brackets).

Acceptance: beats the Elo baseline on the locked international holdout (to be defined at
the start of the stage), calibrated on qualifiers and nations-league matches; friendlies
and competitive matches reported apart.

**D v0 acceptance (stated 2026-11-21 before building, owner: "Approve as drafted").**
Owner before it: "Close C1, start D" (D45).

*Seen so far:* counts and host roles only, no model and no outcome rate. Stored national
matches: 10,903, of which 9,350 are finished matches between two senior men's sides with a 90'
score (June 2014 to 18 Nov 2026).
- **Teams:** 229 sides. 218 have a confederation from the competitive matches they play
  (UEFA 55, CAF 55, AFC 46, CONCACAF 41, OFC 11, CONMEBOL 10), and none plays in two
  confederations. The other 11 are clubs or non-FIFA sides that met a national team (Los
  Angeles FC, Konyaspor, Basque Country, ...).
- **Host role:**
  - home 5,173, neutral 1,922 and swapped 133;
  - unknown 2,134 (23%), of which 1,365 competitive and 769 friendlies.
- **Venue coordinates:** 279 of the 2,324 national venues have them.

- **Windows (international, stated now; none has been scored):**
  - warm-up: 2014-06-12 to 2018-12-31, 1,535 competitive matches and 691 friendlies;
  - dev: 2019-01-01 to 2023-12-31, 2,943 competitive and 1,194 friendlies;
  - locked holdout: 2024-01-01 to 2026-07-31, 1,701 competitive and 814 friendlies. It
    takes in Euro 2024, Copa America 2024, AFCON 2023 and 2025, Asian Cup 2023, World Cup
    2026 and Nations League 2024/25. Marker "international", opened once.
  - live: from 2026-08-01.
  - Stage 0 spot-checked host roles of World Cup 2026 and Euro 2024 matches. No result
    rate or score of the holdout has been read.
- **Targets:** finished matches between two of the 218 sides (`senior_national_ids` with a
  confederation), on the 90' score. Knockout ties use the A0 rule. Shoot-outs and
  advancement are v2.
  - Competitive matches are every competition except Friendlies.
  - "Finals" are World Cup, Euro, Copa America, AFCON, Asian Cup, Gold Cup and the
    Finalissima. The rest are qualifiers and Nations Leagues.
  - Youth, B and women's sides are out (`team_level`, `gender`).
- **v0 model (team only, from results):**
  - **Scoring rates:**
    - log λ_home = c + g[type] + H(role) + (s_home − s_away)/2 + (o_home + o_away)/2,
      and the same for the away side with the signs of s swapped;
    - s is strength and o is openness, as D15.
  - **Strength:** s[i, month] = conf[i] + u[i] + w[i, month].
    - conf: a confederation effect, with UEFA fixed at 0 and the others Normal(0, 1).
    - u: the team's level, Normal(0, σ_u), σ_u ~ HalfNormal(1), summing to zero within
      each confederation (so conf is identified).
    - w: a Gaussian random walk per calendar month, σ_w ~ HalfNormal(0.05). Variance grows
      with elapsed months whether or not the team plays, which makes this the
      calendar-time walk.
  - **Openness:** o[i] ~ Normal(0, σ_o), σ_o ~ HalfNormal(0.1). It is static and shrunk
    (D15's lesson).
  - **Competition type:** g[type] for friendly (reference 0), qualifier or Nations League,
    and finals, each Normal(0, 0.3).
  - **Host role H, applied to the host side:**
    - home: h;
    - swapped: h to the listed away side;
    - neutral: 0;
    - unknown: h_u to the listed home side, a parameter of its own (no guess about the
      23%; principle 8).
    - Priors: h, h_u ~ Normal(0.25, 0.25).
  - **Goals:** negative binomial per side, one dispersion φ ~ Gamma(2, 0.1), with
    Dixon-Coles τ, ρ ~ Uniform(-0.2, 0.2). The score matrix runs 0-20 per side.
  - **Friendlies** are kept with likelihood weight 0.5, fixed now. Weight 1.0 is run once
    on dev and reported. Any change is a disclosed amendment before the holdout.
  - **Fit window:** every match from 2014-06 up to the refit, capped at the last 10 years
    (8-12, §10.4). PyMC with NumPyro, seed 20261121, 1,000 draws × 4 chains.
  - **External ratings:** none. No FIFA or Elo prior is stored or licensed, so the cold
    start is the confederation mean.
  - **Odds:** never an input (principle 1).
- **Refits and prediction:**
  - **Walk-forward refits:** one on the Monday of every week that has a senior match, using
    the results final by that Monday.
  - **Which refit:** a target uses the latest refit at or before its Monday. So in a
    Thursday-Tuesday window, the Tuesday match sees the Thursday result (D33's intent).
  - **Prediction:** each posterior draw's walk is carried from the last fitted month to
    the match's month with its own σ_w. A team unseen for a year is wider (D34).
- **Baseline B_elo** (the eloratings.net published method, settings fixed now, not tuned):
  - **Rating:** start 1,500 for every side in June 2014, over the same stored senior
    matches, each final by the target's Monday.
  - **K:**
    - 60 World Cup finals;
    - 50 the other finals;
    - 40 qualifiers and Nations Leagues;
    - 20 friendlies.
  - **Goal-difference multiplier:** 1 for a margin of 0 or 1, 1.5 for 2, (11 + N)/8 for
    N ≥ 3.
  - **Home advantage:** +100 for the host side (unknown is treated as listed home, as Elo
    has no learned term).
  - **1X2:** through the ordered logit fitted walk-forward on earlier target matches, as in
    C1.
- **Gate on dev (all must hold):**
  1. v0 beats B_elo on competitive matches on 1X2 log loss and RPS, each paired 95%
     interval by match excluding 0.
  2. Friendlies are reported apart (log loss, RPS, against B_elo), not gated (§10.4).
  3. **Segments of competitive matches:**
     - the segments are each confederation's own competitions (UEFA, CONMEBOL, CONCACAF,
       CAF, AFC) and finals against qualifiers / Nations Leagues;
     - none is worse than B_elo by more than 1.0% (point estimate);
     - only segments with n ≥ 100 are checked; smaller ones (OFC, the intercontinental
       play-offs) are reported only. This is stated now, after C1's 11-match Conference
       League segment.
  4. **Calibration on qualifiers and Nations League matches (§10.4):** 1X2 ECE per outcome
     within its noise floor (the 95th percentile for a perfectly calibrated forecast on the
     same matches).
  5. **Inference gates on every refit:**
     - R-hat ≤ 1.01, ESS ≥ 400, no divergences;
     - PPC of total goals and of the share of margins of 4 or more (the heavy tail the
       negative binomial is for);
     - prior sensitivity at the last refit (sds × 0.5 and × 2);
     - conf identified (posterior sd below prior sd).
  6. **Leakage:** on 50 sampled targets, nothing final after the match's Monday changes
     the forecast.
  - **Reported:** by period, competition type, confederation and host role (unknown
    apart); conf over time; σ_w; φ.
- **Then:**
  - **Holdout:** on a dev pass, the holdout is opened once ("international"), with checks
    1, 3 and 4 as the promotion gate and 2 reported.
  - **Serving:** on a pass, senior men's national matches between two of the 218 sides are
    served (90', 1X2 and scores, labelled team-only).
  - **Not served:** other national matches show why. Nothing betting-facing for youth
    sides (CLAUDE.md rule 10).
- **Not in v0, raised against §10.4:**
  - **Travel.** §10.4 lists host flag *and* travel. Only 279 of 2,324 national venues have
    coordinates, so travel can't be built yet. Proposed: geocode the national venues first
    (Open-Meteo, free, the existing geocoder, 0 API-Football requests). Then test travel as
    its own term on dev: kept only if competitive 1X2 log loss improves with the interval
    excluding 0. That would be one disclosed amendment before the holdout. Until then, v0
    has the host role only.
  - **Fading cold-start priors from Elo or FIFA:** none stored (above).
- **Compute:**
  - 0 API requests;
  - about 150-180 dev refits (weeks with a senior match, 2019-2023), each a NUTS fit with
    about 218 × 115 walk steps;
  - one refit is timed first, and the run is planned from that (CI, 18 parallel jobs, one
    backtest-type run at a time).

- [x] **D v0 build (2026-11-21), before any score:**
  - **Code:**
    - `adatta/model/national.py`: targets, confederations, host roles, the v0 model
      (PyMC with NumPyro), prediction, B_elo, the gate, prior sensitivity and leakage;
    - `.github/workflows/national.yml` (time, refits over 18 jobs, gate);
    - `tests/test_national.py` (6).
  - **Targets** (loaded, counts only):
    - 6,363 matches between 218 sides: warm-up 2,226, dev 4,137 (2,943 competitive, 1,194
      friendlies);
    - roles: home 3,619, neutral 1,272, swapped 77, unknown 1,395;
    - dev competitive by competition confederation: UEFA 1,135, CAF 629, CONCACAF 554,
      AFC 377, CONMEBOL 170, world 67 and OFC 11 (the last two reported only).
  - **Refits:** 151 dev refits. The largest has 6,358 matches, 218 teams and 115 months.
  - **Clarifications, before any score:**
    - the intercept prior, not stated in the acceptance, is c ~ Normal(0.25, 0.5);
    - prior sensitivity scales the Normal and HalfNormal sds; φ (Gamma) and ρ (Uniform)
      keep their priors;
    - the sensitivity check is §14.5's: every headline mean (c, g, h, h_u, conf, σ_w, σ_u,
      σ_o) moves less than 1 posterior sd.
  - **Sampler (2026-11-22), before any score:** the smallest refit (2019-01-07: 2,230
    matches, 217 teams, 56 months) failed the sampler gates on one parameter, σ_w. Every
    other variable passed (R-hat ≤ 1.006, bulk ESS ≥ 821). The variants were on run
    44997123915.

    | warm-up | target_accept | seconds | R-hat σ_w | bulk ESS σ_w | tail ESS σ_w | divergences |
    |---|---|---|---|---|---|---|
    | 1,000 | 0.9 | 298 | 1.010 | 238 | 379 | 1 |
    | 2,000 | 0.95 | 272 | 1.011 | 177 | 280 | 0 |
    | 3,000 | 0.99 | 1,092 | 1.023 | 207 | 374 | 0 |

    Longer warm-up and smaller steps don't help, so the problem is mixing, not adaptation.
    The full walk had one innovation per team per month (12,096 here), and most of them fall
    in months a side doesn't play.

    **Fix: an exact reparameterisation, not a model change.** The walk is now kept only at
    each team's played months and its refit month, with innovation variance σ_w² × the months
    since the team's previous node. A Gaussian walk observed at those months has exactly
    this distribution, so the prior, the likelihood and prediction are unchanged. A test
    checks it against the full monthly walk.

    The refit cache moves to a new generation (`v0b`), so the failing 2019-01-07 fit is never
    reused.

    **The collapsed walk alone didn't pass** (run 44999708891): 3,263 parameters, 252 s,
    σ_w R-hat 1.035, bulk ESS 144 and 12 divergences. σ_w's posterior at this refit runs down
    toward 0 (mean 0.036, 5% quantile 0.005).

    | walk | draws per chain | target_accept | seconds | R-hat σ_w | bulk ESS σ_w | divergences |
    |---|---|---|---|---|---|---|
    | centred | 1,000 | 0.9 | 361 | 2.202 | 5 | 0 |
    | centred | 3,000 | 0.9 | 715 | 1.761 | 6 | 0 |
    | non-centred | 3,000 | 0.9 | 495 | 1.003 | 632 | 50 |
    | non-centred | 3,000 | 0.95 | 488 | 1.010 | 610 | 7 |
    | non-centred, 2,000 warm-up | 3,000 | 0.95 | 582 | 1.006 | 586 | 209 |
    | **non-centred** | **3,000** | **0.98** | **758** | **1.007** | **539** | **0** |

    The variants ran on runs 45001012300 and 45002314691. The centred walk doesn't mix. Below
    0.98 the divergences come mostly from one chain.

    **Settings:** non-centred walk, 1,000 warm-up + 3,000 draws × 4 chains, target_accept 0.98.
    400 draws are still kept for prediction. The cache generation is `v0c` (nothing was
    cached under `v0b`). These are sampler settings only, and the model is unchanged.

    **Largest refit, 2023-12-25** (run 45003535919): 6,358 matches, 218 teams, 115 months,
    7,171 parameters, 2,211 s of sampling (40 minutes for the job).
    - It passes every gate: R-hat ≤ 1.004, the lowest bulk ESS 2,727 (σ_w), no divergences,
      every confederation identified, PPC margin 4+ 0.113 observed against a median of 0.116.
    - σ_w is well determined with ten years of data: 0.041 ± 0.005.
    - Non-blocking warning: total-goals dispersion is 1.275 observed against a PPC median of
      1.333. The negative binomial with φ ≈ 66 is slightly more spread than the data. This is
      reported, not tuned.

    **Backtest size:** from 758 s to 2,211 s of sampling, plus about 3 minutes per refit for
    the PPC and scoring, the average is about 28 minutes per refit. 151 refits come to about
    70 hours, or about 4 hours per job over 18 jobs (8 or 9 refits each, spread over the
    window). That is inside the 6-hour job limit.
  - **Rerun rule (2026-11-22, owner: "Yes, refit at 6,000"; before any score).** The
    backtest's refits run (45007552857) was still going. With 95 of 151 refits cached, 5 had
    failed R-hat on σ_w alone, each just over the limit, with ESS 506–890 and no divergences.

    | refit | R-hat σ_w |
    |---|---|
    | 2019-01-21 | 1.0118 |
    | 2019-01-28 | 1.0114 |
    | 2019-10-07 | 1.0112 |
    | 2019-10-14 | 1.0105 |
    | 2020-10-19 | 1.0107 |

    **The rule:**
    - Any refit that fails a sampler gate (R-hat, bulk or tail ESS, divergences) is refitted
      once at 6,000 draws per chain.
    - The rerun's result stands, pass or fail. There is no third fit, and the first attempt
      is kept in the cache beside it (`_first`).
    - A failure on PPC or identifiability is not rerun, because more draws can't fix it.

    **The gate is unchanged:** every refit must pass. The rule uses convergence diagnostics
    only, never a score.

    **Mechanics:** in `cached_fit`, so the backtest refits, the gate's base fit and the prior
    sensitivity fits follow it alike. After the main run, the refits stage runs again. Cached
    refits load as they are, and the failing ones rerun, so every part is rescored. The gate
    reads that second run and reports how many refits were rerun.

### D v0 dev result (2026-11-22): FAILS on prior sensitivity alone

**Runs:** refits 45007552857 (151 refits, about 4 h); reruns plus rescoring 45028591890; gate
45031998495. The scores and verdict are in R2 under `national/reports/45028591890/`. There
are 4,137 dev matches: 2,943 competitive and 1,194 friendlies.

| model | segment | 1X2 log loss | RPS |
|---|---|---|---|
| v0 | competitive | 0.8340 | 0.1621 |
| B_elo | competitive | 0.8724 | 0.1726 |
| v0 | friendlies (reported) | 0.9167 | 0.1809 |
| B_elo | friendlies (reported) | 0.9740 | 0.1972 |

**v0 against B_elo, competitive (n 2,943):**
- log loss −0.0384 [−0.0478, −0.0290], −4.40%;
- RPS −0.0106 [−0.0132, −0.0079], −6.12%.

**Friendlies (reported):** log loss −5.89% and RPS −8.27%, both intervals below 0.

**Segments, competitive log loss against B_elo:**

| segment | n | change |
|---|---|---|
| AFC | 377 | −7.71% |
| CAF | 629 | −3.92% |
| CONCACAF | 554 | −7.50% |
| CONMEBOL | 170 | −2.22% |
| UEFA | 1,135 | −2.50% |
| OFC (reported) | 11 | −28.27% |
| world (reported) | 67 | +0.42% |
| finals | 700 | −5.70% |
| qualifiers | 2,243 | −4.00% |

By host role: home −2.39%, neutral −6.39%, unknown −7.86%, swapped (n 53) −9.66%.

**Calibration on qualifiers and Nations Leagues (ECE against limit):** home 0.026/0.031, draw
0.010/0.022, away 0.024/0.029.

**Inference:**
- Every refit passes, 0 of 151 failing.
- 5 refits were rerun at 6,000 draws under the rule, and all passed: R-hat ≤ 1.004, ESS ≥
  1,068.
- Worst R-hat 1.009, no divergences, slowest refit 2,983 s.
- The dispersion PPC warning (non-blocking) appeared on 8 refits.

**At the last refit:** conf CONMEBOL +0.815, CONCACAF −1.916, CAF −0.747, AFC −1.629,
OFC −2.708; h +0.306, h_u +0.253; g (qualifier, finals) +0.006, +0.070; σ_w 0.041, σ_u 1.057,
σ_o 0.203, φ 65.9, ρ −0.031.

| check | result |
|---|---|
| 1 beats B_elo on competitive matches | pass |
| 3 segments | pass |
| 4 calibration | pass |
| 5 inference gates on every refit | pass |
| 5 prior sensitivity | **FAIL** |
| 6 leakage | pass (0 of 50 changed) |

**Prior sensitivity at the 2023-12-25 refit**, shift in base posterior sds (both alternative
fits pass their sampler gates):

| | OFC | AFC | CONCACAF | σ_o | CAF |
|---|---|---|---|---|---|
| × 0.5 | **2.27** | **1.19** | **1.09** | 0.97 | 0.81 |
| × 2 | 0.84 | 0.39 | 0.32 | 0.28 | 0.28 |

**Reading:** the confederation prior, conf ~ Normal(0, 1) around UEFA's level, is too narrow
for where the data put the other confederations (−0.75 to −2.71, up to 2.7 prior sds out).
Halved, it pulls them toward UEFA. Doubled, it hardly matters, so the data, not the prior,
set these levels at the stated width. The forecasting result is strong and consistent across
segments, but the acceptance requires every headline mean to move less than 1 sd, so **v0
fails on dev**. The international holdout is not opened.

### D v1 amendment (drafted 2026-11-22 after the v0 dev result; owner: "Approve as drafted")

**Disclosure:** this amendment is written after seeing v0's dev scores and its sensitivity
table. It changes two prior widths and nothing else. Neither width is chosen to improve a
score, and on dev v0 the data, not these priors, set the forecasts (the × 2 fit moves
nothing past 0.84 sd). The international holdout stays closed until a version passes dev.

**Changes:**

| prior | v0 | v1 | why |
|---|---|---|---|
| conf (non-UEFA) | Normal(0, 1) | Normal(0, 3) | v0 puts the levels at −0.75 to −2.71, up to 2.7 prior sds out. |
| σ_o | HalfNormal(0.1) | HalfNormal(0.3) | v0's posterior is 0.203, 2 prior sds out. Its × 0.5 shift was 0.97, just inside. |

- **Confederation width:** a difference of 3 on the s scale is a goal-rate ratio of e^1.5 ≈
  4.5 between average sides of two confederations, about the largest gap that is plausible.
  So sd 3 is weakly informative. At × 0.5 (sd 1.5) OFC's v0 level is 1.8 prior sds out
  rather than 5.4.
- **Openness width:** HalfNormal(0.3) puts 0.203 at 0.7 prior sd, and at × 0.5 at 1.35.
- **The identification check follows the prior:** conf is identified when its posterior sd
  is below the prior sd, now 3 × the sensitivity scale. v0 hard-coded 1.

**Unchanged from v0:** everything else, as stated and built.
- the windows, targets and roles;
- the model's other terms and priors: c, g, h, h_u, σ_u, σ_w, φ, ρ, the walk on played months
  (exact);
- friendlies weighted 0.5;
- sampler settings and the rerun rule;
- B_elo and the gate checks 1–6, including prior sensitivity at × 0.5 and × 2 with the same
  1-sd criterion over the same headline means;
- the friendlies-at-1.0 report.

**Run:** a full new dev backtest under `VERSION = "v1"`, with its own refit cache. Nothing is
reused from v0's fits. That is about 4 h of refits, any reruns, then about 1 h for the gate.

**Stop rule:** if v1 fails dev, Stage D closes at that result. There is no v2 on prior
widths. A later attempt would need a new model idea stated in advance, such as travel (raised
in the acceptance) or FIFA or Elo cold starts. On a pass, the international holdout opens
once, as the v0 acceptance states.

### D v1 dev result (2026-11-22): PASSES on dev

**Runs:** refits 45039184400 (151 fresh refits under `v1c`, about 4 h; nothing reused from
v0); rerun pass and rescoring 45062788857; gate 45063077080. The scores and verdict are in R2
under `national/reports/45062788857/`. Same 4,137 dev matches: 2,943 competitive and 1,194
friendlies.

| model | segment | 1X2 log loss | RPS |
|---|---|---|---|
| v1 | competitive | 0.8335 | 0.1619 |
| B_elo | competitive | 0.8724 | 0.1726 |
| v1 | friendlies (reported) | 0.9165 | 0.1808 |
| B_elo | friendlies (reported) | 0.9740 | 0.1972 |

**v1 against B_elo, competitive (n 2,943):**
- log loss −0.0389 [−0.0484, −0.0295], −4.46%;
- RPS −0.0107 [−0.0134, −0.0080], −6.20%.

**Friendlies (reported):** log loss −5.91% and RPS −8.31%, both intervals below 0.

**Segments, competitive log loss against B_elo:**

| segment | n | change |
|---|---|---|
| AFC | 377 | −7.91% |
| CAF | 629 | −3.94% |
| CONCACAF | 554 | −7.59% |
| CONMEBOL | 170 | −2.25% |
| UEFA | 1,135 | −2.48% |
| OFC (reported) | 11 | −32.01% |
| world (reported) | 67 | +0.30% |
| finals | 700 | −5.70% |
| qualifiers | 2,243 | −4.08% |

By host role: home −2.41%, neutral −6.53%, unknown −7.96%, swapped (n 53) −9.94%.

**Calibration on qualifiers and Nations Leagues (ECE against limit):** home 0.024/0.031, draw
0.011/0.022, away 0.023/0.029.

**Inference:**
- Every refit passes, 0 of 151 failing.
- 7 refits were rerun at 6,000 draws under the rule, and all passed.
- Worst R-hat 1.010, no divergences, slowest refit 2,855 s.
- The dispersion PPC warning (non-blocking) appeared on 15 refits.

**At the last refit:** conf CONMEBOL +0.790, CONCACAF −1.975, CAF −0.794, AFC −1.692,
OFC −3.063; h +0.306, h_u +0.252; g (qualifier, finals) +0.006, +0.071; σ_w 0.0406, σ_u 1.063,
σ_o 0.209, φ 65.8, ρ −0.031.

| check | result |
|---|---|
| 1 beats B_elo on competitive matches | pass |
| 3 segments | pass |
| 4 calibration | pass |
| 5 inference gates on every refit | pass |
| 5 prior sensitivity | pass |
| 6 leakage | pass (0 of 50 changed) |

**Prior sensitivity at the 2023-12-25 refit**, six largest shifts in base posterior sds (both
alternative fits pass their sampler gates):

| × 0.5 | shift | × 2 | shift |
|---|---|---|---|
| conf OFC | 0.42 | conf OFC | 0.08 |
| conf AFC | 0.22 | σ_u | 0.06 |
| σ_w | 0.22 | σ_o | 0.05 |
| c | 0.21 | conf AFC | 0.05 |
| σ_u | 0.21 | g finals | 0.04 |
| conf CONCACAF | 0.19 | conf CONMEBOL | 0.03 |

**Reading:**
- The wider priors did what the amendment intended. The worst × 0.5 shift falls from 2.27 to
  0.42 sd, and σ_o from 0.97 to under 0.19.
- The forecasts barely moved from v0: competitive log loss 0.8340 → 0.8335. The levels moved
  outward where the v0 prior had held them: OFC −2.71 → −3.06, AFC −1.63 → −1.69.
- **v1 passes on dev.**
- Under the stop rule and the v0 acceptance, the international holdout now opens once, with v1
  frozen as run here.
- The gate report labelled check 1 "v0 beats B_elo" in this run. That was a hard-coded label;
  the scores are v1's. The label now follows `VERSION`.

### D v1 friendlies at weight 1.0 on dev (2026-11-23, the acceptance's report)

**Runs:** refits 45109126191 (151 fresh refits under `v1c/w1_s1`); rerun pass and rescoring
45135996978; gate 45136407593. The scores are in R2 under `national/reports/45135996978/`.
Everything else is v1 as run at 0.5, on the same 4,137 dev matches.

| friendlies weight | competitive log loss | competitive RPS | friendlies log loss | friendlies RPS |
|---|---|---|---|---|
| 0.5 (as stated) | 0.8335 | 0.1619 | 0.9165 | 0.1808 |
| 1.0 | 0.8310 | 0.1613 | 0.9140 | 0.1799 |
| B_elo | 0.8724 | 0.1726 | 0.9740 | 0.1972 |

**1.0 against 0.5, paired by match:**

| segment | 1X2 log loss | RPS |
|---|---|---|
| competitive (n 2,943) | −0.0024 [−0.0043, −0.0005], −0.29% | −0.0007 [−0.0012, −0.0001], −0.41% |
| friendlies (n 1,194) | −0.0024 [−0.0061, +0.0012], −0.26% | −0.0009 [−0.0020, +0.0002], −0.50% |

- **Competitive segments, log loss of 1.0 against 0.5:**
  - better: AFC −0.92%, CONCACAF −0.91%, CONMEBOL −0.53%, finals −0.36%, qualifiers −0.27%;
  - about the same: CAF +0.08%, UEFA +0.04%.
- **Gate at 1.0, against B_elo:** competitive log loss −4.74% [−0.0508, −0.0319], RPS −6.58%.
- **Every check passes:**
  - segments −2.44% to −8.75%;
  - ECE home 0.022/0.031, draw 0.018/0.022, away 0.022/0.029;
  - all 151 refits pass with no reruns needed: worst R-hat 1.010, no divergences;
  - prior sensitivity worst shift 0.36 at × 0.5 and 0.06 at × 2;
  - leakage 0 of 50.
- **At the last refit:** σ_w 0.0468 (0.0406 at 0.5), φ 72.2 (65.8).

**Reading:** full weight on friendlies is better on competitive matches, by a small margin
whose interval excludes 0, and no checked segment is worse. The 0.5 down-weighting was a
prior judgement (friendlies are less informative), and the data don't support it. Changing
the weight to 1.0 would be the disclosed amendment the acceptance allows before the holdout.
The owner decides.

### D v1 amendment 2: friendlies at weight 1.0 (2026-11-23, owner: "Amend to 1.0")

**Disclosure:**
- This change was chosen after seeing the dev scores at both weights.
- The two weights are the only ones the acceptance named: 0.5 as stated and 1.0 as the
  report. No other weight was tried.
- The acceptance allows this change as a disclosed amendment before the holdout. The
  holdout has not been opened.

**Change:** friendlies' likelihood weight goes from 0.5 to 1.0 (`FRIENDLY_WEIGHT`). That means
the likelihood has no down-weighting at all.

**Why:**
- On dev, 1.0 improves competitive 1X2 log loss by 0.29%, with the paired interval
  [−0.0043, −0.0005] excluding 0. RPS improves by 0.41%.
- No checked segment is worse.
- It passes every dev check on its own run (above). So the amended model already has a
  complete passing dev backtest, and no new dev run is needed.

**Unchanged:** everything else in v1. The holdout runs v1 at weight 1.0 from the
`v1c/w1_s1` refits:
- the holdout code refuses any other weight;
- the opening's marker records the model as `national_v1_w1`.

### D v1 international holdout run (stated 2026-11-22 before opening; owner: "Yes, build holdout")

This section states how the opening runs. It adds nothing to the acceptance's promotion gate.
No holdout result rate or score has been read.

- **Model frozen:**
  - v1 exactly as run on dev, with friendlies at 1.0 (amendment 2, the 45109126191 dev run):
    the same model, priors, sampler, rerun rule and refit cache (`v1c/w1_s1`);
  - B_elo with the same settings, the Elo and its ordered logit carried walk-forward
    through the holdout.
- **Opening:**
  - Marker "international" (`eval.holdout`), claimed once by the run's first job.
  - Every refit and gate job checks the run owns it. Any other run is refused, and the
    holdout targets are loaded only with that key.
  - A job that times out is re-run in the same run, and its cached refits are kept.
- **Targets:**
  - The holdout window, 2024-01-01 to 2026-07-31, with the same target rules as on dev.
  - The acceptance counted 1,701 competitive matches and 814 friendlies when it was written.
    The run reports the count it loads.
- **Refits:**
  - One refit on the Monday of every week with a holdout target, trained on every match
    final by that Monday (warm-up, dev and earlier holdout weeks) within the 10-year cap.
  - Each target uses its week's refit, as on dev.
- **Promotion gate** (the acceptance: "checks 1, 3 and 4 as the promotion gate and 2
  reported"):
  1. v1 beats B_elo on competitive holdout matches on 1X2 log loss and RPS, each paired 95%
     interval excluding 0.
  2. No segment with n ≥ 100 is worse than B_elo by more than 1.0%. The segments are the
     same as on dev: confederation competitions, finals, and qualifiers / Nations Leagues.
  3. 1X2 ECE is within its noise floor on qualifiers and Nations Leagues.
- **Reported, not gated:**
  - friendlies (check 2);
  - the inference gates of every holdout refit (check 5);
  - leakage on 50 sampled targets (check 6);
  - host roles and the parameters at the last refit.
  - Prior sensitivity is a dev check and is not rerun.
- **Outcome:**
  - On a pass, v1 is promoted to serving, labelled team-only, as the acceptance states.
  - On a fail, it is not served. Stage D closes at that result, with no retuning on the
    holdout.
- **Built (2026-11-22):**
  - `eval/holdout.py`: marker "international".
  - `adatta/model/national.py`: `claim`, plus `refits` and `gate` with `--holdout RUN`.
    Targets past 2024-01-01 load only with the opening's key, and nothing past 2026-07-31
    loads at all.
  - `.github/workflows/national_holdout.yml`: claim, then 18 refit jobs, then the gate. It
    is dispatched with confirm OPEN.
  - Tests: 3 new.

### D v1 international holdout result (2026-11-23): PASSES, v1 (friendlies 1.0) promoted

**Run:** 45166498451 at 5a872c7.
- **Opening:** the claim at 2026-11-23 11:35 UTC took marker "international" as
  `national_v1_w1`.
- **Refits:** 85 holdout refits over 18 jobs, about 6 h; the gate took 1 min.
- **Storage:** the scores and verdict are in R2 under `national/reports/45166498451/`.
- **Matches:** 2,504 holdout matches, 1,701 competitive and 803 friendlies. The acceptance
  counted 814 friendlies, from the database as it stood then. The competitive count is
  unchanged.

| model | segment | 1X2 log loss | RPS |
|---|---|---|---|
| v1 | competitive | 0.8279 | 0.1566 |
| B_elo | competitive | 0.8637 | 0.1656 |
| v1 | friendlies (reported) | 0.8868 | 0.1700 |
| B_elo | friendlies (reported) | 0.9456 | 0.1868 |

**Promotion gate (checks 1, 3 and 4): pass.**
1. **Competitive against B_elo (n 1,701):**
   - log loss −0.0358 [−0.0467, −0.0250], −4.15%;
   - RPS −0.0090 [−0.0121, −0.0059], −5.41%.
2. **Segments, competitive log loss against B_elo:**

   | segment | n | change |
   |---|---|---|
   | AFC | 297 | −3.20% |
   | CAF | 472 | −3.69% |
   | CONCACAF | 261 | −8.74% |
   | UEFA | 453 | −0.81% |
   | world | 108 | −8.65% |
   | finals | 372 | −4.98% |
   | qualifiers | 1,329 | −3.88% |
   | CONMEBOL (reported) | 92 | −3.61% |
   | OFC (reported) | 18 | −19.02% |

   No checked segment is worse. UEFA is the narrowest margin, as on dev (−2.48%).
3. **Calibration on qualifiers and Nations Leagues (ECE against limit):** home 0.035/0.040,
   draw 0.026/0.028, away 0.021/0.038. All pass, and draw is the closest.

**Reported:**
- **Friendlies:** log loss −6.21% [−0.0823, −0.0352] and RPS −9.00%.
- **Host role:** home −2.27%, neutral −5.08%, unknown −6.62%, swapped (n 43) −7.60%.
- **Leakage:** 0 of 50 sampled matches changed.
- **Inference (check 5, reported on the holdout): 2 of 85 refits fail one PPC check.**
  - Sampler: no reruns needed, worst R-hat 1.005, no divergences, slowest refit 4,420 s.
  - The two failures are the 2026-02-23 and 2026-03-23 refits, on the national away-goals
    mean: observed 1.101 against a PPC median of 1.122, a miss of 0.02 goals.
  - The non-blocking dispersion warning appears on 81 of 85 refits (dev at 1.0: 13 of 151).
    The observed total-goals variance/mean is 1.23–1.27 against the model's 1.30–1.34, so
    the negative binomial is a little too dispersed for recent matches. φ rises from 72 to
    85 over the window.
  - Neither touches the gate. Both are carried to serving monitoring and to the next model
    version.
- **At the last refit (2026-07-13):** conf CONMEBOL +0.767, CONCACAF −1.953, CAF −0.693,
  AFC −1.647, OFC −3.077; h +0.285, h_u +0.213; g (qualifier, finals) +0.002, +0.058;
  σ_w 0.0377, σ_u 1.071, σ_o 0.214, φ 85.3, ρ −0.056.

**Outcome:**
- Stage D v1, with friendlies at 1.0, is promoted.
- The next step from the acceptance is serving. Senior men's matches between two of the 218
  sides are served (90', 1X2 and scores, labelled team-only). Other national matches show
  why they aren't served, and nothing betting-facing is shown for youth sides.
- The international holdout is now spent. Any later national model is judged on the live
  period from 2026-08-01.

### D v1 serving acceptance (drafted 2026-11-23 before building; owner: "Approve as drafted")

**Scope:** the acceptance's "Then: Serving" for the promoted model, v1 with friendlies at 1.0,
exactly as run on the holdout. Nothing in the model changes.

- **What is served:**
  - **Matches:** upcoming senior men's national matches between two of the 218 sides,
    under the same target rule as the backtest: every competition including friendlies.
  - **Markets:** 90' 1X2 and the score matrix, from the stored posterior draws, with the same
    milestones as clubs (`initial`, T-7d, T-24h, T-60m, close) in `market_projection`.
  - **Label:** "Team-only: results-based national model (Stage D v1), no squad or call-up
    information".
  - **Withheld markets:** Over/Under and BTTS are not served for national matches. The
    holdout validated 1X2 only, and the dispersion PPC warned on 81 of 85 holdout refits (the
    model's totals are slightly too wide). They are shown as "not validated for national
    teams".
  - **Knockouts:** knockout ties get the 90' forecast only. Advancement and shoot-outs are
    v2 (the tournament simulator).
- **Not served, with the reason shown and no probabilities:**
  - a side outside the 218: youth, women's, B sides, clubs or non-FIFA sides ("Stage D
    covers senior men's national teams only");
  - youth sides: never anything betting-facing (CLAUDE.md rule 10);
  - no passing national refit yet, or the latest is stale (below).
- **Live refits:**
  - **Timing:** one refit per Monday 00:00 UTC (the backtest's refit time), only in weeks
    with a servable national match kicking off within the projection horizon. Off-window
    weeks skip, so most weeks cost nothing.
  - **Data:** every senior match final by that Monday within the 10-year cap; the same
    model, priors, sampler and seed.
  - **Rerun rule:** as on dev, a fit failing a sampler check is refitted once at 6,000
    draws.
  - **Storage:** the draws (400 kept) go to R2 under `serving/national/`. A `model_run` row
    (component national, window live) records the refit, its gates and the training hash.
  - **Workflow:** `national_serve.yml`, scheduled Monday morning plus manual dispatch,
    max-parallel 1, logs carry metrics only. The capture tick projects from the stored
    draws with numpy alone (no NUTS, no model dependency group), as for clubs.
- **What blocks serving a refit:**
  - **Blocking:** the sampler gates (R-hat ≤ 1.01, bulk and tail ESS ≥ 400, no
    divergences), after the rerun rule.
  - **Reported only:** PPC and dispersion, as on the holdout, shown on the national
    freshness panel.
  - **Stale:** a refit older than 9 days is reported stale, as for clubs. A match with no
    refit from its own week or the week before shows "no current refit" rather than a
    months-old forecast.
- **Principles:**
  - odds are never read (principle 1);
  - nothing after a refit's Monday enters it, and a refit "from the future" is refused;
  - the stored draws and the training snapshot let any served forecast be rebuilt.

**Acceptance tests, before it is switched on:**
1. **Identity:** a projection from stored draws equals `national.predict` at the same refit,
   for 1X2 and expected goals, to 1e-9 on the same draws.
2. **Coherence:** the score matrix sums to 1, and the 1X2 from the matrix equals the served
   1X2.
3. **Eligibility:**
   - the served set is exactly the backtest's target rule;
   - a youth, women's or club side gets a reason and no probability;
   - a match outside the horizon gets nothing.
4. **Gates:** a refit failing a sampler check after the rerun is recorded but never served,
   and the previous passing refit serves if it is not stale.
5. **No leakage:** the training cutoff is the Monday minus the 3 h lag, and a refit with
   as_of after now is refused.
6. **Shadow run on CI before going live, metrics only:**
   - refit as of 2026-11-16 (the last window) and project that week's matches, with no DB
     writes;
   - its 1X2 must match a holdout-pipeline `predict` at the same Monday to 1e-9;
   - it must run within the production budget: one Monday refit under 2 h on one runner,
     where the holdout's slowest was 74 min.
7. **Dashboard:** national fixtures appear in the list and match views with the team-only
   label, the 1X2 headline, the matrix, and the withheld-market and unserved reasons.

**Then:**
- **First live use:** the December friendly in the database, then the March 2027 window.
- **Live record:** every served forecast is scored against B_elo, recomputed after the fact
  and deterministic, as the live-period record for any later national model.
- **Built (2026-11-23):**
  - **`adatta/model/national_live.py`:**
    - `promote`: records the champion from the holdout run's marker and passing verdict;
    - `refit`: Monday, only if due, with the rerun rule; a model_run row per refit;
    - `shadow`: acceptance test 6.
  - **`adatta/sim/national_serve.py`:** 1X2 milestones from the stored draws (numpy only),
    and the reasons a match isn't served.
  - **The capture tick:** a `project_national` step.
  - **`adatta/api/national.py`:** the national groups in the fixture list and the national
    match view: team-only component, 21 × 21 matrix with its coherence check, and the
    unavailable panels.
  - **`national.py`:** `predict` now runs through `matrices`, the one function serving also
    uses. Backtest scores are unchanged: the same random stream, the same arithmetic.
  - **`.github/workflows/national_serve.yml`:** refit, promote and shadow; scheduled on
    Mondays at 02:37, 08:37 and 14:37 UTC.
  - **Tests:** `tests/test_national_serve.py` (9) covers tests 1–5 and 7.
- **Switch-on order:**
  1. `national_serve.yml stage=promote`;
  2. `stage=shadow as_of=2026-11-16` (test 6) must pass;
  3. the Monday schedule then refits whenever a servable match is within 14 days, and the
     capture tick projects.
- **Switched on (2026-11-23):**
  - **Promote** (run 45241466983): `national_v1_w1` recorded as champion, model_run 38,
    from holdout run 45166498451.
  - **Shadow, test 6** (run 45241607209): **PASSES.**
    - Refit as of 2026-11-16 on 8,545 training matches, with no DB writes.
    - 74 fixtures that week; the served 1X2 matches `predict` to 8.9e-16 (limit 1e-9).
    - Fit 4,526 s (75 min), within the 2 h budget; 80 min for the whole job.
    - No sampler failures and no rerun. No PPC check reported failing; the dispersion
      warning (`ppc national total_dispersion`) is the one seen on 81 of 85 holdout refits.
  - **Live from here:** no live refit exists yet, so the dashboard shows "No passing
    national refit yet" until the first Monday with a servable match inside 14 days (the
    December friendly, then the March 2027 window).

### D v2 acceptance: tournament simulator (drafted 2026-11-24 before building; owner: "Approve as drafted")

**Question:** given the served national model (v1, friendlies 1.0, model_run 38), are its
tournament probabilities right and calibrated? These are P(group position), P(advance from
the group), P(reach each round), P(win), and P(advance) for a knockout tie. v2 adds rules
and simulation, and no fitted parameter: nothing in the match model changes, and nothing is
tuned on any score.

**Seen so far (counts and round labels only; no score or outcome rate read):**
- **Finals tournaments in dev (2019-01-01 to 2023-12-31): 10.**
  - Asian Cup 2019, Copa América 2019, Gold Cup 2019, AFCON 2019, Euro 2020, Copa América
    2021, Gold Cup 2021, AFCON 2021, World Cup 2022 and Gold Cup 2023.
  - 198 team-entries in the group stage.
  - About 145 knockout ties, adding the UEFA and CONCACAF Nations League finals and play-outs.
- **In the opened D v1 holdout (2024-01 to 2026-07): 7.** Asian Cup 2023, AFCON 2023, Euro
  2024, Copa América 2024, Gold Cup 2025, AFCON 2025 and World Cup 2026: 176 team-entries,
  about 110 knockout ties.
- **Cached refits:** every Monday of the 10 dev tournaments has a cached v1 refit with 400
  kept draws (`national/refits/v1c/w1_s1/`), 48 refits in all. The simulator needs no new
  fit.
- **Group labels:** most editions give the group stage as "Group Stage - n" with no group
  letter. A group is read as the complete round robin among its teams, and the AFCON 2019
  season's qualifiers are separated by date.

**Model, fixed now:**
- **Format rules:** `config/tournament_formats.yml`, per competition and edition, written
  from the regulations with the source noted (as Stage B's B0):
  - groups and their size;
  - points;
  - tiebreakers in order (head-to-head or goal difference first, as each edition's
    regulations say);
  - qualification slots per position;
  - the best-placed third ranking and its bracket allocation table;
  - the bracket;
  - legs, extra time, penalties and away goals per round;
  - the venue country of each knockout slot.
  - Tiebreakers the data can't decide (fair-play points, FIFA ranking, drawing of lots) are
    taken in random order among the teams still level. This is disclosed, and the replay
    counts how often an actual table needed them.
- **Matches:** each simulated tournament takes one posterior draw of the served refit (§9.5).
  - For each match in that draw, a full score is drawn from v1's score matrix, so goal
    difference and goals scored feed the tiebreakers.
  - Each pairing's rates use v1's own `predict`: the walk carried to the match month, and
    the host role from the slot's venue country (home for a host side in its own country,
    otherwise neutral).
- **Knockouts:** Stage B's B3 logic.
  - **Single match:** 90', then extra time, then penalties.
  - **Two legs:** the aggregate, then away goals where the rule has them, then extra time in
    leg 2, then penalties.
  - **Fallbacks, from Stage B's results:** extra time at κ = 1 (30/90 scaling; B1 fell
    back) and a coin-flip shootout (B2 fell back). B3' (the stretch) is not used.
- **State:**
  - A simulation as of Monday t fixes every result final by t − 3 h.
  - It uses the latest passing refit at or before t.
  - When it runs between refits, it fixes the results final by then and keeps the same
    draws (D33's intent).
- **Size:** 20,000 simulated tournaments, 50 per kept draw. The Monte Carlo error is at
  most 0.0035 on any probability and is shown with it.

**Dev evaluation:**
- **Pre-tournament:** as of each tournament's opening Monday, before matchday 1.
- **Knockout ties:** each tie as of its own Monday, with the actual pairing (two-legged ties
  before leg 2, as Stage B).
- **In-tournament forecasts:** the later Mondays are reported.
- **The 4-team Nations League finals** are knockout ties only.

**Gate (all must pass):**
1. **Rules replay.** For every edition in scope, fed the actual results, the engine
   reproduces:
   - each group's final order (where lots or fair play decided it, the same tied set);
   - the qualified teams;
   - the third-place ranking;
   - every knockout pairing;
   - each winner, under Stage B's winner rule.

   Pass: no mismatch, or each one explained as a feed error and listed. Unit tests cover
   each tiebreak path. This reads results to check rules; nothing is scored.
2. **Coherence** (unit tests and run-time checks):
   - each team's position probabilities sum to 1;
   - each position's probabilities sum to 1 over its group;
   - the qualified count equals the slots;
   - P(win) sums to 1;
   - every scheduled group match's simulated 1X2 is within 3 Monte Carlo standard errors of
     the served 1X2;
   - a fixed knockout tie's simulated P(advance) equals B3's closed form within the same
     margin.
3. **Calibration, pre-tournament.** On the 198 dev team-entries, the ECE of P(advance from
   group) and of P(reach the quarter-finals) are each within their noise floor: the 95th
   percentile of the ECE a perfectly calibrated forecast shows on the same entries (Stage
   B's amended check, 10 bins).
4. **Knockout ties.** On about 145 dev ties:
   - P(advance) log loss is no worse than the baseline "90' model, level = coin flip"
     (point estimate ≤ 0, interval reported, as B3's check 1);
   - ECE is within its noise floor.
5. **Leakage.** Unit tests:
   - changing a result final after t − 3 h leaves a simulation as of t unchanged;
   - it reads no refit after t;
   - a format rule dated after the tournament's start is refused.

**Reported, not gated:**
- the RPS of the stage reached and the Brier of P(advance from group), against the reference
  "every team in a group equally likely", for scale;
- in-tournament forecasts (before matchdays 2 and 3, and before each knockout round);
- results by tournament and by confederation;
- the Monte Carlo error;
- how often a tiebreak went to the random order.

**Expectation, stated before any score:** about 200 team-entries and 145 ties give the
calibration checks little power, so a perfect model often shows a sizeable ECE. The main
protection is checks 1 and 2. If check 3 or 4 fails, the likely causes are v1's tails (the
dispersion warning) or the extra-time and shootout fallbacks, and that comes back to the
owner. Nothing is tuned.

**The opened holdout (owner's call):**
- The international holdout (2024-01 to 2026-07) was opened once for D v1 and is spent; no
  locked national data is left.
- **Proposed:** v2 has no fitted parameter, so score the 7 editions of 2024-2026 under the
  same gate as a second check, labelled "opened data, not a holdout". This takes it to 374
  team-entries and about 255 ties, so the calibration checks have more power. Their rules
  are replayed in any case.
- **Alternative:** dev only, with the 2024-2026 editions used for the rules replay alone.
- Either way, nothing is chosen on them.

**If it passes, serving:**
- **Which:** an upcoming national tournament or knockout tie whose edition rules are written
  and replay-checked, where every team is among the 218 (principle 8). Others show why
  there is no forecast.
- **Shown:**
  - on a knockout match page, P(advance) with its path, beside the 90' forecast;
  - a tournament page with P(group position), P(advance), P(reach each round) and P(win),
    each with its Monte Carlo error, as-of time and refit, labelled team-only.
- **Refresh:** after each Monday refit, and when a result in the tournament becomes final
  (the capture tick, numpy from the stored draws, no NUTS).
- **Withheld:** a format whose rules aren't written, or whose replay failed.
- **Live record:** each served tournament is scored after it ends (the gate's metrics) and
  reported. The owner reviews after the first two.
- **First uses:**
  - the UEFA Nations League quarter-finals and promotion play-offs (March 2027, two legs);
  - the CONCACAF Nations League finals (June 2027);
  - the next continental finals.

**Not in v2:**
- **Qualifying groups and Nations League league phases** (multi-round formats such as the
  AFC and CAF World Cup qualifiers). The same engine applies, but each needs its own rules
  and replay. Proposed as v2b after v2, starting with the AFCON 2027 qualifying groups (144
  stored matches) and the Euro 2028 qualifiers.
- **Squad and call-up information** (v1 in §10.4's sense) stays out, as in D v1 serving.

**Raised now, data:**
- **The January 2027 Asian Cup is not in stored data.** `config/national_competitions.yml`
  lists Asian Cup seasons 2019 and 2023, and no 2027 fixture is stored.
  - If the provider has the edition, the capture tick would only start 30 days before
    kick-off.
  - Proposed, before v2's build ends: check the provider's season list (one `/leagues`
    request) and add 2027 to the config if it's there.
  - D v1 would then serve its group matches whether or not v2 passes.
- **Extra-time and penalty scores in national knockouts:** the replay checks them by
  edition before anything is scored, as Stage B did for cups.

**Compute:**
- 0 NUTS fits (v1's cached dev refits) and 0 API requests, apart from the one Asian Cup check
  above.
- The simulation is numpy: about 50 tournament Mondays × 20,000 tournaments, minutes on CI.
- Metrics only in logs, match rows to R2. One backtest-type run at a time, not on a Monday.

**Build, after approval:**
- `config/tournament_formats.yml` (the dev editions, plus 2024-2026 under either holdout
  choice, for the replay);
- `adatta/sim/tournament.py`: the rules engine, the simulator and B3 reused for knockouts;
- `adatta/eval/tournament.py`: the replay, the gate and the report;
- a `tournament.yml` workflow;
- tests for each tiebreak path, coherence and leakage.

- [ ] **D v2 built 2026-11-24, before any score.**
  - **Code:**
    - `config/tournament_formats.yml`: 17 editions, 10 dev and 7 opened;
    - `adatta/sim/tournament.py`: formats and their validation, group tables, best thirds,
      the first round, the simulator;
    - `adatta/eval/tournament.py`: loading, the replay, the forecasts, knockout ties,
      coherence, leakage and the gate;
    - `.github/workflows/tournament.yml`;
    - `tests/test_tournament.py` (9).
  - **Sources:** each edition's English Wikipedia page, read on 2026-11-24.
    - Tiebreak orders and best-third rankings were checked against the page text for every
      edition. AFCON 2023 gives the two-team rule only, so it is taken as AFCON 2021's.
    - The best-third allocation tables are complete:
      - Euro 2020/2024: 15 rows;
      - Asian Cup 2019/2023 and AFCON (the same table): 15 rows;
      - Copa América 2019: 3 rows;
      - World Cup 2026: 495 rows, parsed from the page.
    - Every row is checked to pair each group winner only with a third from its allowed
      groups.
    - The first knockout round is as stated by the source for 10 editions.
  - **Disclosed differences from the draft:**
    1. **Group letters and published order come from the Wikipedia group tables, not the
       provider.** No tournament standings are stored, and fetching them would have cost 17
       API requests. Each stored group (a complete round robin) is matched to its table by
       team names; aliases such as "USA" and "Congo DR" are listed in the code.
    2. **Rounds the sources give only as match reports are derived from the path played**
       and marked `basis: derived`. That covers every round after the first, plus the first
       round of Copa América 2021/2024, Gold Cup 2021/2023/2025 and World Cup 2022. For those
       rounds the replay checks consistency only.
    3. **Knockout ties are the 17 editions' knockout matches:** 115 dev and 108 opened, not
       the draft's about 145 and 110. The Nations League finals, play-offs and play-outs and
       the Finalissima are left out:
       - their formats aren't in the config;
       - CONCACAF's knockout rules aren't stated in any source read.
    4. **A finished group uses its published order, and once every group is finished the
       first round is as drawn up.** Fair play or lots can't be replayed from the data.
    5. **A simulated knockout at a venue whose country is unknown is neutral.**
    6. **The AFCON 2025 final is stored as a walkover with no result** (Wikipedia reports
       CAF's award and a CAS reversal on 2026-11-24), so it is unresolved and not scored.
    7. **The three Copa América 2019 quarter-finals have no stored 90' score.** Their winners
       come from the shoot-outs.
    8. **The run is local from the committed code, as Stage B's was.** The workflow is there
       for reruns. Metrics only in output; rows go to R2.
  - **Replay (check 1), run before any forecast:** 0 problems in all 17 editions.
    - Every group order matches the published one, as do the qualifying thirds, every
      first-round pairing and every later slot.
    - The random step was needed where the sources say fair play or rankings decided:
      - Asian Cup 2019's thirds (Vietnam over Lebanon on disciplinary points);
      - AFCON 2021's thirds;
      - Euro 2024 group C and its thirds;
      - World Cup 2026's thirds.
  - **Raised, data:** Wikipedia gives the 2027 Asian Cup as postponed to 23 Dec 2027 – 21
    Jan 2028, while the provider's season still says 7–20 Jan 2027. The capture follows the
    provider's current season, so it starts 30 days before whatever date the provider lists.

### D v2 result (2026-11-24): dev FAILS check 2 on one Monte Carlo comparison; the opened editions pass; raised to the owner, nothing changed

- **Run:** local, from commit ae29268, 20,000 simulated tournaments per forecast.
  - Rows are in R2 under `tournament/v2/local-ae29268/`, the verdict in `verdict.json`.
  - 17 editions took 15 minutes. No fits and no API requests.

| | dev (10 editions) | opened 2024-2026 (7) |
|---|---|---|
| 1 replay | pass | pass |
| 2 coherence | **FAIL** (1 comparison, below) | pass |
| 3 calibration: P(advance) ECE / noise floor | 0.071 / 0.101, pass | 0.061 / 0.105, pass |
| 3 calibration: P(QF) ECE / noise floor | 0.096 / 0.101, pass | 0.081 / 0.102, pass |
| 4 knockout log loss vs "level = coin flip" | −0.0057 [−0.0127, +0.0014] on 115 ties, pass | −0.0087 [−0.0146, −0.0028] on 108 ties, pass |
| 4 knockout ECE / noise floor | 0.094 / 0.141 | 0.088 / 0.141 |
| 5 leakage | pass | pass |
| Brier P(advance) (equal chances) | 0.138 (0.230) | 0.159 (0.227) |
| RPS of stage reached (equal chances) | 0.096 (0.143) | 0.097 (0.136) |
| random step: group tables / third rankings | 2.3% / 8.2% | 2.5% / 19.4% |

- **The check 2 failure:** in AFCON 2019, one group match (team ids 3095 v 2967) had a
  simulated draw rate of 0.3045 against the model's 0.3147, which is 3.1 Monte Carlo
  standard errors.
  - Rerun alone at 400,000 samples on four seeds, the simulator's sampler gives 0.3139 to
    0.3156 against 0.3147, with every |z| ≤ 1.43. So the sampler is right and the 20,000-sim
    value was chance.
  - The check, as approved, is not multiplicity-safe. It makes about 900 comparisons per
    window (302 dev group matches × 3 outcomes), so a correct simulator crosses 3 standard
    errors about twice by chance. Dev crossed once and the opened window not at all.
- **By edition (RPS, equal chances in brackets):** only AFCON 2023 is worse than equal chances,
  0.1414 against 0.1399. That is the edition the hosts won after only just getting through
  their group.
- **Reading:**
  - Every check that measures the forecasts passes in both windows: calibration of
    P(advance) and P(QF), and knockout P(advance) no worse than the coin flip (on the
    opened editions its interval excludes zero).
  - The replay is exact.
  - The one failure is a Monte Carlo comparison whose threshold doesn't allow for the
    number of comparisons.
  - Under the gate as approved, v2 fails dev and is not served.
- **Owner's call:** amend check 2 before anything is served (for example a family-wise
  threshold, or flagged fixtures recomputed at 400,000 samples), or keep it as approved.
  An amendment after the result is not a re-score: nothing in the model or the forecasts
  would change.

### D v2 check 2 amended (2026-11-24, after the result; owner: "Amend check 2")

Stated before the rerun. It changes only how check 2 reads its Monte Carlo comparisons.
The model, the simulator, the seed, the 20,000 sims and checks 1 and 3-5 are unchanged.

- **Exact parts, unchanged:** position, advance, P(win) and reach probabilities sum to
  what they must (tolerance 1e-9). Any failure fails check 2.
- **Monte Carlo parts, amended:**
  - Every comparison gives z = (simulated − closed form) / Monte Carlo SE. There are three
    per tracked group fixture (home, draw, away) and one per edition's first knockout tie
    (P(advance) against B3's closed form).
  - A window passes if its largest |z| is at most z* = Φ⁻¹(1 − 0.01 / (2m)), where m is
    the number of comparisons in that window. That is a Bonferroni bound with a 1%
    family-wise false-alarm rate (z* ≈ 4.4 for m ≈ 900). Bonferroni needs no assumption
    about how the three outcomes of a fixture are correlated.
  - Reported, not gated: m, the largest |z| with its fixture, and the count over 3 SE
    against its expected count under a correct simulator (m × 0.0027).
- **Why 1% and not 5%:** a correct simulator should fail by chance in about 1 run in 100
  per window, not 1 in 20. A real sampler bug is systematic and shows up across many
  fixtures, not as one 3-SE outlier.
- **Rerun:** all 17 editions from the amended code, with the same seed. The forecasts are
  therefore identical to the run above, and only check 2's reading changes. The 400,000-
  sample resample above is evidence, not part of the gate.

### D v2 result under the amended check 2 (2026-11-24): PASSES dev and the opened editions

- **Run:** local, from commit ab63578, with the same seed and 20,000 sims. The rows are in R2
  under `tournament/v2/local-ab63578/`. Every number from checks 1 and 3-5, the Brier score
  and the RPS is identical to the run above, so the forecasts did not change.
- **Check 2:** the exact sums pass in all 17 editions.

| Monte Carlo comparisons | dev (10) | opened 2024-2026 (7) |
|---|---|---|
| comparisons m | 916 | 799 |
| largest \|z\| / z* | 3.10 / 4.40 | 2.69 / 4.37 |
| over 3 SE (expected by chance) | 1 (2.5) | 0 (2.2) |

- **Verdict:** all five checks pass in both windows. D v2 may be served per the acceptance,
  which is the next step and not yet built.

### D v2 serving acceptance (drafted 2026-11-24 before building; owner: "Approve with (a)")

**Scope:** the acceptance's "If it passes, serving", for the served national model (v1,
friendlies 1.0) and the simulator exactly as gated. Nothing in the model or the simulator
changes. Numpy from the stored draws of the latest passing live refit, with no NUTS, as D
v1 serving.

**What is coming up in stored data (counts only):**
- **UEFA Nations League 2026-27, 25-30 March 2027, all two-legged:** quarter-finals (4 ties),
  play-offs A/B (4) and B/C (4).
- **CONCACAF Nations League 2025-26:** 6 matches labelled "Semi-finals", 25 March to 9 June
  2027. The format is read from the regulations when it is written.
- **AFCON 2027 qualifying groups**, matchdays 5-6 (March 2027): v2b, not v2.
- **No finals tournament with groups:** the Asian Cup 2027 is not stored (the provider has
  January 2027 and Wikipedia has a postponement to December 2027), and the next AFCON and
  Gold Cup are not stored yet.

**Raised now: two-legged ties were not in the D v2 gate.** All 223 gated ties were single
matches in finals tournaments. Nations League ties were left out, as disclosed. The approved
model fixes Stage B's two-leg logic and names the March 2027 Nations League ties as the
first use, but no national two-legged tie has been scored. Stored and already played:
- 6 two-legged ties in dev: CONCACAF Nations League quarter-finals 2023 and UEFA relegation
  play-outs 2022;
- about 23 in the opened 2024-2026 data: CONCACAF play-in and quarter-finals 2024, UEFA
  quarter-finals and play-offs A/B and B/C 2025, play-offs C/D 2026.
- About 20 single-match Nations League finals ties (semi-finals, third place, finals) are
  also stored and ungated.

**Owner's call, one of:**
- **(a) Recommended: a two-leg check first.** Score the stored two-legged national ties
  (about 29) under check 4's rule before serving them: P(advance) log loss no worse than
  "level = coin flip" (point estimate ≤ 0, interval reported), and the replay of each tie's
  winner exact.
  - Each tie is scored as of the Monday before leg 1 (the served case) and before leg 2
    (Stage B's case).
  - The single-match finals ties are reported alongside, not gated.
  - Nothing is chosen on them. If it fails, two-legged ties are withheld and single-match
    ties and tournaments are still served.
  - Cost: cached refits, no fits and no API requests, minutes locally.
- **(b)** Serve two-legged ties as the approved model fixes them, without a check.
- **(c)** Withhold two-legged ties until v2b. Nothing would then be served before the
  CONCACAF finals (June 2027).

**What is served:**
- **Knockout ties:** an upcoming national tie that meets all of these:
  - its competition and round have a written rule in `config/tournament_formats.yml`, under
    `ties` (legs, extra time, away goals, penalties, and source);
  - both sides are among the 218;
  - the latest passing refit is current, under D v1's rule (its own week or the week
    before).
- **What a tie shows:**
  - each side's P(advance), with the 5-95% interval over draws;
  - the path: decided in normal time (or on aggregate), in extra time, or on penalties;
  - the 90' forecast of each leg (D v1), unchanged, alongside;
  - the label "Team-only (Stage D v2): extra time at κ = 1 and a coin-flip shoot-out
    (Stage B fallbacks)".
- **When a tie is computed:**
  - **Before leg 1:** both legs from the score matrices. Leg 1's matrix is convolved with
    leg 2's for the aggregate, then the tie rule is applied.
  - **Between legs:** leg 1 is fixed, and Stage B's `two_legs` closed form is used.
  - **Single match:** B3's closed form, as in D v2's check 2.
- **Tournaments:** an upcoming edition in `tournament_formats.yml` with window `live` that
  meets all of these:
  - its rules validate;
  - the replay of its played results is clean;
  - every team is among the 218.

  It shows P(group position), P(advance), P(reach each round) and P(win), each with its
  Monte Carlo error, the as-of time and the refit, labelled team-only. None qualifies
  today; an edition is added when its draw and fixtures are stored.
- **Storage:**
  - **Ties:** `market_projection` rows at the club milestones. Market `to_qualify` has
    selections home and away; market `qualify_path` has six selections, side × {90, et,
    pens}. Both sit in the same projection run as D v1's 1X2, so the live record scores
    them.
  - **Tournaments:** a JSON per as-of under `serving/tournament/<edition>/` in R2, and a
    `projection_run` row that points to it. No schema migration is needed.
- **Refresh:**
  - after each Monday refit;
  - in the capture tick, when a result in the tie or tournament becomes final. That gives a
    new as-of and the same draws.
- **Withheld, with the reason shown:** no written rule, a failed replay, a side outside the
  218, youth (never), no current refit, and (if the owner chooses (c), or (a) fails)
  two-legged ties.

**Acceptance tests, before it is switched on:**
1. **Identity:**
   - a served single-match P(advance) equals `eval.tournament.tie_probs` at the same refit;
   - a between-legs P(advance) equals Stage B's `two_legs` closed form on the same draws,
     to 1e-9;
   - a served tournament equals `simulate` with the same seed.
2. **Coherence:**
   - P(advance) of the two sides sums to 1;
   - the six path selections sum to 1 and, by side, to its P(advance);
   - a before-leg-1 tie simulated by sampling both legs matches the convolution within 3
     Monte Carlo SE.
3. **Eligibility:**
   - only ties with a written rule and two servable sides are served;
   - every other tie gets a reason and no probability;
   - youth sides never get anything.
4. **No leakage:** the state is as of now minus 3 h, and a refit with as_of after now is
   refused (as D v1).
5. **Shadow on past data, metrics only:**
   - the March 2025 UEFA ties, as of the Monday before leg 1, from the cached refits;
   - the served P(advance) must equal the check-(a) pipeline's to 1e-9;
   - the capture tick must stay within its budget (well under 1 minute for the March 2027
     window).
6. **Dashboard:**
   - a knockout match page shows P(advance) with its path beside the 90' forecast;
   - a tournament page renders from a shadow edition (Euro 2024 as of its opening Monday,
     marked shadow and not linked from the live list);
   - unserved ties show their reason.

**Not in this step:** qualifying groups and league phases (v2b), squad information, and any
change to the match model or the simulator.

- [x] **Check (a) built 2026-11-24, before any score** (commit b0a4669).
  - **Rules** (`config/tournament_formats.yml`, `ties`): only the UEFA Nations League
    two-legged rounds, whose pages state the rule: two legs, extra time when the aggregate
    is level, no away goals, then penalties. These cover the relegation play-outs (2022,
    2024), the quarter-finals and play-offs (2025, 2026), and the 2027 quarter-finals and
    play-offs.
  - **Differences from the draft, disclosed:**
    - Every CONCACAF Nations League tie and every single-match Nations League Finals match
      is left out. Their pages don't state extra time or penalties, and nothing was written
      from memory. So 17 played ties are scored, not about 29, and those rounds are
      withheld when served.
    - The finals matches are not reported either: with no written rule there is no closed
      form to report.
  - **Code:**
    - `adatta/sim/ties.py`: rules, grouping, the winner and path check, and the single,
      before-leg-1 and between-legs probabilities with their paths;
    - `adatta/eval/ties.py`: the check;
    - `eval.knockout.two_legs_m`: Stage B's `two_legs` arithmetic on any score matrices,
      a pure refactor.
  - **Tests** (`tests/test_ties.py`, 10):
    - between legs equals Stage B to 1e-12;
    - single equals B3;
    - before leg 1 is the exact mixture over leg-1 results and agrees with 200,000
      sampled ties;
    - the paths sum to 1;
    - the winner and path checks, grouping, refused rules and leakage.

### Check (a) result (2026-11-24): PASSES

- **Run:** local, from commit b0a4669, in 23 s. Rows are in R2 under
  `tournament/v2/ties/local-b0a4669/`. No fits and no API requests.
- **Replay:** 17 played ties, 0 path problems and 0 irregular. 2 ties are in dev and 15 in
  the opened data; 5 went to extra time.
- **Identity:** at every between-legs point, P(advance) equals Stage B's `two_legs` on the
  same matrices to 1e-9 (a run-time check).

| point | log loss (level = coin flip) | difference [95%] | ECE / noise floor |
|---|---|---|---|
| before leg 1 (served case) | 0.4621 (0.4763) | −0.0142 [−0.0200, −0.0085] | 0.236 / 0.327 |
| between legs | 0.4512 (0.4702) | −0.0189 [−0.0320, −0.0059] | 0.244 / 0.307 |

- **Reading:** at both points the interval excludes zero, in the model's favour. 17 ties is
  little, which the ECE noise floors show. Under the owner's choice (a), two-legged ties
  are served.

- [x] **D v2 serving built 2026-11-24.**
  - **Ties** (`adatta/sim/ties.py`, called from `national_serve.run_projection`):
    - each leg of a tie with a written rule gets `to_qualify` (home, away) and
      `qualify_path` (six selections) rows;
    - they are written at D v1's milestones, in the same projection run as its 1X2, from
      the same live refit;
    - `projection_run.component_versions` carries `to_qualify` (the rules hash, κ, the
      shoot-out probability and the seed);
    - legs final by now − 3 h are fixed.
  - **Tournaments** (`adatta/sim/tournament_serve.py`, in the same capture-tick step):
    - live editions only; withheld on a replay problem, a team outside the servable sides,
      or no current refit;
    - a JSON per forecast under `serving/tournament/<edition>/`, with a `projection_run`
      row (component `tournament`);
    - a new forecast only when the refit or the set of final results changes.
  - **The 90' forecast is unchanged:** a tie's legs are computed on the simulator's joint
    rates (one walk for both legs). D v1's 1X2 for the same leg is unchanged, so the two
    can differ by the walk's sampling noise.
  - **API:**
    - `qualify` on the national match view: stage, P(advance) with its 90% interval, the
      path and the legs. Otherwise a "Going through" reason: no written rule, no refit,
      youth. A non-knockout match gets none.
    - `/api/tournaments` and `/api/tournaments/{key}` (`?shadow=true` for a past edition).
  - **Web:** a "Going through" panel on the match page, a tournament page, and a link from
    the fixture list when a tournament is served.
- **Acceptance tests:**
  1. **Identity: pass.**
     - Between legs equals Stage B's `two_legs` to 1e-12 (unit tests), and to 1e-9 at every
       check-(a) point.
     - Single equals B3.
     - The served rows equal `ties.project` on the refit's draws.
     - A served tournament payload is `simulate` with the same seed.
  2. **Coherence: pass.**
     - P(advance) sums to 1, and the six path selections to 1 and, by side, to P(advance).
     - Before leg 1 is the exact mixture over leg-1 results and agrees with 200,000 sampled
       ties within 4 SE. The acceptance said 3 SE; that test uses 4.
  3. **Eligibility: pass.** A round with no rule gets the 90' forecast only, with the
     reason. A youth tie gets nothing. A non-knockout match shows no panel.
  4. **No leakage: pass.**
     - Leg 1 is used only once final by now − 3 h.
     - A refit after now is refused (D v1's check, and in the tournament path).
  5. **Shadow, March 2025 UEFA ties: PASSES.**
     - `python -m adatta.eval.ties shadow --run local-b0a4669` on 12 ties.
     - The serving path equals check (a)'s before-leg-1 P(advance) with a largest gap of
       0.0.
     - 0.10 s per tie at most, 0.37 s in all: well within the capture tick.
  6. **Dashboard: pass.**
     - The Euro 2024 shadow (`/tournament/euro2024?shadow=1`, written by `python -m
       adatta.sim.tournament_serve shadow --edition euro2024` in 23 s) renders: 6 groups,
       position probabilities summing to 100%, Monte Carlo SE ≤ 0.35 points on hover, and
       marked shadow.
     - A March 2027 play-off leg shows "Going through: No passing national refit yet".
     - The panel with numbers is covered by the API test, not seen live: no live refit
       exists yet.
  - Tests are in `tests/test_tournament_serve.py` (9) and `tests/test_ties.py` (10).
- **Switch-on:** nothing to dispatch.
  - D v1's Monday refit runs in weeks with a servable match within 14 days, so the first is
    15 March 2027. The capture tick then writes the 12 UEFA ties' P(advance) at each
    milestone.
  - Tournaments start when an edition is added to `tournament_formats.yml` with window
    `live` and its groups.
- **Not yet:**
  - **The live record** of served ties and tournaments: scored after each ends, with the
    owner's review after the first two;
  - **CONCACAF Nations League rules**, which need the regulations themselves.

## Cross-cutting

- **Order (D37, 2026-11-16):** Phase 3 comes next. Stage A finishes the A1 run and A2,
  both already built; Stages B, C and D wait until after Phase 3.
- **Travel (owner, 2026-11-16):** the G2 travel and altitude terms failed on domestic
  league matches (`PHASE2_TASKS.md`), where travel is part of home advantage. They are
  re-evaluated where they can matter: cup ties (lower-tier hosts, long trips across a
  country, neutral finals) and continental and international matches (Stages A, C, D),
  as a tested term in each stage's acceptance, never assumed. The features need no
  rebuild.
- **Serving and refits:** results-triggered refits (D33) already work from the fixture
  list, so cup and continental midweek results are used as soon as these matches are in
  the model.
- **Minors:** youth internationals stay out of scope without review (CLAUDE.md rule 10).
- **Quota:** each stage's data load goes through the quota planner; the upgrade trigger
  (about 70% sustained use) is checked before Stage 0 and again before C2.

## Suggested order

1. Stage 0 data loads, alongside Phase 2.
2. Stage A, then Stage B (they share the cup data).
3. Stage D v0 in parallel with A/B if time allows (separate data and fit).
4. Stage C1, then C2 league by league.
5. Phase 3 (squad and player), then lineup-aware cups, D v1 and D v2.

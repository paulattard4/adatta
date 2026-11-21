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

- [ ] **C1 build (2026-11-21), before any C1 score:**
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

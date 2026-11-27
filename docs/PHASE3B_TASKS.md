# Phase 3b — half split, goal decomposition, discipline (spec §8.2, §8.3, §11, §19.3)

Goal: markets split by half and goal-type and discipline sub-models, built on the
production full-time model (`phase1b_strength_open_xg`) without changing it. Order: (1) the
half split for goals, (2) discipline and corners, (3) goal decomposition (penalty, own goal;
set pieces need another signal, SOURCE_FEASIBILITY.md).

Status (2026-11-25): **P3b-1 acceptance approved (owner: "Approve as drafted"); building.
Dev result: FAILS check 2 alone (the 0-0 PPC on six 2020 refits); checks 1, 3-5 pass.
Owner: "Amend, judge on (i)" (D50): re-gated on dev, PASSES. Window (i) PASSES
(run `local-9de69db`). P3b-2 (serving) approved (D51), built; (e) 1-5 pass, (e) 6 is the first live week.**

Tick each item only when its acceptance check passes.

## Tasks

- [ ] **P3b-1 Half split for goals (§8.2).** Drafted 2026-11-25 before building; owner,
      2026-11-25: "Approve as drafted" (D46: window (i); D47: logit-normal).

      **Seen so far** (metrics only, no requests, 2026-11-25; the holdout and 2026/27
      half-time outcomes not inspected beyond coverage counts):
      - Half-time score stored on 99.97% of dev matches (19,394 in the ten bundle
        leagues; 5 missing, in 62 and 136), 100% of the holdout (7,575) and of 2026/27
        so far (1,261). No row has a half-time score above the full-time one (40,097
        checked).
      - First-half share of 90' goals, warm-up + dev: home 0.442 (45,697 goals), away
        0.434 (36,284). By league 0.426 (141) to 0.453 (136); with about 8,000 goals per
        league the binomial SE is about 0.0055, so the league spread is small. 31.5% of
        matches are 0-0 at half time.
      - `has_half_split_stats`: 297 open rows available, 16 partial, 3 unknown (all
        stored competitions).

      **Scope.** Team goals only, in regular-season league matches of the ten bundle
      leagues (Phase 1a's scope), in league-seasons where `has_half_split_stats` is
      available (rule 8: otherwise no half markets, never an imputed split). Cups,
      national teams, player-level splits (which need event minutes), and corner and card
      splits come later.

      **(a) Mechanism (spec §8.2, as written).** The full-time matrix is primary. Within
      each posterior draw d of the production model, the 90' matrix M_d (Dixon-Coles rho
      and draw inflation kappa included, max_goals 15) is split by binomial thinning: the
      h home goals fall in the first half with probability p_home, independently, and the
      a away goals with p_away:

          P(HT = (x, y), FT = (h, a)) = M_d[h, a] · Bin(x; h, p_home) · Bin(y; a, p_away)

      Half scores sum to full time in every draw and the full-time matrix keeps rho.
      Second-half goals are FT − HT. Stated simplification (§8.2): no effect of the
      half-time score on the second half beyond what thinning implies. Layer 2 is
      untouched: half-time scores never enter it.

      **(b) The share model.** For side j of match i with N_ij 90' goals,
      x_ij ~ Binomial(N_ij, p_ij) (sides with N = 0 carry no information). Three nested
      variants, pre-registered in this order:
      - **S0, side only:** logit p = β_side, β ~ Normal(logit 0.44, 0.5).
      - **S1, + league:** + α_league, α ~ Normal(0, σ_league), centred,
        σ_league ~ HalfNormal(0.2).
      - **S2, + scoring team (the spec's pooling by team and league):** + u_team,
        u ~ Normal(0, σ_team), σ_team ~ HalfNormal(0.2). A team with no training match
        gets u = 0 (the pool mean).
      A conceding-team term and covariates (congestion, importance, formation) are §8.2's
      later refinement, not part of this item.

      Fitted jointly over the ten leagues by NUTS, 2,000 draws, as of each refit date on
      matches final by then (kickoff + 3 h ≤ as-of) within `lookback_days` (1,095, as
      Layer 2). Grid: every 28 days on the dev Mondays (D14); the share moves slowly.
      Share draws are paired with the full-time draws by index. They are independent
      fits, so no correlation is claimed from that (§11.3); mean probabilities don't
      depend on the pairing.

      **(c) Inference gates, every share refit:** R-hat ≤ 1.01, bulk ESS ≥ 400, no
      divergences.
      - PPC, dispersion: the variance of x given N, for N = 1, 2, 3, 4 and 5+, inside the
        posterior predictive 95% band. The same for the share of 0-0 half times among
        matches with goals.
      - Prior sensitivity: refit with the σ prior scales halved and doubled. The mean
        share of every league moves by less than 0.005.
      - Identifiability: α centred, and the σ_team posterior is reported against its
        prior.
      A refit that fails the gates falls back to the next simpler variant for that date
      and is counted. The chosen variant must pass at ≥ 97% of refits. If the dispersion
      PPC fails, I raise it rather than switching on my own to a beta-binomial (a
      per-match Beta share, which still sums exactly).

      **(d) Dev run.** Dev seasons 2019-2023. The full-time draws come from the fit cache
      of the production weekly run 43421580602 (no Layer 2 refits), predicted at the
      headline horizon exactly as its 1X2 was. Markets:
      - half-time 1X2 (headline);
      - HT/FT (nine combinations);
      - half-time O/U 0.5 and 1.5;
      - second-half 1X2;
      - half-time correct score.

      Baselines:
      - **B_clim:** each league's half-time 1X2 and HT/FT frequencies over the same
        training window as the share refit.
      - **S0 thinning:** the comparison for S1 or S2.

      **(e) Dev gate (all must pass):**
      1. **Coherence, exact, every match:**
         - the HT/FT nine sum to 1 and marginalise to the stored FT 1X2 (to 1e-9) and to
           the half-time 1X2;
         - the half-time matrix sums to 1 − the FT tail mass;
         - within a draw, 1H + 2H = FT (unit and property tests).
      2. **Share model chosen by the conditional score.** The score is
         −log P(HT score | FT score) per match, with the share integrated over its draws.
         S1 replaces S0 only if the paired 95% interval of the mean difference is below
         0; S2 replaces the chosen one likewise. Otherwise the simpler variant stays. The
         chosen variant passes (c) at ≥ 97% of refits.
      3. **Better than climatology:**
         - half-time 1X2 log loss and RPS below B_clim, paired 95% interval excluding 0;
         - HT/FT log loss likewise.
      4. **Calibrated:** ECE of each half-time 1X2 outcome and of half-time O/U 1.5 no
         more than 0.005 above its noise floor.
      5. **Segments (D18 rule):** no league worse than B_clim by more than 1.0% on
         half-time 1X2 log loss. If S1 or S2 is chosen, no league worse than S0 by more
         than 1.0% on the conditional score.
      6. **Leakage:** adding matches after the as-of changes no forecast and no share
         draw (tests). The share fit reads only outcomes final by its as-of. Odds are not
         read.

      **Reported, not gated:**
      - each variant's conditional score;
      - the posterior share per league and side, and the spread of team shares;
      - HT/FT ECE per combination;
      - the simplification check: among matches level at half time, the observed
        second-half goal rate against the one thinning implies.

      **(f) Promotion window (D46, owner).** On a dev pass, checks 1, 3 and 4 are run
      once on a window never used for half-time work, with the same share variant and
      the same rules:
      - **(i) 2026/27 to date, fixed now. Recommended.** The window is every 2026/27
        match in scope with kickoff before 2026-11-24 (1,261 matches, final before this
        draft; none of their half-time outcomes inspected):
        - matches from 9 November (126) use the stored live refits exactly as served;
        - the 1,135 earlier matches use a production-spec weekly walk-forward over the
          Mondays without a stored live refit, about 65 NUTS refits in CI;
        - the share is refit on the same Mondays.
        It is the live period of D38, scored as-of, and it answers once the refits have
        run.
      - **(ii) Live only, from the first stored live refit (9 Nov 2026),** judged at
        2,000 matches. At about 100 matches a week in the ten leagues, plus the winter
        breaks, that is around April 2027. Shadow until then. No CI cost.
      - **(iii) Open the 2024/25-2025/26 holdout a third time** (7,575 matches, half-time
        outcomes untouched). D38 declined a third opening for step (d).

      **(g) Spec point (D47, owner).** §8.2 says "share_1H ∼ Beta, hierarchically pooled by
      team and league". I propose the logit-normal hierarchy in (b): the same pooling, and
      it adds the side term (home 0.442 against away 0.434 above) without a nested Beta
      construction. A Beta hierarchy (league mean ~ Beta, team ~ Beta(m·κ, (1 − m)·κ)) is
      the alternative. The per-match Beta share in (c) stays an option for overdispersion
      either way.

      **Raised while building (2026-11-25), before any dev score:** the dispersion PPC
      in (c) fails. This was found at the first share refit (as of 2019-07-22, training
      2016-2019). The half-time splits are **under-dispersed**: goals are spread across
      the halves more evenly than binomial thinning allows.
      - PPC quantiles: N = 1 0.73, N = 2 0.47, N = 3 0.00, N = 4 0.01, N = 5+ 0.00. The
        0-0 half-time PPC passes at 0.05. S0, S1 and S2 are alike.
      - Direct check on warm-up + dev (62,138 sides with a goal, pooled share 0.4385):
        the variance of first-half goals against binomial is 0.986 at N = 2, 0.96 at
        N = 3, 0.92 at N = 4 and 0.78 at N = 5. At N = 5, all five or none in the first
        half happen 3.7% of the time against 7.2% for a binomial. Per match (total
        goals), the ratio is 0.94 at 3 goals and 0.85 at 6.
      - (c)'s named fallback, a per-match Beta share, can only widen the split
        (over-dispersion), so it doesn't apply. Nothing has been changed; it is raised to
        the owner.
      - Also seen: S2's R-hat was 1.012 at that refit (S0 1.007, S1 1.010; no
        divergences).
      - Cost: grouping the sides by (side, league, team) gives the same likelihood up to
        a constant and the same posterior, at 13 s, 14 s and 38 s per fit. That is about
        3.3 hours for the dev share refits with the sensitivity fits, in the local model
        container (D13).

      *Amended 2026-11-25, before any dev score (owner: "Report-only, as D20"; D48):*
      1. The dispersion PPC (N = 1, 2, 3, 4, 5+) is **reported, not gating**, as D20 did
         for goal totals. Binomial thinning stays as §8.2 writes it, a stated
         simplification (goals spread across the halves more evenly than it implies).
         The 0-0 half-time PPC stays gating. Checks 3-5 decide whether it matters for
         the markets.
      2. NUTS warm-up is 1,000 steps (draws stay 2,000), a sampler setting.
      3. Reported, not gated, beside the dispersion PPC: half-time O/U 0.5 and 1.5 ECE
         in the top fifth of matches by expected total goals, where most sides with 3+
         goals are.

      **Built (2026-11-25), before any dev score:**
      - `adatta/sim/half_split.py` (numpy): thinning, the half markets (half-time 1X2,
        HT/FT, half-time O/U 0.5 and 1.5, second-half 1X2, half-time correct score),
        check 1's coherence, and the conditional score.
      - `adatta/model/half_split.py` (model container, D13):
        - the data (half-time score null when not stored; scope = league-seasons with
          `has_half_split_stats` available; training uses any stored half-time score);
        - the 28-day share plan (dev only), S0-S2, and the gates of (c) as amended;
        - CLI `run --variant`.
        The fit groups the sides by (side, league, team): the same likelihood up to a
        constant and the same posterior.
      - `adatta/eval/half_split.py`:
        - `matrices` rebuilds each dev target's posterior-mean 90' matrix from run
          43421580602's own fit cache (its stored config hash `2a82dbe0285182f9`). Every
          target's 1X2 must equal the run's stored one to 1e-9, or the stage stops. The
          current production hash `a215d52e759864ed` holds later refits at a few dates
          (France 2019: up to 0.0084 apart in p_home), so it is not used.
        - `assemble` implements (e) and the reported items.
        - B_clim uses Jeffreys pseudo-counts (0.5 per outcome).
        - A target's share is the latest share refit at or before its forecast's as-of.
      - `adatta/model/backtest.py`: `score_targets(..., matrix=True)` adds the
        posterior-mean matrix using the vectorised `serve.score_matrices` (equal to the
        per-draw loop to 1e-12). The default path is unchanged.
      - The posterior-mean half probabilities come from the mean 90' matrix and 200 share
        draws: the split is linear in the matrix and the fits are independent.
      - `tests/test_half_split.py` (12).

      **Dev result (2026-11-25): FAILS check 2 alone; checks 1, 3, 4 and 5 pass. Not
      promoted; nothing tuned.** Run `local-7d419a7`: matrices from run 43421580602's fit
      cache, identical to its 1X2 on all 19,254 dev targets, and 183 share refits (S0, S1
      and S2 × 61 dates).
      - **Rerun of killed refits:** the S1 and S2 containers were killed mid-run, likely
        out of memory: S1 after 58 dates, S2 after 45. The missing dates were rerun with
        `--only`, with the same seed and code apart from that option.
      - **Scope:**
        - 17,723 targets scored. The 1,531 out of scope are 4 matches with no half-time
          score, plus the 2022 seasons of Ligue 2, 2. Bundesliga, Serie B and Segunda.
          Those four have no open `has_half_split_stats` row in the capability table (an
          audit gap; their half-time scores are stored).
        - Of the 17,723, 1,550 fall on the 6 S0 refits that failed (c) and have no
          fallback, so checks 3-5 score 16,173.
      1. **Coherence:** 0 of 16,173 incoherent. **Pass.**
      2. **Share model chosen by the conditional score:** S0. **FAIL.**
         - S1 vs S0: +0.0000 [-0.0002, +0.0002]. S2 vs S0: +0.0000 [-0.0002, +0.0003].
           Neither league nor team terms help, so S0 stays.
         - S0 passes (c) at 55 of 61 refits (90%, against the 97% needed). All 6 failures
           are the 0-0 half-time PPC, at quantiles 0.000-0.020 against the 0.025 band
           edge. They are the six refits from 2020-05-25 to 2020-11-09, whose training
           windows include the 2020 restart (no crowds, five substitutes, drinks breaks).
           Elsewhere that quantile has a median of 0.135 and a maximum of 0.37.
         - S1 fails (c) at 11 of 61 refits and S2 at 59 of 61 (mostly convergence: tiny
           league and team spreads). Neither is chosen anyway.
      3. **Better than climatology: pass.**
         - Half-time 1X2 log loss 1.0463 against B_clim 1.0759: -0.0296 [-0.0332, -0.0261]
           (-2.8%).
         - RPS: -0.0084 [-0.0094, -0.0074].
         - HT/FT log loss 1.9098 against 1.9683: -0.0585 [-0.0635, -0.0534] (-3.0%).
      4. **Calibrated** (ECE against its noise floor): half-time home 0.0083 (floor
         0.0102), draw 0.0069 (0.0092), away 0.0043 (0.0092), over 1.5 0.0091 (0.0093).
         **Pass.**
      5. **Segments:** every league beats B_clim on half-time 1X2 log loss. **Pass.**
      - **Reported, not gated:**
        - Conditional score: S0 1.33369, S1 1.33344, S2 1.33347.
        - Simplification check: after a level half time, 1.401 second-half goals observed
          against 1.441 implied. Thinning overstates them by about 3%, the same
          under-dispersion D48 recorded.
        - Top fifth by expected goals: half-time O/U 0.5 ECE 0.0170 (floor 0.0162),
          O/U 1.5 0.0140 (floor 0.0210).
      - **Reading:** the half markets themselves do well: better than climatology by
        about 3%, calibrated, and better in every league. The failure is in the share
        model's own gate: the 0-0 PPC on six 2020 training windows. Following the
        acceptance, the gate is not relaxed after seeing the score. Raised to the owner;
        window (i) stays unopened.

      *Amendment 2 (2026-11-25, after the dev result; owner: "Amend, judge on (i)"; D50),
      disclosed as made after seeing the dev score:*
      1. **The 0-0 half-time PPC is reported, not gating,** like the dispersion PPC (D48).
         Both measure the shape of the split, which binomial thinning cannot fit exactly.
         A share refit then passes (c) on convergence (R-hat, ESS, divergences) and prior
         sensitivity.
      2. **Dev is re-gated from the stored refits of run `local-7d419a7`,** with no refit
         and nothing else changed: the same matrices, draws, variants, selection rule and
         checks 1-5. The result is recorded beside the first, which stands as run.
      3. **Window (i) (D46) decides,** with checks 1, 3 and 4 as in (f). It has never been
         inspected for half-time work.

      **Dev re-gated under amendment 2 (2026-11-25, run `local-a21ae9d` on the stored
      refits of `local-7d419a7`; no refit): PASSES, S0.** The first result above stands
      as run.
      - 17,723 scored (out of scope as before).
      - **Check 2:** S0 passes (c) at 61 of 61 refits. S1 vs S0: -0.0000 [-0.0002,
        +0.0002]; S2 vs S0: +0.0000 [-0.0002, +0.0002]; S0 stays.
      - **Check 1:** 0 incoherent.
      - **Check 3:**
        - Half-time 1X2 log loss 1.0467 against 1.0755: -0.0287 [-0.0321, -0.0254]
          (-2.7%).
        - RPS: -0.0081 [-0.0091, -0.0072].
        - HT/FT log loss 1.9111 against 1.9687: -0.0576 [-0.0624, -0.0528] (-2.9%).
      - **Check 4, ECE against its floor:** home 0.0063 (0.0097), draw 0.0057 (0.0088),
        away 0.0037 (0.0086), over 1.5 0.0095 (0.0090). Over 1.5 is within the 0.005
        margin.
      - **Check 5:** every league better than B_clim.
      - **Reported:**
        - After a level half time, 1.398 second-half goals observed against 1.441
          implied.
        - Top fifth by expected goals: O/U 0.5 ECE 0.0178 (floor 0.0149), O/U 1.5 0.0146
          (floor 0.0198).
      - Window (i) decides (checks 1, 3, 4).

      **Window (i) result (2026-11-25, run `local-9de69db`): PASSES, S0.** Gating checks 1,
      3 and 4 (D46, D50).
      - **Scored:** 1,247 matches, the 2026/27 matches with kickoff before 2026-11-24,
        none out of scope.
        - 1,137 use walk-forward matrices from CI run 45603457907.
        - 110 use the live Monday refit they were served from, identical to the served
          1X2 to 1e-9.
      - **Shares:** S0 refit on the 17 weekly Mondays from 2026-08-03 to 11-23
        (`local-81f6449`). All 17 pass (c).
      - **Check 1:** 0 incoherent.
      - **Check 3:**
        - Half-time 1X2 log loss 1.0592 against 1.0849: -0.0257 [-0.0377, -0.0138]
          (-2.4%).
        - RPS: -0.0074 [-0.0107, -0.0040].
        - HT/FT log loss 1.9232 against 1.9788: -0.0557 [-0.0731, -0.0383] (-2.8%).
      - **Check 4, ECE against its floor:** home 0.0279 (0.0358), draw 0.0135 (0.0313),
        away 0.0138 (0.0328), over 1.5 0.0269 (0.0332). All are under the floor.
      - **Reported, not gating on (i):**
        - **Check 5:** nine leagues are better than B_clim. Serie B (136, 120 matches)
          is worse: +3.07%, a log loss difference of +0.0329 [+0.0067, +0.0592] by
          bootstrap.
          - It comes from the full-time model, not the split. Serie B's full-time 1X2
            log loss on the same matches is 1.1001, against 1.0931 for a constant fitted
            in-sample to these results.
          - Recorded for the Layer 2 watch; nothing tuned.
        - After a level half time: 1.567 second-half goals observed against 1.476
          implied. On dev it was 1.398 against 1.441, so the sign differs and neither
          window points to a correction.
        - Top fifth by expected goals (250 matches): O/U 0.5 ECE 0.0219 (floor 0.0579),
          O/U 1.5 0.0398 (floor 0.0742).
      - **Fixed while assembling, no score effect:** live rows carried `as_of` labelled
        GMT and walk-forward rows UTC, which polars would not concatenate. Both are now
        UTC: at the source, and on load for the parts already stored.

      **Then.** A serving acceptance is drafted after the promotion result, before
      anything is shown. It covers the markets in the same projection run as the 1X2,
      the match-page panel, and the coherence checks at serve time.

      **Cost:** 0 API requests and no Layer 2 refits for dev. About 180 small share fits
      (3 variants × about 60 dates), roughly 3 hours. They run as one backtest-type
      workflow, alone, and not on a Monday. Window option (i) adds about 65 Layer 2 NUTS
      refits in CI, as a second workflow after the dev one.

- [ ] **P3b-2 Serve the half markets.** Drafted 2026-11-25, after the P3b-1 window (i)
      pass and before building or showing anything. Owner, 2026-11-25: "Approve as
      drafted" (D51: the three gated markets; to the page after (e) 1-5).

      **What is served.** Only the markets that gated on dev and window (i):
      - half-time 1X2 (`HT1X2`: home, draw, away; checks 3 and 4);
      - HT/FT (`HTFT`: the nine combinations `1/1` ... `2/2`; check 3);
      - half-time O/U 1.5 (`HTOU` 1.5: over, under; check 4).

      Half-time O/U 0.5, second-half 1X2 and the half-time correct score were scored
      but never gated (the top-fifth O/U 0.5 ECE was above its floor on dev; the
      second-half rate after a level half time has opposite signs on dev and the
      window). They stay unserved; owner choice (h).

      **Scope.** As P3b-1: regular-season league matches of the ten bundle leagues, in
      league-seasons where `has_half_split_stats` is available. Cup ties (served as the
      home side's league fixture), national teams and the C2 leagues get no half markets
      until their own acceptance. Out of scope means no half rows (rule 8), never an
      imputed split.

      **(a) The share refit in production.** S0 as chosen, with the (c) gates as amended
      (D48, D50: convergence and prior sensitivity gate; both PPCs are reported).
      - One refit every Monday as of 00:00 UTC, jointly over the ten leagues, on matches
        final by then within `lookback_days`. These are the same Mondays as window (i).
        Results-trigger refits of Layer 2 (D41) reuse the latest Monday share.
      - It runs as its own job in `serve.yml`, on the Monday dispatch, after the bundle
        refits. It is a single S0 fit plus two sensitivity fits, about 1 minute, with
        `max-parallel 1` unchanged.
      - Draws go to R2 under `serving/half/`. A `model_run` row (window live, component
        `half_split`) records the as-of, the gates and the training hash.
      - A refit that fails its gates is recorded and not used. There is no simpler
        variant than S0, so the last passing refit stays in use. A share refit more than
        28 days old (the dev grid) is not used, and in-scope fixtures then get no half
        rows; the stale-refit report covers it as for Layer 2 (`STALE_AFTER`).

      **(b) Projection.** Half markets are written by `serve.run_projection` in the same
      projection run, for the same match and milestone as the 90' markets, and from the
      same Layer 2 refit's draws. `component_versions` names the share refit used.
      - The 2,000 share draws are thinned evenly to the 400 stored 90' draws
        (`max_pred_draws`) and paired by index (as (b) of P3b-1; no correlation is
        claimed).
      - The published probability is the mean over draws. The 5th-95th interval is
        taken per draw, as for every other market.
      - A fixture already projected before deployment gets half markets from its next
        milestone. No past milestone is rewritten.
      - The leak rule is the same as Layer 2's. The share refit used has as-of ≤ the
        projection time and reads only outcomes final by its as-of. Odds are not read.

      **(c) Coherence at write time.** Before a fixture's half rows are written:
      - the HT/FT nine sum to 1;
      - they marginalise to the 90' 1X2 being written for that milestone, to 1e-9, and to
        the half-time 1X2;
      - half-time O/U 1.5 is computed from the same half-time matrix.
      A fixture that fails is reported as a problem, and gets its 90' rows and no half
      rows.

      **(d) API and match page.**
      - A "Half time" panel shows the half-time 1X2, the HT/FT 3×3 grid and half-time
        O/U 1.5, with intervals, for the latest milestone.
      - As for the score matrix (§11.2), the view recomputes the half markets from the
        stored Layer 2 refit and share refit and checks them against the stored rows to
        1e-9 (`COHERENCE_TOL`). An incoherent recomputation is not shown, and the
        coherence flag says why.
      - Out of scope, or no passing share refit: the panel says which, in one line.
      - No odds anywhere on the panel.

      **(e) Acceptance (all must pass before the panel is shown):**
      1. **The 90' markets are untouched.** With the half split switched on, every 90'
         row a projection writes is identical to the one written with it off (test, on a
         stored refit).
      2. **Replay of window (i).** For the 110 live window matches, the half-time 1X2,
         HT/FT and O/U 1.5 recomputed by the serving code from the stored live refits
         and the window's S0 Monday refits match the window (i) rows (`local-9de69db`)
         to within 0.001 absolute. The difference is Monte Carlo only: the window used
         the posterior-mean matrix with 200 share draws.
      3. **Coherence:** (c) holds on every replayed match, and the unit tests break it
         on purpose.
      4. **Scope:** tests show no half rows for a cup tie, a national match, a
         league-season without `has_half_split_stats`, a share refit failing its gates,
         or a share refit older than 28 days.
      5. **Leakage and idempotence:**
         - a share refit dated after the projection time is refused;
         - writing the same (match, milestone) twice adds nothing.
      6. **Live, first week after deployment:** every in-scope fixture with a 90'
         milestone has its half rows, or a stated reason. The page's coherence flag is
         true on all of them. The Monday share refit passes its gates in CI.

      **Reported, not gated:** the live half-time record (log loss and RPS against B_clim
      per league, half-time 1X2 and HT/FT) on the played page, from the first served
      match. Serie B is watched there (P3b-1 window: +3.1% against climatology, from its
      90' model).

      **(h) Owner choices.**
      - **Markets: the three gated markets only (recommended),** or also O/U 0.5 and
        second-half 1X2, shown as unvalidated.
      - **Deployment: straight to the page after (e) 1-5 (recommended).** Window (i) was
        the promotion test, and (e) 6 is checked in the first week. The alternative is a
        shadow week with rows written but the panel hidden.

      **Cost:** 0 API requests (half-time scores already arrive with results). About 1
      minute of CI a week for the share refit. Projection time rises by the thinning:
      400 draws × a 16 × 16 matrix per fixture, well under a second.

      **Built (2026-11-25), (e) 1-5 pass; (e) 6 after deployment.**
      - **Share refit:** `python -m adatta.model.half_split live`. S0 as of Monday 00:00
        UTC, 400 thinned draws, the (c) gates as amended. It writes to
        `serving/half/S0/<date>.npz` and a `model_run` row (`half_split_S0`, component
        `half_split`, live), and waits ("not ready") while a result before the as-of is
        missing. It is the new `share` job in `serve.yml`, after the bundle refits, skipped
        for a trigger as-of.
      - **Projection:** `serve.run_projection(..., half_split=True)` from the capture tick
        (`adatta/sim/half_serve.py`). The rows are `HT1X2` (1, X, 2), `HTFT` (`1/1` ...
        `2/2`) and `HTOU` 1.5 (over, under). `component_versions["half_split"]` names the
        share refit used.
      - **Match view:** `half_coherence` and `half_reason` on the match API, and the "Half
        time" panel (half-time 1X2, the HT/FT grid, half-time O/U 1.5). The half markets
        are left out of the 90' market grid. The "Half-split" entry under "Not available
        yet" is removed.
      - **(e) 1:** the 90' rows are identical with the split on and off (probability and
        interval, every selection).
      - **(e) 2:** replay of window (i): 110 live matches through the serving code against
        `local-9de69db` (`python -m adatta.eval.half_split replay --matrices local-81f6449
        --shares local-81f6449 --rows local-9de69db`). The maximum absolute difference is
        5.5e-5 on half-time 1X2, 5.5e-5 on HT/FT and 6.2e-5 on O/U 1.5, against 0.001.
        **Passes.**
      - **(e) 3:** the HT/FT nine sum to 1 and marginalise to the stored 90' 1X2 and the
        half-time 1X2 to 1e-9; the per-draw markets equal P3b-1's split to 1e-12. Tests
        break coherence on purpose:
        - at write, the fixture keeps its 90' rows only and a problem is reported;
        - at view, a stored row moved by 1e-6 is not shown.
      - **(e) 4:** no half rows for a cup tie, a league-season with the capability
        `partial`, a failed share refit, one older than 28 days, or none. The last passing
        refit is used when the newest one failed.
      - **(e) 5:** a share refit dated after the projection time is not used, and a second
        tick adds no rows. The production refit records itself once, refuses a non-Monday
        and waits for missing results.
      - 583 tests pass; ruff and mypy are clean.
      - **Local page check:** match 135 shows the reason line ("no usable share refit"),
        because production has no share refit until this code runs in CI.
      - **(e) 6, live, from deployment:**
        - The first share refit runs on the next Monday dispatch (2026-11-30). A serve
          dispatch with `as_of` 2026-11-23 would start the half markets this week: the
          bundle refits for that Monday already exist and are reused.
        - Then for a week: every in-scope fixture with a 90' milestone has its half rows
          or a stated reason, the page's coherence flag is true, and the share refit
          passes in CI.

- [ ] **P3b-3 Discipline: team cards with referee strictness (§8.4, Layer 5c).** Drafted
      2026-11-27 before building (owner: "Yes, that order", item 3). **Approved
      2026-11-27** (owner: "Approve as drafted", D66): window (i), negative binomial,
      league matches only.

      **Seen so far** (counts and spreads only, no requests, 2026-11-27). Card numbers
      come from warm-up + dev (2016-2023). Holdout and 2026/27 card outcomes were not
      inspected beyond coverage.
      - **Coverage:** card, foul and corner counts for both teams on 100% of stored
        matches in the ten bundle leagues, 2016-2025. The referee is named on 99.6-100%.
      - **Cards per match** (yellow + red as the provider counts, both teams),
        2016-2023:
        - top flights: Premier League 3.64, Ligue 1 4.00, Bundesliga 3.92, Serie A 4.77,
          La Liga 5.24;
        - second tiers: 3.73 to 5.30.
        - Variance / mean 0.93-1.26, close to Poisson at match level.
        - Red cards 0.21 per match.
        - The yearly mean moves 4.16 (2020) to 4.61 (2019).
      - **Referees:** 26-61 per league with at least 10 dev matches (median 28-72 each).
        The sd of their true cards-per-match means (method of moments) is 0.19
        (Bundesliga) to 0.59 (Segunda), against a single match's sd of 2.1-2.6.
      - **Teams:** the sd of a team-season's true cards per team-match is 0.19-0.38,
        against 1.4-1.7 for a single team-match. Two teams together are about the same
        size as the referee. Each is roughly 5-12% of a match's cards: real, but modest.
      - **When the referee is known** (2026/27 matches tracked from at least 72 h
        before kickoff; 9-26 per league):
        - every top flight and second tier except Spain's is named at least 24 h ahead
          in 74-100% of matches, with a median of 35-58 h;
        - La Liga's median is 8 h (none at 24 h, 80% by T-60m) and Segunda's 10 h (4% at
          24 h, all by T-60m).

        This matches D26.

      **Scope.** Team cards (yellow + red, as the provider counts) in regular-season
      league matches of the ten bundle leagues, where `has_cards_data` holds (rule 8:
      otherwise no card markets).
      - **Markets:** total match cards (the distribution; O/U 3.5, 4.5, 5.5) and team
        cards (O/U 1.5, 2.5).
      - **Later items:** red-card probability (a separate, heavily shrunk process,
        §8.4), fouls (tier 2), player cards, corners (P3b-4), cups, national teams, and
        importance and derby terms (§8.4's other inputs, as a gated refinement).
      - Layer 2 is untouched. Cards feed markets only (§8.4: "Layer 6 only as minor
        context", not used here).

      **(a) The model.** For team j against opponent o in match i, with referee r of
      country c:

          y_ij ~ NegBinomial(mu_ij, phi)          (owner choice 2)
          log mu_ij = c[l, s] + h·home_ij + a[j] + b[o] + s_ref[r]

      - **c[l, s]**, league-season level: ~ Normal(c_l, σ_season), so the current
        season's level is pooled to the league's while it has few matches.
      - **h**, home term.
      - **a[j]**, own card tendency: ~ Normal(0, σ_a), team pooled to league through
        c_l.
      - **b[o]**, the cards a team draws from its opponents (the spec's "opponent foul
        tendency"): ~ Normal(0, σ_b).
      - **s_ref[r]**, referee strictness: ~ Normal(0, σ_ref), one per referee identity
        (`features.referees`, the country's pool across tiers). A referee with no
        training match gets a draw from the pool, Normal(0, σ_ref), as does any match
        whose referee isn't known at the horizon. A new team gets a = b = 0.
      - **Priors:** σ ~ HalfNormal(0.3); c_l ~ Normal(log 2.2, 0.5); h ~ Normal(0, 0.2);
        phi ~ Gamma(2, 0.1).
      - **Fitting:** per bundle (one referee pool per country), on league matches final
        by the as-of (kickoff + 3 h) within 730 days. NUTS, 2,000 draws, refit every 28
        days on the dev Mondays (D14).
      - The two teams' counts are independent given the referee and teams. That's an
        assumption: the PPC below checks it, and a shared match term is the named fix,
        raised first.

      **Three nested variants, pre-registered in this order:**
      - **C0:** league-season level and home only (climatology with pooling).
      - **C1:** + a and b (team only).
      - **C2:** + s_ref (the referee). The item is about C2. The referee enters only if
        C2 beats C1.

      **(b) Horizons:**
      - **Headline T-60m:** the referee from the final record. Live, it is known by then
        in 80-100% of matches in every league.
      - **T-24h, reported:** the actual referee in every league except La Liga and
        Segunda, which draw from the pool (from the live timing above, not D26's
        optimistic rule).

      **(c) Inference gates, every refit:**
      - R-hat ≤ 1.01, bulk and tail ESS ≥ 400, no divergences;
      - PPC: dispersion of team cards and of total cards inside the posterior predictive
        95% band; the correlation between the two teams' counts reported;
      - prior sensitivity: σ priors halved and doubled, every referee's posterior mean
        moves by less than 1 posterior sd;
      - identifiability: the correlation between a and b reported.

      A failing refit falls back to the next simpler variant for that date and is
      counted. The chosen variant must pass at ≥ 97% of refits.

      **(d) Dev run.** Dev seasons 2019-2023, at T-60m.
      - **Score:** the RPS of the total-cards distribution (0-40, team caps at 20) per
        match, plus O/U 4.5 log loss and team O/U 1.5 log loss.
      - **B_clim:** each league's empirical distribution of team cards, home and away,
        over the same 730-day training window.

      **(e) Dev gate (all must pass):**
      1. **Coherence, exact:** the total distribution is the two team distributions
         convolved, and sums to 1 minus the cap's tail. O/U probabilities are monotone
         across lines (tests).
      2. **Variant chosen by total-cards RPS:** C1 replaces C0, and C2 replaces C1, only
         if the paired 95% interval of the mean difference is below 0.
      3. **Better than B_clim:** total-cards RPS and O/U 4.5 log loss, paired 95%
         interval below 0.
      4. **Calibrated:** ECE of O/U 3.5, 4.5 and 5.5 within its noise floor + 0.005.
      5. **Segments:** no league worse than B_clim by more than 1.0% on total-cards RPS,
         and, if C2 is chosen, none worse than C1 by more than 1.0%.
      6. **Leakage:** adding later matches changes no forecast. A referee's estimate
         reads only matches final by the as-of. Odds are not read.

      **Reported:** each variant's scores; σ_ref, σ_a, σ_b and phi over time; the
      referee effect for the T-24h pool leagues; RPS at T-24h; scores by league.

      **(f) Promotion window (owner choice 1).** On a dev pass, checks 1, 3 and 4 run
      once on a window never used for card work, with the chosen variant:
      - **(i) 2026/27 to date, fixed now (recommended).** Every in-scope 2026/27 match
        with kickoff before 2026-11-27 (1,269 matches). The card model refits on the
        same 28-day Mondays, with no Layer 2 dependency.
      - **(ii) Live only,** from the first shadow forecast, judged at 2,000 matches
        (around April 2027).
      - **(iii) Open the 2024/25-2025/26 holdout** for card outcomes (never used for
        cards). That would be the holdout's third opening.

      **(g) On a pass,** serving the card markets is its own item, with the dashboard
      and the market comparison where card odds are stored. **On a fail,** nothing is
      served.

      **Power (D59):**
      - Dev has about 19,000 matches, enough to detect a referee gain in RPS of a few
        tenths of a percent.
      - On window (i) (1,269 matches) the comparison is against climatology. There
        the team and referee terms together move about 10-15% of the cards' spread, so
        the expected gain is larger.
      - Recomputed from dev pairs before the window is read.

      **Cost:** 0 API requests. Small fits (about 90 team and 70 referee effects per
      bundle, about 1,500 matches): an estimated 20-60 s each, so about 1,000 fits for
      dev with the sensitivity runs. That is one backtest-type CI run of about 25 jobs,
      under an hour, one at a time, not on a Monday.

      **Owner choices:**
      1. **Promotion window:** (i) 2026/27 to date, fixed (recommended) / (ii) live only
         / (iii) the holdout's third opening.
      2. **Count likelihood:**
         - (a) negative binomial with a learned phi (recommended): Poisson is its limit,
           and the match-level ratio up to 1.26 includes referee and team spread the
           model explains;
         - (b) Poisson.
      3. **Cups:**
         - (a) league matches only now, with cups added in the tier-3 / cups-in-training
           work (item 5 of the agreed order) (recommended);
         - (b) domestic cup matches between two bundle sides in training now, which add
           referee matches.

      **Built 2026-11-27, not run.** Details fixed here, before any score:
      - **Table** (`adatta/features/cards.py`, built locally, `features/cards/v1/
        bf5eba3cbfc7fa50`): 76,408 team-matches; referee named on 99.7-100% per league.
        All three variants train on rows with a named referee, so they share their rows.
      - **Model** (`adatta/model/cards.py`):
        - NUTS 2,000 draws × 4 chains, target_accept 0.95 (P's); the league-season level
          centred and the team and referee terms non-centred; 400 draws kept. (Changed
          before any score: CI run 46014739829's recovery test found 3 divergences with
          target_accept 0.9 and a non-centred level.)
        - C2 refitted at prior ×0.5 and ×2 on every refit (the sensitivity check);
        - the PPC uses 200 draws.
      - **Scores** (`adatta/eval/cards.py`):
        - `rps_total` is the discrete CRPS of the total over 0-40 (unnormalised; only
          differences are read);
        - B_clim uses 0.5 pseudo-counts per value 0-20;
        - a refit failing (c) is replaced by the next simpler variant for that refit
          (C2 → C1 → C0 → B_clim). The gate reads these effective rows.
      - **Window (i)'s refits** train, as P3b-1's did, on the 730 days before each
        Monday, which reach into 2024/25 and 2025/26. Those are history for a live
        forecast, not holdout targets.
      - **Judging:** the judge step refuses to read window (i) unless the named dev run
        passed, and a store marker allows one reading.
      - **Tests:** 9 in `tests/test_cards.py`: table, leakage, holdout refusal,
        distributions, referee pool, scoring, PPC calibration, variant choice, fallback.
        2 in `tests/test_cards_model.py` (CI model job): each variant's terms, and
        recovery of six referee effects.
      - **Run:** `cards.yml`, stage dev: 25 jobs, about 60 fits each, estimated 1-2 hours.
        It runs after P3-j step 2's run, one at a time, not on a Monday.

      **Raised while building (2026-11-27), before any score: D67, drafted, not
      approved.**

      The recovery test in CI (runs 46014739829 and the next) and one real refit run
      locally (England as of 2021-01-25: 3,552 rows, 49 teams, 46 referees, 6
      league-seasons; diagnostics only, no forecast scored) found three things.

      1. **The learned league-season sd diverges.** With only 2-3 seasons per league in a
         730-day window, σ_season isn't identified: 3 divergences non-centred, 72
         centred. This is D21's reset problem again. **Proposed:** a fixed sd, δ ~
         Normal(0, 0.1). Yearly card levels moved about ±5% over 2016-2023. With it:
         - the recovery tests pass in the local container (0 divergences);
         - the real refit is clean in all three variants (R-hat ≤ 1.003, ESS ≥ 2,300,
           0 divergences);
         - C2's prior sensitivity moves no referee more than 0.04 sd.
      2. **Team cards are under-dispersed, and the two teams are positively linked.** At
         that refit, under C2:
         - the Pearson dispersion of team cards is 0.91;
         - the team-dispersion PPC quantile is 0.00 (C1 0.005);
         - the observed correlation of the two teams' counts is 0.14, against the
           model's 0.03;
         - the match-total PPC passes (quantile 0.70; C1 0.94).

         A negative binomial can't go below Poisson, and the draft's named fix (a shared
         match term) would add spread, making the team check worse. The two errors
         offset in the total, which is the headline market.

         **Proposed (as D48 did for the half split):**
         - the team-dispersion PPC is reported, not gating;
         - the total-dispersion PPC stays gating;
         - team O/U calibration is reported, and team markets aren't served without
           their own check;
         - same-game combinations of the two teams' cards are not claimed (§11.3).

         An under-dispersed likelihood (Conway-Maxwell-Poisson) with a match term is
         the larger alternative, left for later.
      3. **Fits are slower than the draft's estimate.** In the container a refit's five
         fits take about 16 minutes (C0 120 s, C1 207 s, C2 218 s, sensitivity about
         440 s). At 2,000 draws, bulk ESS was 2,300-3,000, against a gate of 400.
         **Proposed:** 1,000 draws × 4 chains (the fit time roughly halves, and ESS stays
         well above 400). The dev run is then about 25 jobs of 1.5-2 h, about 3-4 h in
         all.

      Nothing has been scored, and these change no metric or gate threshold. They're
      raised because 1 and 3 change approved settings and 2 changes what gates.

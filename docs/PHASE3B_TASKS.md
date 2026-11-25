# Phase 3b — half split, goal decomposition, discipline (spec §8.2, §8.3, §11, §19.3)

Goal: markets split by half and goal-type and discipline sub-models, built on the
production full-time model (`phase1b_strength_open_xg`) without changing it. Order: (1) the
half split for goals, (2) discipline and corners, (3) goal decomposition (penalty, own goal;
set pieces need another signal, SOURCE_FEASIBILITY.md).

Status (2026-11-25): **P3b-1 acceptance approved (owner: "Approve as drafted"); building.
Dev result: FAILS check 2 alone (the 0-0 PPC on six 2020 refits); checks 1, 3-5 pass.
Owner: "Amend, judge on (i)" (D50): re-gated on dev, PASSES; window (i) running.**

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

      **Then.** A serving acceptance is drafted after the promotion result, before
      anything is shown. It covers the markets in the same projection run as the 1X2,
      the match-page panel, and the coherence checks at serve time.

      **Cost:** 0 API requests and no Layer 2 refits for dev. About 180 small share fits
      (3 variants × about 60 dates), roughly 3 hours. They run as one backtest-type
      workflow, alone, and not on a Monday. Window option (i) adds about 65 Layer 2 NUTS
      refits in CI, as a second workflow after the dev one.

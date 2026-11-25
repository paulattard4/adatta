# Phase 3b — half split, goal decomposition, discipline (spec §8.2, §8.3, §11, §19.3)

Goal: markets split by half and goal-type and discipline sub-models, built on the
production full-time model (`phase1b_strength_open_xg`) without changing it. Order: (1) the
half split for goals, (2) discipline and corners, (3) goal decomposition (penalty, own goal;
set pieces need another signal, SOURCE_FEASIBILITY.md).

Status (2026-11-25): **P3b-1 acceptance drafted, awaiting the owner. Nothing built.**

Tick each item only when its acceptance check passes.

## Tasks

- [ ] **P3b-1 Half split for goals (§8.2).** Drafted 2026-11-25 before building; not
      approved yet. Two owner decisions are open (D46, D47, at the end of this item).

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

      **Then.** A serving acceptance is drafted after the promotion result, before
      anything is shown. It covers the markets in the same projection run as the 1X2,
      the match-page panel, and the coherence checks at serve time.

      **Cost:** 0 API requests and no Layer 2 refits for dev. About 180 small share fits
      (3 variants × about 60 dates), roughly 3 hours. They run as one backtest-type
      workflow, alone, and not on a Monday. Window option (i) adds about 65 Layer 2 NUTS
      refits in CI, as a second workflow after the dev one.

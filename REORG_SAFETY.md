# Reorg safety gate powered by VDF ticks (Chia‑style class group, 1024‑bit, \~200 B proofs)

### Summary

Introduce an **operator‑gated reorg policy** that uses **verified VDF tick counters** to distinguish deep reorg attacks from benign network splits. The policy:

* Triggers when the node is **online** and a proposed reorg is **deeper than 6 blocks** (configurable).
* Computes a **hashrate share** for each competing branch using **work per verified tick** since the LCA.
* Produces two independent signals:

  1. **Hashrate conservation** check (looks like a split vs. hash surge), and
  2. **Poisson catch‑up plausibility** (probability the competing fork could plausibly overtake).
* Surfaces a **single score and alert level** (green/amber/red/critical) and **requires manual operator consent** to follow the reorg in suspicious cases.

**Important:** Ticks are **validated** but **not used in fork choice**; this keeps consensus simple while adding defense‑in‑depth at policy layer.

---

## Motivation

* **Timestamps are gameable**; verified VDF ticks provide a **tamper‑evident lower bound** on elapsed sequential work between blocks.
* When a long reorg arrives (e.g., >6), nodes today generally follow “most work” blindly. The goal is a **sane default** that **stalls only when there’s statistical evidence of an attack**, while letting benign partitions converge automatically.

---

## Assumptions about the tick/VDF design

* VDF: **Chia‑style class group of unknown order**, discriminant size **1024**.
* Per‑header fields (already in this fork):

  * `nTick` — cumulative ticks since genesis (recommended: `uint64_t`).
  * `vdfProof` — **Wesolowski** proof (\~**200 bytes**).
* Typical squaring speeds: **150 k/s** (commodity single‑thread) to **1 M/s** (ASIC theoretical). This implies a **\~6.7× hardware spread**; treat ticks as a **noisy lower‑bound proxy for time**, not seconds.

Header size note: classic header 80 B → \~**280–290 B** with `nTick` + `vdfProof`. Compact blocks should be updated accordingly (see “Wire format” below).

---

## Scope

* **Consensus:** unchanged (still “most work”).
* **Validation:** verify `vdfProof` binds `(prev_header || nTick_{i-1}) → nTick_i` and `nTick` is strictly increasing; reject malformed/invalid proofs.
* **Policy:** reorg safety gate + alerts (this proposal).
* **UI/RPC:** surface operator prompts, scores, and audit logs.

---

## Design

### 1) Data computed at a proposed reorg

Let **LCA** be the last common ancestor, **A** the current branch, **B** the competing branch.

* **Work since LCA**
  `W_A = chainwork(A_tip) − chainwork(LCA)`
  `W_B = chainwork(B_tip) − chainwork(LCA)`

* **Verified ticks since LCA**
  `T_A = nTick(A_tip) − nTick(LCA)`
  `T_B = nTick(B_tip) − nTick(LCA)`

* **Rolling baseline at LCA** (robust estimator, window `n0`, e.g., 2,016 blocks before LCA):

  $$
  \hat H_0 = \frac{\sum \Delta W}{\sum \Delta T} \quad\text{with}\quad \Delta T\ \text{winsorized at p5/p95},\ \text{and}\ \Delta W = \text{per‑block work}.
  $$

  Interpretation: baseline **work per tick**.

* **Per‑branch hashrate estimates** (work per tick since LCA):

  $$
  \hat H_A = W_A / T_A,\qquad \hat H_B = W_B / T_B.
  $$

* **Conservation ratio** (“looks like a split if \~1”):

  $$
  R_{\text{total}} = (\hat H_A + \hat H_B) / \hat H_0.
  $$

* **Fork share** (used in Poisson model):

  $$
  q = \frac{\hat H_B}{\hat H_A + \hat H_B},\qquad p = 1-q.
  $$

* **Deficit z** (work‑equivalent confirmations for A since LCA):
  Prefer **work‑equivalent** $z^* = W_A / \overline W$ with $\overline W$ = robust mean work/block in the pre‑LCA window. (Fallback: block‑count deficit.)

### 2) Tick‑health and uncertainty handling

Ticks are only a **lower‑bound proxy for time** and vary with hardware (≈6.7× range).

* Compute **log‑domain outlier scores** on per‑block `ΔT`:

  * Maintain rolling **median** $m$ and **MAD** (median absolute deviation) of $\ln(\Delta T)$ over the last `N=2,016` blocks.
  * A block’s `ΔT` is **trusted** if $|\ln(\Delta T) - m| \le \kappa \cdot \text{MAD}$ with default $\kappa=3.5$.
* Define a branch‑level **tick quality index** $Q\in[0,1]$ = fraction of trusted `ΔT` since LCA (smoothed with EWMA).
* If either `T_A` or `T_B` fails trust (e.g., $Q<0.6$), **winsorize** extreme `ΔT` at p5/p95 and **de‑emphasize** tick‑based rates (see “Alert blending” below).

### 3) Signals and score

**Signal A — Hashrate conservation (partition‑likeness).**

* Expectation under a clean split: $R_{\text{total}} \approx 1$.
* Tolerance band (default): **±25%** → green if $0.75 \le R_{\text{total}} \le 1.25$.
* **Red flag** if $R_{\text{total}} > 1.5$ (apparent hashrate surge) or $<0.6$ (sharp drop/outage).

**Signal B — Poisson catch‑up plausibility.**
For attacker share $q<p$, LCA deficit $z$ (work‑equivalent), set $\lambda = z \cdot (q/p)$ and:

$$
P_{\text{catch}}(z,q)=\sum_{k=0}^{z-1}\frac{e^{-\lambda}\lambda^k}{k!}\left(\frac{q}{p}\right)^{z-k} \;+\; \sum_{k=z}^{\infty}\frac{e^{-\lambda}\lambda^k}{k!}.
$$

Equivalently,

$$
P_{\text{catch}}=1-\sum_{k=0}^{z-1}\frac{e^{-\lambda}\lambda^k}{k!}\left(1-\left(\frac{q}{p}\right)^{z-k}\right).
$$

If $q\ge p$, set $P_{\text{catch}}=1$.

Define a **log‑10 surprise score**:

$$
S_{\text{poisson}}=-\log_{10}\!\big(\max(P_{\text{catch}},10^{-12})\big).
$$

**Alert levels (statistically grounded):**

* **Green** (Informational): $S_{\text{poisson}}<1$  ( $P>10\%$ )
* **Amber** (Watch): $1\le S_{\text{poisson}}<2$  ( $1\%\!<\!P\le10\%$ )
* **Red** (Alert): $2\le S_{\text{poisson}}<4$  ( $0.01\%\!<\!P\le1\%$ )
* **Critical**: $S_{\text{poisson}}\ge4$ ( $P\le10^{-4}$ )

**Alert blending with tick quality $Q$.**
Compute a **blended surprise** using a backstop based on **timestamps (MTP)**:

* Let $S_{\text{mtp}}$ be the same score computed with $q$ derived from work per **MTP seconds** since LCA.
* Final score $S = Q\cdot S_{\text{poisson}} + (1-Q)\cdot S_{\text{mtp}}$.
* Final **alert level** derives from $S$ using the thresholds above.
* $Q$ is also **displayed** alongside a “Tick signal degraded” warning if $Q<0.6$.

### 4) Gate logic and defaults

* **Trigger:** node is **online** (saw a header within `τ_online = 2× target block time`) and **proposed reorg depth `d` > 6** (`-reorggate=6`).
* **Auto‑follow** (no operator prompt) **only if all**:

  1. $0.75 \le R_{\text{total}} \le 1.25$ (looks like a split),
  2. $q \ge 0.45$ (competing branch had near‑majority), and
  3. $S<1$ (statistically plausible).
* **Require operator consent** if **any** fail, or if $Q<0.6$.
* **Fail‑safe liveness:** if unattended for `-reorgautoresolve=30m` (configurable), auto‑resolve to **most work** (or an opt‑in **quorum witness** rule if configured).

### 5) Wire format (p2p)

* Extend `headers`/`block`/`cmpctblock` to carry `nTick` and `vdfProof`.
* For compact blocks, include `vdfProof` in the **Header Prefill**.
* Version‑gate with a new `NODE_VDFTICK` service bit to avoid negotiation mishaps.

### 6) CLI / Configuration

* `-reorggate=<N>` (default `6`): depth threshold to trigger gate.
* `-reorgautoresolve=<duration>` (default `30m`): unattended timeout.
* `-reorgsplitband=<fraction>` (default `0.25`): tolerance for $R_{\text{total}}$.
* `-reorgtickmadk=<float>` (default `3.5`): MAD cut‑off for tick outliers.
* `-reorgtickswin=<N>` (default `2016`): window for tick stats.
* `-reorgwitness=<uri,...>` (optional): external attestations for extra confidence.

### 7) RPC / UI

* **`getreorgassessment`** → JSON with:

  * `depth`, `delta_work`, `lca`, `branch_a_tip`, `branch_b_tip`
  * `T_A`, `T_B`, `H_A`, `H_B`, `H_0`, `R_total`, `q`, `Q`, `S_poisson`, `S_mtp`, `S_final`, `alert_level`
  * `recommendation`: `auto_follow | require_confirm | hold`
* **`reorgdecision`** `{"action": "accept"|"reject"|"defer"}`
* GUI/CLI prints a single‑screen triage, e.g.:

```
Proposed reorg d=9, Δwork=+1.8 blocks
R_total=1.62  (outside split band)      [RED]
q=0.27, S_poisson=2.3 (≈0.5%)           [RED]
Tick quality Q=0.88 (good)
Decision: REQUIRE OPERATOR
```

### 8) Performance

* Wesolowski verify is $O(\log T)$ class‑group ops per block; keep proofs \~200 B.
* Keep `nTick` as `uint64_t`: at 1 M ticks/s and 600 s blocks, **6×10^8 ticks/block**; over 100 years (\~5.26 M blocks) cumulative ≈ **3.1×10^15** ≪ 2^64.

### 9) Security considerations & gotchas (covered by this design)

* **Ticks ≠ time:** treat as lower‑bound proxy; use **robust stats**, **winsorization**, and **blending with MTP** when tick quality degrades.
* **Gaming `ΔT`:** p5/p95 clamping + MAD‑based outlier detection + **Q**.
* **Unknown‑order trapdoors:** ensure class‑group setup has no trapdoor; allow multi‑group in future (quorum proofs).
* **Eclipse/partition:** show peer diversity and (optional) witness attestations before operator approval.
* **Difficulty changes:** use **work‑equivalent** $z$, not just block counts.

---

## Pseudocode (scoring & alerts)

```c++
// Robust helpers
struct RobustStats { double median, mad; };

double LogPoissonPMF(int k, double lambda);     // stable log-space PMF
double CatchUpProbability(int z, double q) {
    double p = 1.0 - q;
    if (q >= p) return 1.0;
    double lambda = z * (q / p);
    // P = 1 - sum_{k=0}^{z-1} Poiss(k; lambda) * (1 - (q/p)^{z-k})
    double sum = 0.0;
    for (int k = 0; k < z; ++k) {
        double logpmf = LogPoissonPMF(k, lambda);
        double term = 1.0 - pow(q/p, z - k);
        sum += exp(logpmf) * term;
    }
    double P = 1.0 - sum;
    return std::max(0.0, std::min(1.0, P));
}

double SurpriseScore(double P) {
    const double floorP = 1e-12;
    return -log10(std::max(P, floorP));
}

struct ReorgAssessment {
    int depth;
    double delta_work_blocks;
    double TA, TB, HA, HB, H0, Rtotal, q, Q, S_poisson, S_mtp, S_final;
    std::string level; // GREEN/AMBER/RED/CRITICAL
    std::string recommendation; // auto_follow/require_confirm/hold
};

ReorgAssessment AssessReorg(const Branch& A, const Branch& B, const LCA& lca, const TickWindow& tw) {
    double WA = WorkSinceLCA(A, lca);
    double WB = WorkSinceLCA(B, lca);
    double TA = TicksSinceLCA(A, lca);
    double TB = TicksSinceLCA(B, lca);

    // Robust baseline
    double H0 = RobustWorkPerTickBefore(lca, tw);  // trimmed/winsorized

    // Winsorize ΔT extremes inside window before computing HA/HB
    double HA = WA / std::max(TA, 1.0);
    double HB = WB / std::max(TB, 1.0);

    double Rtotal = (HA + HB) / std::max(H0, 1e-30);
    double q = HB / std::max(HA + HB, 1e-30);

    int z = WorkEquivalentDeficit(A, lca); // use avg per-block work pre-LCA
    double P_tick = CatchUpProbability(z, q);
    double S_tick = SurpriseScore(P_tick);

    // Tick quality index Q in [0,1] from MAD on log(ΔT)
    double Qidx = TickQualityIndex(tw);

    // MTP-based fallback
    double q_mtp = WorkSharePerMTPSec(A, B, lca);
    double P_mtp = CatchUpProbability(z, q_mtp);
    double S_mtp = SurpriseScore(P_mtp);

    double S = Qidx * S_tick + (1.0 - Qidx) * S_mtp;

    std::string level =
        (S < 1.0) ? "GREEN" :
        (S < 2.0) ? "AMBER" :
        (S < 4.0) ? "RED" : "CRITICAL";

    bool inSplitBand = (Rtotal >= 0.75 && Rtotal <= 1.25);
    bool autoFollow = inSplitBand && (q >= 0.45) && (S < 1.0);

    return {
        .depth = BlocksReplaced(A),
        .delta_work_blocks = EquivalentBlocks(WB - WA),
        .TA = TA, .TB = TB, .HA = HA, .HB = HB, .H0 = H0,
        .Rtotal = Rtotal, .q = q, .Q = Qidx,
        .S_poisson = S_tick, .S_mtp = S_mtp, .S_final = S,
        .level = level,
        .recommendation = autoFollow ? "auto_follow" : "require_confirm"
    };
}
```

*Implementation notes:*

* Use **log‑space** PMF to avoid underflow for small $P$.
* Winsorize `ΔT` at p5/p95 before aggregations; compute MAD on `ln(ΔT)` for outlier detection.
* For z, prefer **work‑equivalent** using robust pre‑LCA `avg_work_per_block`.
* For headless builds, the operator prompt can be **RPC‑driven**; GUI shows the triage panel.
* Prometheus counters: `reorg_gate_triggered_total`, `reorg_gate_red_total`, `tick_quality_gauge`.

---

## Sensible defaults (tuned to the expected hardware spread)

* **Gate depth**: `6` (trigger on deeper reorgs).
* **Split band**: `±25%` around $R_{\text{total}}=1$.
* **Poisson alert thresholds**: as above (10%, 1%, 0.01%).
* **Tick outlier detection**: `κ=3.5` on MAD of `ln(ΔT)`; window `N=2,016`.
* **Tick quality floor**: if `Q<0.6`, show **“Tick signal degraded”** and blend 40% MTP.
* **Auto‑resolve timeout**: 30 minutes (configurable).
* **Safety clamps**: minimum `T_A,T_B ≥ 1` to avoid divide‑by‑zero; cap `S` at 12 for logging.

### Example ready‑made interpretations for operators

* **GREEN**: “Statistically plausible; looks like a network split; safe to follow.”
* **AMBER**: “Unlikely but possible; review peer diversity; consider waiting.”
* **RED**: “Very unlikely; possible rented hash/attack; manual decision required.”
* **CRITICAL**: “Extraordinary; do **not** auto‑follow without external confirmation.”

---

## Testing plan

1. **Unit tests**

   * Closed‑form cases for `CatchUpProbability()` (q≥p → 1; z=0 → 1; q→0 → $P\to 0$).
   * Numerical stability for small $P$ (down to 1e‑12).
   * Robust‑stats behavior on adversarial `ΔT` sequences (heavy tails).

2. **Property/fuzz**

   * Randomized branches with tick inflation/deflation, timestamp drift, difficulty changes.

3. **Simulations**

   * **60/40 partition**, reconcile after N blocks → should be GREEN/AMBER, mostly auto‑follow.
   * **Rented hash spike** taking over after depth 8 with $q≈0.25$ → RED/CRITICAL, prompt operator.
   * **Eclipse recovery** (A was isolated) → RED but high `R_total≈1`, operator can safely approve B.

4. **P2P**

   * Backward‑compat checks for nodes without `NODE_VDFTICK`.
   * Compact block relay with enlarged headers.

---

## Open questions

* Whether to support optional **multi‑group VDF** proofs (quorum) to harden against a single trapdoor.
* Whether the **auto‑resolve** fallback should be “most work” or require a **witness quorum** when configured.
* Whether telemetry should include a **peer diversity** (ASN/geo) snapshot in the triage UI.

---

## Acceptance criteria

* The node **prompts** the operator on reorgs deeper than `-reorggate` **unless** the event scores **GREEN** and passes the split‑band test.
* **RPC/GUI** exposes all metrics (`R_total`, `q`, `Q`, `S_*`) and a clear recommendation.
* **Logs** record the assessment and final decision with hashes and LCA.
* Under simulations, **false RED rates** on benign splits are <1% (tunable); **true RED** on contrived attacks is >95%.


---

### Notes on sizes & types

* `nTick`: `uint64_t` is ample headroom (≈3.1×10^15 ticks in 100 years at 1 M/s and 10‑min blocks).
* `vdfProof`: keep ≤256 B target (current \~200 B).
* Header serialization & compact blocks should be version‑gated to avoid accidental relay to legacy peers.


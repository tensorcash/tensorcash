# Quick Verification Process - Mathematical Theory & Implementation

## Overview

The quick verification process validates a proof-of-work submission through three main stages: block-level validation, parameter bounds checking, and sequence consistency verification. This document provides both mathematical foundations and references to the actual implementation.

---

## 1. Cryptographic Validation

### 1.1 Mathematical Specification

Let $\mathcal{H}: \{0,1\}^* \to \{0,1\}^{256}$ be a cryptographic hash function and $G$ be a group of unknown order. The VDF verification checks:

$$\text{VDF-Verify}(g, h, T, \pi) = \begin{cases} 
1 & \text{if } \pi \text{ proves } h = g^{2^T} \\
0 & \text{otherwise}
\end{cases}$$

The block header hash must satisfy:
$$\text{SHA256}(\text{SHA256}(\text{header\_prefix} \| \text{nonce})) < \text{target}$$

where $\text{nonce} = \text{hash}[0:32]$ and $\|$ denotes concatenation.

### 1.2 Implementation Reference

**File:** `services/verification-api/src/proof_verifier.py`

**VDF and Hash Validation** (Lines 3039-3144):
```python
# services/verification-api/src/proof_verifier.py:3039-3144
def _verify_block_sanity(self) -> bool:
    """Verify block-level proof-of-work."""
    with self.logger.verification_context(step="block_sanity_check"):
        try:
            # 1. Verify VDF
            vdf_valid = chiavdf_verify(
                self.proof['block_hash'],
                self.proof['vdf'],
                self.proof['tick']
            )
            
            if not vdf_valid:
                self.logger.error(
                    "VDF verification failed",
                    failure_type="vdf_verification_failure",
                    # ... error details
                )
                return False
                
            # 2. Check header_prefix + hash[:32] < target
            header_bytes = hex_to_bytes_tensor(self.proof['header_prefix'])
            header_data = torch.tensor(list(header_bytes), dtype=torch.uint8)
            
            # Extract nonce from hash
            hash_bytes = hex_to_bytes_tensor(self.proof['hash'])
            # ... validation logic continues
```

---

## 2. Parameter Space Validation

### 2.1 Mathematical Specification

Define the valid parameter space $\Theta$:

$$\Theta = \{(\kappa, \rho, \tau, \lambda) : \kappa \in (k_{\min}, k_{\max}], \rho \in (p_{\min}, 1], \tau \in (\tau_{\min}, \tau_{\max}), \lambda \in [1, \lambda_{\max}]\}$$

where:
- $\kappa$: top-k parameter
- $\rho$: top-p parameter  
- $\tau$: temperature
- $\lambda$: repetition penalty

**Entropy Constraint**: $\mathbb{E}[p_{\text{chosen}}] < \epsilon_{\text{entropy}}$ where $p_{\text{chosen}}$ are the probabilities of selected tokens.

### 2.2 Implementation Reference

**File:** `services/verification-api/src/proof_verifier.py`

**Parameter Bounds Validation** (Lines 3160-3188):
```python
# services/verification-api/src/proof_verifier.py:3160-3188
def _verify_parameters(self) -> bool:
    """Verify proof parameters are within bounds."""
    with self.logger.verification_context(step="parameter_validation"):
        checks = [
            (TOPK_MIN < self.top_k <= TOPK_MAX, f"top_k out of range: {self.top_k}"),
            (TOPP_MIN < self.top_p <= TOPP_MAX, f"top_p out of range: {self.top_p}"),
            (TEMP_MIN < self.temperature < TEMP_MAX, f"temperature out of range: {self.temperature}"),
            (REP_PENALTY < self.repetition_penalty <= 1, f"repetition_penalty out of range: {self.repetition_penalty}"),
        ]
        # ... validation logic
```

The entropy constraint above is enforced separately from the parameter-bounds
check, by `_verify_reuse_entropy` during sequence verification: the per-step
acceptance interval widths `[lower, upper]` are accumulated into a fixed-point
"reuse score" and rejected if they exceed a cap, bounding the realized entropy
of the sampled sequence.

---

## 3. Deterministic Sampling Verification

### 3.1 Mathematical Specification

For step $i$, the deterministic uniform value is:

$$u_i = \Phi^{-1}\left(\frac{\text{digest}_i \bmod 2^{64}}{2^{64}}\right)$$

where:
$$\text{digest}_i = \text{SHA256}(\text{header\_prefix} \| \text{vdf} \| \text{tick} \| i \| \text{ctx}_i \| \text{precision})$$

and $\text{ctx}_i$ contains the last $w$ tokens of the context at step $i$.

### 3.2 Implementation Reference

**File:** `services/verification-api/src/proof_verifier.py`

**Deterministic U-Value Generation** (Lines 2816-2836):
```python
# services/verification-api/src/proof_verifier.py:2816-2836
def _get_u(self, context_tokens, step_idx, hash_out=False):
    """Generate deterministic u value from context and step."""
    window_tokens = torch.zeros(self.window_size, dtype=torch.int64, device=self.device)
    context_len = min(len(context_tokens), self.window_size)
    window_tokens[-context_len:] = context_tokens[-context_len:]

    ctx_bytes = _tok_le_bytes(window_tokens.unsqueeze(0))
    j4 = _u32le(torch.tensor([step_idx], dtype=torch.uint32, device=self.device))
    T8 = _u32le(torch.tensor([self.proof['tick']], dtype=torch.uint32, device=self.device))
    precision_bytes = _str_bytes(self.stated_precision,
                                batch_size=ctx_bytes.size(0),
                                device=self.device)
    
    header_data = hex_to_bytes_tensor(self.proof['header_prefix'], device=self.device)
    v = hex_to_bytes_tensor(self.proof['vdf'], device=self.device)
    msg = _build_msg(header_data, v, T8, j4, ctx_bytes, precision_bytes)
    digest = sha256_many(msg)
    if hash_out:
        return digest[0].cpu().numpy().tobytes().hex()
    return _digest_to_u(digest).item()
```

---

## 4. Logit Processing Pipeline

### 4.1 Mathematical Specification

Given raw logits $\ell_i \in \mathbb{R}^V$ for step $i$:

**Temperature Scaling:**
$$\ell_i^{(1)} = \frac{\ell_i}{\tau}$$

**Repetition Penalty:**
$$\ell_i^{(2)}[j] = \begin{cases}
\ell_i^{(1)}[j] / \lambda & \text{if } j \in \text{ctx}_i \text{ and } \ell_i^{(1)}[j] > 0 \\
\ell_i^{(1)}[j] \cdot \lambda & \text{if } j \in \text{ctx}_i \text{ and } \ell_i^{(1)}[j] \leq 0 \\
\ell_i^{(1)}[j] & \text{otherwise}
\end{cases}$$

**Top-k Filtering:**
$$\ell_i^{(3)}[j] = \begin{cases}
\ell_i^{(2)}[j] & \text{if } \ell_i^{(2)}[j] \geq \ell_{(\kappa)} \\
-\infty & \text{otherwise}
\end{cases}$$

where $\ell_{(\kappa)}$ is the $\kappa$-th order statistic.

**Top-p Filtering:**
Let $\sigma$ be a permutation such that $\ell_i^{(3)}[\sigma(1)] \geq \ell_i^{(3)}[\sigma(2)] \geq \ldots$

Define cumulative mass: $c_j = \sum_{k=1}^j \text{softmax}(\ell_i^{(3)})_{\sigma(k)}$

$$\ell_i^{(4)}[j] = \begin{cases}
\ell_i^{(3)}[j] & \text{if } j = \sigma(k) \text{ and } c_{k-1} \leq 1-\rho \\
-\infty & \text{otherwise}
\end{cases}$$

### 4.2 Implementation Reference

**File:** `services/verification-api/src/proof_verifier.py`

**Sampling with Temperature, Repetition Penalty, Top-k/p** (Lines 2908-3033):
```python
# services/verification-api/src/proof_verifier.py:2908-3033
@pow_profiler
def _sample(self, idx_sent, val_sent, context_tokens, u, expected_lse=None, query_token=None):
    """Sample from logits using temperature, repetition penalty, top-k/p, and u."""
    
    # 1) Temperature scale
    temp_logits = val_sent / self.temperature
    
    # 2) Apply repetition penalty
    rep_pen = getattr(self, 'repetition_penalty', 1.0)
    if rep_pen != 1.0:
        mask_rep = torch.isin(idx_sent, context_tokens)
        temp_logits[mask_rep] /= rep_pen

    # 3) Apply top-k and top-p exactly as sampler does
    vals_sorted, idx_sorted = temp_logits.sort(dim=-1, descending=False)
    
    # Top-k: mask values strictly below the k-th largest
    k = getattr(self, 'top_k', None)
    if k is not None and vals_sorted.numel() > k:
        threshold = vals_sorted[-k]
        mask_k = vals_sorted <= threshold
        vals_sorted.masked_fill_(mask_k, -float('inf'))

    # Scatter the top-k result back into token-id order before top-p.
    temp_logits = torch.empty_like(vals_sorted).scatter(
        dim=-1, index=idx_sorted, src=vals_sorted)
    temp_logits = self._restore_argmax_if_empty(
        temp_logits, pre_trunc_logits, idx_sent)

    # Top-p: trim the post-top-k finite support in canonical
    # (logit desc, token id asc) order so the cut is deterministic.
    p = getattr(self, 'top_p', 1.0)
    if p < 1.0:
        temp_logits = self._apply_stable_top_p_support(
            temp_logits, idx_sent, p)
```

---

## 5. Probability Distribution and CDF

### 5.1 Mathematical Specification

The final token probabilities are:
$$p_i[j] = \frac{\exp(\ell_i^{(4)}[j])}{\sum_{k=1}^V \exp(\ell_i^{(4)}[k])}$$

**ID-Sorted CDF Construction:**
Let $\pi$ be the permutation sorting token IDs: $\text{id}_{\pi(1)} < \text{id}_{\pi(2)} < \ldots$

The cumulative distribution function is:
$$F_i(j) = \sum_{k=1}^j p_i[\pi(k)]$$

### 5.2 Sampling Consistency Verification

For expected token $t^*$ at position $\text{pos}$ in the ID-sorted order:

$$\text{Verify}_i \iff F_i(\text{pos}-1) < u_i \leq F_i(\text{pos})$$

### 5.3 Implementation Reference

**File:** `services/verification-api/src/proof_verifier.py`

**CDF Construction and Verification** (Lines 2958-2987):
```python
# services/verification-api/src/proof_verifier.py:2958-2987
# 4) Final normalization
log_Z = torch.logsumexp(temp_logits, dim=0)
probs = torch.exp(temp_logits - log_Z)

# 5) Build ID-sorted CDF in double for determinism
order = torch.argsort(idx_sent)
sorted_probs = probs[order]
cdf = torch.cumsum(sorted_probs.cpu(), dim=0)

# 6) Sample using u
pos = (cdf >= u).nonzero(as_tuple=True)[0][0].item()
sampled_token = idx_sent[order][pos].item()
sampled_prob = cdf[pos].item()

# Get CDF value for query token if requested
query_cdf = None
if query_token is not None:
    query_pos = (idx_sent[order] == query_token).nonzero(as_tuple=True)[0].cpu()
    lower = cdf[query_pos-1].item() if query_pos > 0 else 0.0
    upper = cdf[query_pos].item()
```

---

## 6. Vectorized Verification Algorithm

### 6.1 Mathematical Specification

For window size $W$, construct:
- $\mathbf{u} = [u_0, u_1, \ldots, u_{W-1}]^T \in [0,1]^W$
- $\mathbf{L} = [F_0(\text{pos}_0-1), F_1(\text{pos}_1-1), \ldots, F_{W-1}(\text{pos}_{W-1}-1)]^T$
- $\mathbf{U} = [F_0(\text{pos}_0), F_1(\text{pos}_1), \ldots, F_{W-1}(\text{pos}_{W-1})]^T$

**Global Acceptance Criterion:**
$$\text{QuickVerify} \iff \bigwedge_{i=0}^{W-1} \left(L_i < u_i \leq U_i\right) \land |\hat{u}_i - u_i| \leq 10^{-7}$$

where $\hat{u}_i$ is the recomputed deterministic value.

### 6.2 Implementation Reference

**File:** `services/verification-api/src/proof_verifier.py`

**Vectorized Sequence Verification** (Lines 4521-4612):
```python
# services/verification-api/src/proof_verifier.py:4521-4612
def verify_sequence_light_vectorized(self) -> bool:
    """Vectorized verification of all steps in the proof window."""
    with self.logger.verification_context(step="sequence_verification"):
        try:
            window_size = self.window_size
            
            # 1. Prepare all contexts
            all_contexts = []
            for i in range(window_size):
                context = torch.cat([self.prompt_tokens, self.chosen_tokens[:i]])
                all_contexts.append(context)

            # 2. Get all u values at once
            step_indices = torch.arange(window_size, dtype=torch.long, device=self.device)
            u_batch = self._get_u_batch(all_contexts, step_indices)
            
            # 3. Check u values
            expected_u = self.expected_u[:window_size]
            u_matches = torch.abs(u_batch - expected_u) <= 1e-7
            # ... validation continues
```

**Batched U-Value Generation** (Lines 4406-4446):
```python
# services/verification-api/src/proof_verifier.py:4406-4446
def _get_u_batch(self, all_contexts, step_indices):
    """Generate deterministic u values for multiple steps at once."""
    batch_size = len(all_contexts)

    # Build the windowed-token matrix [batch_size × window_size]
    window_tokens = torch.zeros(batch_size, self.window_size,
                                dtype=torch.int64, device=self.device)
    for i, ctx in enumerate(all_contexts):
        L = min(len(ctx), self.window_size)
        window_tokens[i, -L:] = ctx[-L:]

    # Encode everything exactly as in batch_sample_tokens
    ctx_bytes = _tok_le_bytes(window_tokens)
    j4 = _u32le(step_indices.view(-1, 1).to(torch.uint32))
    # ... batched message construction
```

---

## 7. Smell Test (Statistical Fingerprinting)

### 7.1 Mathematical Specification

**Inert Token Set Selection:**
Select a set $S \subset \{1, 2, \ldots, V\}$ of "inert tokens" based on variance criteria:

$$S = \arg\min_{|T|=K} \sum_{j \in T} \text{Var}(X_j)$$

subject to $\mathbb{E}[X_j] \in (0.001, 0.05)$ where $X_j$ is the frequency of token $j$ in top-50 lists.

**Mahalanobis Distance Tests:**
For observed token counts $\mathbf{c} \in \mathbb{N}^{|S|}$ and gap statistics $\mathbf{g} \in \mathbb{R}^{49}$:

$$M_{\text{count}} = (\mathbf{c} - \boldsymbol{\mu}_c)^T \boldsymbol{\Sigma}_c^{-1} (\mathbf{c} - \boldsymbol{\mu}_c)$$

$$M_{\text{gap}} = (\mathbf{g} - \boldsymbol{\mu}_g)^T \boldsymbol{\Sigma}_g^{-1} (\mathbf{g} - \boldsymbol{\mu}_g)$$

**Cosine Similarity Test:**
$$z = \text{atanh}(2\bar{r} - 1)$$

where $\bar{r}$ is the mean pairwise cosine similarity.

### 7.2 Implementation Reference

**File:** `services/verification-api/src/proof_verifier.py`

**Smell Test Validation** (Lines 2492-2605):
```python
# services/verification-api/src/proof_verifier.py:2492-2605
def _validate_topk_batch(self, topk_logits, topk_indices, stats):
    """Statistical PoW verifier with comprehensive validation."""
    
    # 1. Presence Test
    delta = counts_S - chunk_counts_mean
    maha = torch.einsum('i,ij,j->', delta, chunk_counts_cov_inv, delta).item()
    p_freq = (1.0 - Chi2(chunk_counts_cov.size(0)).cdf(torch.tensor(maha, device=device))).item()

    # 2. Gap Test
    gaps = (topk_logits[:, :-1] - topk_logits[:, 1:]).double()
    g_hat = gaps.mean(0)
    delta = g_hat - g_ref
    maha_gap = torch.einsum('i,ij,j->', delta, cov_inv, delta).item()
    p_gap = (1.0 - Chi2(49).cdf(torch.tensor(maha_gap, device=device))).item()

    # 3. Cosine-similarity Test
    c = F.embedding(topk_indices, emb_pca)                 # [N, 50, d]
    cos_mean = self._compute_dispersion_metrics_batched(c).mean()

    # Fisher z-transform
    r = (cos_mean * 2) - 1
    z_val = torch.atanh(r.clamp(-0.999_999, 0.999_999))
    # ... statistical test continues
```

**Statistics Collection** (Lines 2280-2491):
```python
# services/verification-api/src/proof_verifier.py:2280-2491
def _collect_logits_stats(self, seq_length=4, total_tokens=500_000,
                          batch_size=20, inert_topk=75, chunk_size=256):
    """Collect model fingerprinting statistics."""
    
    # Variance-based inert-token selection
    C = torch.cat(counts_per_chunk, dim=0).to(torch.float64)
    slots_per_prompt = C.sum(1, keepdim=True).clamp_min(1)
    P_mat = C / slots_per_prompt

    mu = P_mat.mean(0)
    var = P_mat.var(0, unbiased=False)
    cv = (var.sqrt() / mu.clamp_min(1e-12)).clamp(max=10)
    mask = (mu > 0.001) & (mu < 0.05)
    # ... token selection logic
```

---

## 8. Complete Quick Verification Interface

### 8.1 Implementation Reference

**File:** `services/verification-api/src/proof_verifier.py`

**Main Quick Verification Entry Point** (Lines 5855-5898):
```python
# services/verification-api/src/proof_verifier.py:5855-5898
def quick_verify(self, proof):
    try:
        d = pfunpack.unpack_validation_request(proof)['request']['pow_blob']
        self.initialise(d)

        with self.logger.verification_context(
            hash_id=d.get('hash'),
            verification_type="quick"
        ):
            if self._verify_block_sanity():
                if self._verify_parameters():
                    if self.verify_sequence_light_vectorized():
                        self.logger.info("Quick verification passed")
                        return ResponseValue.ResponseValue.Quick_OK
                    else:
                        self.logger.error("Quick verification failed at sequence verification")
                        return ResponseValue.ResponseValue.Quick_Fail
                # ... error handling
```

**Quick Verification with Smell Test** (Lines 5950-5999):
```python
# services/verification-api/src/proof_verifier.py:5950-5999
def quick_verify_smell_test(self, proof):
    # ... initialization and basic checks
    if self.verify_sequence_light_vectorized():
        if self.verify_sequence_smell_test():
            self.logger.info("Quick verification passed")
            return ResponseValue.ResponseValue.Quick_OK_Smell_OK
        else:
            self.logger.error("Quick verification failed at smell test")
            return ResponseValue.ResponseValue.Quick_OK_Smell_Fail
    # ... error handling
```

---

## 9. Computational Complexity

- **Hash Operations:** $\mathcal{O}(W)$ SHA-256 computations
- **Sorting:** $\mathcal{O}(W \cdot K \log K)$ where $K$ is average top-k size  
- **CDF Construction:** $\mathcal{O}(W \cdot K)$
- **Vectorized U-Generation:** $\mathcal{O}(W)$ with batched SHA-256
- **Total:** $\mathcal{O}(W \cdot K \log K)$ with efficient vectorization

The vectorized implementation achieves significant speedup while maintaining identical deterministic results as the sequential version.
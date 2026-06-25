```
TIP: 0004
Title: Model-operator blindness in verifiable proof-of-inference
Author: takakuni <takakuni@tensorcash.org>
Type: Informational
Status: Draft
Created: 2026-06-25
```

## Abstract

TensorCash makes proof-of-work *useful*: a miner runs AI inference and the
network re-checks the result cheaply. The current proof achieves trustless
verifiability by committing the inference in cleartext — the prompt token ids,
the sampled output token ids, and the per-step top-k logits are all published in
the block so any node can replay the seeded sampler. Verifiability and
confidentiality are therefore in direct tension: anything the network can re-run,
the network — and the miner operating the hardware — can also read. This TIP
frames the open research problem of **operator blindness**: deploying models
whose inference is intelligible and useful to the requesting party, yet whose committed
token-id footprint makes recovering the conversation's *semantic content*
cryptographically hard or computationally expensive. It establishes a negative
result and a positive direction. The negative result: hiding the tokenizer or
applying orthonormal transforms and row permutations to a public embedding
matrix is obfuscation, not confidentiality — such maps are recoverable up to,
and then through, an orthogonal symmetry without parallel data. The positive
direction: decouple the verifiable forward pass from token-frequency structure
(dynamic byte/patch models) and operate it over an entropy-equalized
representation (neural/arithmetic compression) so the public footprint
approaches a high-entropy, near-uniform stream. Each prospect is to be specified
in a future Standards-Track TIP; this document is non-normative.

## Motivation

TensorCash's distinguishing claim is that its proof-of-work is economically
useful: the cycles a miner spends are AI inference whose correctness the network
verifies, rather than hashing discarded immediately. The verification design is
deliberately transparent — the proof publishes the token ids and logits needed
to replay the inference, so checking is O(generated tokens) and requires no
trust in the miner. That transparency is the source of the protocol's
trustlessness and also the source of a hard limitation: **a verifiable proof is,
by construction, a readable proof.** A miner who runs the model, and every full
node that validates the block, can reconstruct the submitted prompt and the
model's response from the committed token ids whenever the tokenizer is known.

For a large class of intended workloads — confidential assistants, regulated or
proprietary data, "blind"/oblivious compute markets where a buyer rents compute
without disclosing what is being computed — that readability is disqualifying. A
credible **confidential compute market** is one of the most valuable things a
useful-PoW network could unlock at scale: it lets compute be sold without the
seller learning the workload. Today that market has only one practical building
block, the Trusted Execution Environment (TEE), and TEEs are an attestation, not
a self-verifying cryptographic proof — they move trust to a hardware vendor and
an attestation service rather than removing it. The question this TIP scopes is
whether the *representation the model computes over* can itself carry most of the
confidentiality, so that blindness does not depend wholly on either heavyweight
cryptography (too slow to be competitive) or hardware trust (not trustless).

This is a forward research framing. It proposes no consensus change and mandates
nothing. Its purpose is to fix the threat model, record a result that rules out a
tempting but unsound class of "solutions," and point implementers at the
directions worth a Standards-Track specification.

## Specification

This is an Informational TIP; the "specification" is a structured statement of
the problem, the design space, and the research prospects, with normative
guidance (RFC 2119) only where a class of approach must be ruled in or out.

### 1. Baseline: what the proof reveals today

The proof-of-inference object committed in a block carries, in cleartext:

- the **prompt token ids** and the **sampled output token ids** (one per
  generation step);
- the per-step **top-k logits and indices** and the sampler parameters
  (temperature, top-p, top-k, repetition penalty);
- a seeded uniform value `u` per step and a VDF output binding the proof to the
  header.

Reference points in the consensus node (repo-relative): the proof structure in
`src/primitives/proofblob.h`; the replay verifier in
`src/verification/quick_verifier.cpp`, enforced from `ConnectBlock` in
`src/validation.cpp`. The verifiable sampler lives in the inference-engine fork
(the proof-of-inference sampler in the `vllm` fork). The verifier does **not**
re-run a forward pass: it recomputes `u = H(header || vdf || tick || step ||
context_tokens || precision)`, rebuilds the CDF from the *committed* logits, and
checks that the claimed token is the inverse-CDF bucket for `u`. The security
model is "publish everything needed to re-check the seeded sampling cheaply."

**Threat model for blindness.** The adversary is the party that can read the
proof: the miner operating the hardware, and any full node. Define operator
blindness as: given the committed proof for an inference, the adversary cannot
recover the plaintext conversation except at a work factor that is large and
tunable. The requesting party (and any party holding the model's private decoding
material) retains an intelligible result. Two things leak even in the limit and
are out of scope to hide here — the *length*/step-count footprint and coarse
timing; these are addressed only by padding/batching, not by representation.

### 2. Negative result: obfuscating the embedding is not confidentiality

A natural first instinct is to keep the model's **tokenizer private** while its
**embedding matrix is public** (or vice versa), and/or to apply a secret linear
transform — an orthonormal rotation `Q` and a row permutation `P` — to the
embedding/unembedding so the published token ids no longer map to a known
vocabulary. This is **obfuscation, not encryption**, and the literature is
decisive on why.

- **High-dimensional dense representations leak their content.** Embedding
  inversion recovers text from vectors at near-lossless rates: vec2text recovers
  ~92% of 32-token inputs exactly (Morris et al., EMNLP 2023, arXiv:2310.06816);
  earlier work recovers 50–70% of input words (Song & Raghunathan, CCS 2020,
  arXiv:2004.00053). The *next-token distribution alone* leaks the upstream text
  (Language Model Inversion, Morris et al., ICLR 2024, arXiv:2311.13647). Hiding
  the surface symbols does not hide the geometry that carries meaning.
- **A secret orthonormal map is recoverable up to an orthogonal symmetry — for
  pocket change.** SVD on a model's API logits recovers its embedding/unembedding
  projection "up to orthogonal symmetry" (Stealing Part of a Production Language
  Model, Carlini et al., ICML 2024, arXiv:2403.06634). That is exactly the `Q`
  in a secret rotation: it cannot be hidden by being secret.
- **The residual orthogonal ambiguity is then resolved without parallel data.**
  Unsupervised cross-space alignment recovers an orthogonal mapping between two
  embedding spaces from distributional structure alone — adversarial
  initialisation followed by iterative Procrustes (MUSE; Conneau et al., ICLR
  2018, arXiv:1710.04087, 81.7% precision matching the supervised baseline) or
  robust self-learning (VecMap; Artetxe et al., ACL 2018, arXiv:1805.06297).
  Recovering `Q` "up to orthogonal symmetry" and then Procrustes-aligning the
  remainder is a complete reconstruction pipeline against a rotated embedding.
- **The row permutation `P` falls to frequency analysis.** Token-id frequencies
  and co-occurrence statistics are a fingerprint, exactly as letter frequencies
  break a substitution cipher. The *ordered* BPE merge list is a ranked
  frequency table that leaks training-data proportions (Data Mixture Inference,
  Hayase et al., NeurIPS 2024, arXiv:2407.16607); the same signal de-anonymizes a
  permuted vocabulary.

**Normative guidance.** A confidentiality scheme whose security rests solely on a
private tokenizer, a private linear/orthonormal transform of a public embedding
space, or a fixed permutation of token ids **MUST NOT** be treated as providing
cryptographic confidentiality. Such transforms **MAY** be documented as
obfuscation (raising constant-factor attacker effort) but **MUST NOT** be relied
upon where the threat model is a motivated operator with access to the model
family.

*Caveat (stated for honesty).* Unsupervised orthogonal recovery assumes the two
spaces are approximately isomorphic; it degrades under heavy under-training,
domain mismatch, or structurally different embedding algorithms (Søgaard et al.,
ACL 2018, arXiv:1805.03620; Vulić et al., EMNLP 2020, arXiv:2004.04070). This
narrows the attack in specific regimes but does not rehabilitate the approach as
a confidentiality primitive: the defender cannot guarantee non-isomorphism
without also degrading the model.

### 3. Why heavyweight cryptographic hiding is not competitive

If obfuscation is out, the question is whether genuine reconstruction-resistance
can be bought cryptographically at a price an open compute market will bear.
Current evidence says no:

- **Homomorphic encryption (HE) of transformer inference is orders of magnitude
  slower than plaintext.** Reported costs: BOLT, BERT ≈ 185–369 s on LAN (IEEE
  S&P 2024); BumbleBee, LLaMA-7B ≈ ~8 minutes per token (NDSS 2025); NEXUS, the
  fastest here, ≈ 37.3 s for BERT (NDSS 2025) — still seconds-to-minutes against
  milliseconds in the clear.
- **Secure multiparty computation (MPC) inference is in the same regime.** PUMA,
  LLaMA-7B ≈ ~5 minutes per token (arXiv:2307.12533); SecFormer improves on it
  by ~3.5× and remains seconds-to-minutes (ACL 2024).
- **Zero-knowledge ML (zkML) is no escape — it is as expensive or worse, and
  infeasible at state-of-the-art model sizes.** Proving a forward pass in
  zero-knowledge carries a prover blow-up comparable to or larger than HE/MPC;
  published zkML systems demonstrate correctness for small CNNs and sub-billion-
  parameter networks at prover times of seconds-to-minutes per inference and
  memory that scales with circuit size, and do not reach contemporary
  multi-billion-parameter transformer inference at practical cost. zkML also
  solves *verifiability under hiding* rather than confidentiality per se, so it
  does not remove the §2/§3 cost wall; it relocates it into the prover. It is
  recorded here as a ruled-out path, not a building block.
- **Federated / split inference is *not* a confidentiality shortcut.** Sharing
  gradients or activations leaks the inputs: Deep Leakage from Gradients
  reconstructs private data from shared gradients (Zhu et al., NeurIPS 2019,
  arXiv:1906.08935); GradInversion recovers batches of real images (Yin et al.,
  CVPR 2021, arXiv:2104.07586). Federated schemes only become confidential by
  paying the HE/MPC cost above.

A market that sells compute at a several-hundred-fold latency penalty is not a
market. This is why, in practice, the only deployable blindness primitive today
is the TEE.

### 4. TEE: the only currently-viable primitive, and why it is not the end state

Trusted Execution Environments (Intel TDX/SGX, AMD SEV-SNP, NVIDIA H100
Confidential Computing) run inference inside a hardware-attested enclave so the
operator's OS cannot read the workload. For compute-bound large-model inference
the *overhead is modest*: <7% for typical LLM queries on Hopper GPUs, trending to
near-zero as compute dominates the CPU↔GPU transfer (arXiv:2409.03992); 4–8% for
Llama-2-7B on H100 confidential mode, decreasing with batch size
(arXiv:2509.18886). The honest correction to a common assumption: **raw TEE
overhead is usually low for big-model inference; it is high only for I/O-heavy or
process-isolated configurations** (28–67% for some SGX/TDX I/O paths,
arXiv:2408.00443).

The limitation that prevents TEE from credibly carrying "blind compute at scale"
on its own is therefore **not** primarily overhead — it is the trust model. A TEE
gives an *attestation*, not a self-verifying cryptographic proof: confidentiality
rests on the silicon vendor, the firmware, the attestation service, and the
absence of exploitable side channels, all of which are live and unsettled threats
in the literature (SoK on hardware TEEs, arXiv:2205.12742). For a trustless
useful-PoW network the goal is blindness that does not reintroduce a trusted
third party. TEE is the right pragmatic floor for the near term; it should not be
the ceiling.

### 5. Positive direction: blindness in the representation the model computes over

The research bet of this TIP is to move most of the confidentiality into the
*representation*, so that the publicly committed footprint is intrinsically
high-entropy and the mapping back to plaintext requires private material the
network never sees. Two distinct properties are needed, and they are commonly
conflated — keeping them separate is the main technical contribution of this
framing.

**Property A — decouple compute from token-frequency structure (compute
flattening).** Dynamic byte/patch models make the forward pass cost a function of
*information content* rather than a fixed per-token cost. The Byte Latent
Transformer allocates compute by next-byte entropy via dynamic patches and scales
better than tokens at fixed FLOPs (Pagnoni et al., Meta FAIR, ACL 2025,
arXiv:2412.09871); MEGABYTE (Yu et al., NeurIPS 2023, arXiv:2305.07185) and
related byte models point the same way. The relevant consequence for blindness:
the per-inference *compute footprint* stops being a precise function of which
tokens appeared, becoming a function of how much "inference intelligence" the
input demanded.

> **Important and load-bearing caveat.** Patch-based byte models operate on **raw
> plaintext bytes**. They equalize *compute per patch*; they do **not** flatten
> the *value distribution* of the stream, and a frequency-analysis attack on the
> raw bytes is unaffected. Property A alone provides **no confidentiality**.
> Treating "entropy-based patching" as if it were "entropy coding" would be a
> category error.

**Property B — flatten the symbol distribution (distribution flattening).** True
entropy/arithmetic coding of the representation produces a near-uniform,
non-Zipfian symbol stream, which is exactly what defeats the frequency analysis
of §2. Training a model over neurally compressed text demonstrates both the
property and its cost: a small frozen byte model (the "M1" role) drives an
arithmetic coder whose output a larger model (the "M2" role) learns over;
arithmetic-coded text is near-uniform but **hard to learn**, and "Equal-Info
Windows" partially restore learnability while still trailing BPE at equal
parameters (Lester et al., Google DeepMind, ICLR 2025, arXiv:2404.03626). This is
the genuine basis for the claim that "compressed data destroys embedding and
tokenizer-frequency structure" — but it comes with a real learnability tax, and
flattening and learnability are in tension.

**The prospect is the composition, not either half.** Operate a verifiable
forward pass over a representation that is (A) patch/entropy-budgeted so the
committed footprint's *size and compute* track information content rather than
token identity, and (B) entropy-equalized so the committed *symbol values*
approach uniform and carry no exploitable frequency signal — with the
plaintext↔representation codec held privately by the requesting party (and the
model), never exposed to the network, so that even given the full proof an
operator must invert a learned compressor over a
near-uniform stream to recover semantics. The target is a *tunable work factor*
for reconstruction, not the brittle constant-factor bump that obfuscation buys.

#### Research prospects (each to be specified in a future Standards-Track TIP)

1. **Rethink what is committed on chain (longer-dated; the crux).** The lever is
   not heavier cryptography wrapped around a fixed cleartext commitment — §3 rules
   that out, zkML included. It is changing *what the proof commits to*. Re-express
   the §1 replay check so the committed footprint is over compressed/entropy-coded
   symbols rather than vocabulary token ids, with the private codec bound by a
   hiding commitment, such that the on-chain artifact is verifiable yet carries no
   exploitable semantic signal. Touches: proof-of-inference object and the replay
   verifier. Open problem: the seeded-sampler replay currently needs cleartext
   logits; a blind variant must make the same determinism checkable from a
   commitment without revealing the logits — and must do so without falling back
   on the infeasible zkML route.
2. **Compute-budgeted footprint (near-term, study).** Characterise, empirically,
   how much frequency signal survives in a patch-model's committed footprint, and
   whether Property A meaningfully raises attacker cost on its own (the §5 caveat
   says: expected to be little — this prospect is to *measure* it, not assume
   it). Touches: inference engine + proof schema; no consensus change to study.
3. **Entropy-equalized representation with a learnability budget (longer-dated).**
   Quantify the accuracy/cost of running useful assistant-grade inference over an
   equal-information-window representation, and the resulting reconstruction work
   factor. Touches: model training; informs whether (1) is worth a consensus
   change.

### 6. What would have to be true

The cost analysis forces a single conclusion: **the viable lever is what gets
committed on chain, not how heavily it is wrapped.** Every general-purpose
cryptographic route that hides a cleartext commitment after the fact — HE, MPC,
and zkML alike — is either non-competitive or infeasible at state-of-the-art
model sizes (§3). What remains is to make the committed artifact *itself* carry
no exploitable semantics while staying cheaply verifiable. That requires some
cleverness in the commitment, not a heavier outer envelope.

Concretely, a consensus-level blindness mechanism is only worth proposing if it
simultaneously: (a) preserves cheap public verifiability — the determinism of
the seeded sampler must remain checkable from whatever is committed, **without**
the per-inference prover cost of zkML; (b) keeps the reconstruction work factor
large and tunable under the §2 attacks applied to the *committed compressed*
footprint; (c) stays cost-competitive with cleartext inference within a small
constant; and (d) does not silently reintroduce a trusted third party. No known
construction meets all four. Identifying one — by changing what the proof commits
to — is the point of the prospects above.

## Rationale

The framing is deliberately split into a negative result and a positive
direction because the most likely failure mode for this problem is shipping
obfuscation (a private tokenizer or a secret rotation) and believing it is
confidentiality. §2 records, with citations, why that is unsound, so the project
does not relearn it.

TEE already exists and already solves operator confidentiality where its trust
model is acceptable; it is described in §4 as the existing baseline, not proposed
as research. The general-purpose cryptographic routes (HE, MPC, zkML) are
rejected on cost and feasibility (§3), not on security.

The representation-level direction is chosen because it is the only avenue that
could, in principle, deliver tunable confidentiality *without* a several-hundred-
fold cost penalty or a trusted third party — by making the artifact the network
commits to intrinsically uninformative rather than by encrypting a fixed
artifact. The two-property decomposition (compute flattening vs distribution
flattening) is emphasised because conflating them — in particular assuming
byte/patch models flatten frequencies — would produce a scheme that looks private
and is not.

## Backwards compatibility

None. This is an Informational TIP. It changes no consensus rule, wire format, or
RPC, and nodes that ignore it are unaffected. Any concrete mechanism arising from
§5 would be a separate Standards-Track TIP carrying its own activation and
backwards-compatibility analysis, including how the proof-of-inference object and
the replay verifier would change for non-upgraded nodes.

## Activation parameters

N/A (Informational).

## Reference implementation

TBD — Draft. This TIP records a research direction; reference implementations, if
any, attach to the future Standards-Track TIPs that specify individual prospects
in §5.

## Security considerations

- **Obfuscation must not be mistaken for confidentiality.** The central security
  statement of this TIP is §2: a private tokenizer, a secret orthonormal/linear
  transform of a public embedding space, or a token-id permutation are
  recoverable (embedding inversion; projection recovery up to orthogonal
  symmetry; unsupervised Procrustes alignment; frequency analysis) and provide no
  cryptographic confidentiality. Deploying such a scheme under a confidentiality
  claim would be actively harmful — it invites parties to transmit sensitive
  content believing it protected.
- **Verifiability vs confidentiality is a real constraint, not an oversight.**
  The current proof is readable *because* it is cheaply verifiable. Any blindness
  mechanism must show how verification survives once the cleartext token
  ids/logits are removed; "encrypt the proof" without a replacement verification
  path silently breaks consensus checkability.
- **Side channels persist under any representation.** Length/step-count and
  timing footprints leak coarse information regardless of how the symbols are
  encoded; mitigations are padding/batching at the protocol layer, out of scope
  for the representation and to be specified separately.
- **TEE trust assumptions are explicit.** The existing TEE baseline depends on
  the hardware vendor, firmware, attestation service, and side-channel resistance
  (arXiv:2205.12742). It is an attestation, not a trustless proof, and must be
  documented as such wherever offered.
- **No live-network weakness is asserted or disclosed here.** The transparency of
  the proof is an intended, documented property of verifiable PoW, not a defect;
  this TIP proposes forward research to extend it, not a fix to an exploitable
  flaw.

## Test vectors

N/A. This Informational TIP defines no wire format. Future Standards-Track TIPs
specifying §5 prospects will carry golden vectors for their proof and
verification changes.

## References

- Morris et al., *Text Embeddings Reveal (Almost) As Much As Text*, EMNLP 2023, arXiv:2310.06816
- Song & Raghunathan, *Information Leakage in Embedding Models*, CCS 2020, arXiv:2004.00053
- Morris et al., *Language Model Inversion*, ICLR 2024, arXiv:2311.13647
- Carlini et al., *Stealing Part of a Production Language Model*, ICML 2024, arXiv:2403.06634
- Conneau et al., *Word Translation Without Parallel Data* (MUSE), ICLR 2018, arXiv:1710.04087
- Artetxe et al., *A Robust Self-Learning Method for Fully Unsupervised Cross-Lingual Mappings*, ACL 2018, arXiv:1805.06297
- Hayase et al., *Data Mixture Inference: What do BPE Tokenizers Reveal?*, NeurIPS 2024, arXiv:2407.16607
- Søgaard et al., *On the Limitations of Unsupervised Bilingual Dictionary Induction*, ACL 2018, arXiv:1805.03620
- Vulić et al., *Are All Good Word Vector Spaces Isomorphic?*, EMNLP 2020, arXiv:2004.04070
- Pang et al., *BOLT*, IEEE S&P 2024
- Lu et al., *BumbleBee*, NDSS 2025
- Zhang et al., *NEXUS*, NDSS 2025
- Dong et al., *PUMA*, arXiv:2307.12533
- Luo et al., *SecFormer*, ACL 2024
- Zhu et al., *Deep Leakage from Gradients*, NeurIPS 2019, arXiv:1906.08935
- Yin et al., *See through Gradients: GradInversion*, CVPR 2021, arXiv:2104.07586
- Zhu et al., *Confidential Computing on NVIDIA Hopper GPUs*, 2024, arXiv:2409.03992
- Chrapek et al., *Confidential LLM Inference: CPU and GPU TEEs*, 2025, arXiv:2509.18886
- Coppolino et al., *Benchmarking SGX, SEV, and TDX*, 2024, arXiv:2408.00443
- Schneider et al., *SoK: Hardware-supported Trusted Execution Environments*, 2022, arXiv:2205.12742
- Pagnoni et al., *Byte Latent Transformer: Patches Scale Better Than Tokens*, ACL 2025, arXiv:2412.09871
- Yu et al., *MEGABYTE*, NeurIPS 2023, arXiv:2305.07185
- Lester et al., *Training LLMs over Neurally Compressed Text*, ICLR 2025, arXiv:2404.03626

## Copyright

This document is released into the public domain (CC0).

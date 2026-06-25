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
cryptographically hard or computationally expensive. Operator blindness is
strictly stronger than hiding the on-chain commitment: the operator runs the
forward pass, so a zero-knowledge proof — which hides only from the verifier — is
useful for the committed-artifact half but not sufficient; the operator must
additionally compute over data it cannot decode. It establishes a negative
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

Two facts make this a materially safer starting point than it first appears — and
than most on-chain AI designs. First, the protocol mandates publishing only the
*winning* inference: the proof-of-work win is inference-derived and rare, so
consensus commits on the order of one in millions of the inferences a miner runs,
not the whole inference space. The on-chain disclosure surface is therefore a
sparse, randomly selected sample of traffic rather than every input and output —
the overwhelming majority of conversations never touch the chain at all. Second,
everything beyond that one committed sample is **operator-side**, and the operator
surface is the miner's own to control: a miner on its own hardware, or inside a
TEE, decides how that plaintext is handled, because the protocol forces nothing
more than the single winning result onto the public ledger. Many on-chain AI
paradigms instead publish, or re-execute across every node, the inputs and outputs
of *every* inference; here the consensus-mandated surface is far smaller and the
remainder is a private infrastructure boundary rather than a broadcast. This is a
better baseline, not a solution: a winning inference is still committed in
cleartext today (§1), which is exactly the residue this TIP's research aims to
close.

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

**Threat model.** Distinguish two adversaries of increasing power. A *chain
observer* sees only what is committed on chain — today, the plaintext prompt and
output token ids and the logits, and only for the rare lottery-winning inferences
that become blocks: a sparse random sample of all inferences served, not the full
space. A *model operator* is strictly stronger: it runs
the forward pass, so it additionally sees the runtime inputs, outputs,
intermediate activations and memory, and any model or codec assets loaded to
serve the request. **Chain-observer blindness** means the committed proof reveals
no recoverable semantics. **Operator blindness** — the target of this TIP — is the
strictly stronger property that the party running the hardware also learns
nothing; it is achievable only if the request reaches the operator already
client-side-encoded and the operator never receives plaintext or the decoding
material. The goal: the adversary cannot recover the plaintext conversation
except at a work factor that is large and tunable, while the requesting party,
holding the private decoding material, retains an intelligible result. Two
channels leak regardless of representation and are out of scope — the
*length*/step-count footprint and coarse timing — addressed only by
padding/batching.

A property to preserve. The proof-of-work win is **inference-derived**: the
target is a double-SHA over a commitment built from the header, VDF, tick, and
the sampled output (itself a function of the forward-pass logits), and the header
nonce is taken from that hash (`CheckProofOfWork`, `src/validation.cpp`). A miner
cannot grind the target without running inference, so every failed attempt costs
a forward pass — this is what makes the work *useful*, and it must survive any
blinding redesign. Because only a winning inference is ever committed, a blinding
or proving scheme need apply to the winner alone, not to the discarded attempts.

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
  break a substitution cipher. By analogy, the *ordered* BPE merge list is a
  ranked frequency table from which training-data proportions are recoverable
  (Data Mixture Inference, Hayase et al., NeurIPS 2024, arXiv:2407.16607). That
  result does not itself de-anonymize a shuffled token-id vocabulary, but it
  illustrates the governing principle: token-frequency orderings are a recoverable
  fingerprint, and a permutation meant to hide content must defeat exactly that
  statistical signal over high-volume traffic.

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

### 3. Why cryptographic hiding is not the answer — and what zkML actually buys

If obfuscation is out, the question is whether genuine reconstruction-resistance
can be bought cryptographically at a price an open compute market will bear. For
the operator-blinding routes (HE, MPC) the evidence says no — they are orders of
magnitude too slow. zkML is the instructive exception: it is feasible at scale but
hides from the verifier rather than the operator, so it solves a different
problem. Taken in turn:

- **Homomorphic encryption (HE) of transformer inference is orders of magnitude
  slower than plaintext.** Reported costs: BOLT, BERT ≈ 185–369 s on LAN (IEEE
  S&P 2024); BumbleBee, LLaMA-7B ≈ ~8 minutes per token (NDSS 2025); NEXUS, the
  fastest here, ≈ 37.3 s for BERT (NDSS 2025) — still seconds-to-minutes against
  milliseconds in the clear.
- **Secure multiparty computation (MPC) inference is in the same regime.** PUMA,
  LLaMA-7B ≈ ~5 minutes per token (arXiv:2307.12533); SecFormer improves on it
  by ~3.5× and remains seconds-to-minutes (ACL 2024).
- **Zero-knowledge ML (zkML) is feasible at scale, but on the wrong axis and
  off-cadence.** Contrary to a common assumption, zkML now reaches
  state-of-the-art models: zkLLM (Sun, Zhang et al., CCS 2024, arXiv:2404.16109)
  proves a single 2048-token forward pass of a 13B-parameter transformer with a
  **~188 kB proof** ("< 200 kB"), **~1–3 s** verification, and **~13 min** proving
  on one GPU, plus a one-time **~11 MB** model-weight commitment. It does this by
  abandoning monolithic SNARKs (Groth16/Plonk, whose per-constraint elliptic-curve
  prover and global FFT make a 13B forward pass infeasible) for a sumcheck/GKR
  prover — linear-time, transparent, no per-circuit trusted setup — with lookup
  arguments (`tlookup`) and a specialised attention argument (`zkAttn`) absorbing
  the non-linear ops that otherwise wreck arithmetic circuits. Two facts matter
  here. First, **size is not the blocker**: a 188 kB proof sits well under the
  consensus `MAX_POW_BLOB_SIZE` (1,000,000 bytes, `src/consensus/consensus.h`) and
  would *replace* today's bulky cleartext logit arrays; the ~11 MB weight
  commitment is one-time per model and belongs at model registration, never per
  block. The difficulty predicate is likewise free — proving
  `SHA256d(commitment) < target` is ~50–60k constraints, under one part per
  million of a 13B forward pass, and in any case is better checked publicly on the
  commitment *outside* the circuit. Second, **zkML buys the wrong axis**: zkLLM is
  zero-knowledge toward the *verifier* (it hides the weights; the input is public
  to the protocol) while the *prover* holds weights and input in plaintext to run
  the forward pass. In a proof-of-inference network the miner is the prover and
  runs the model, so zkML yields *chain-observer* blindness — committing a succinct
  proof instead of plaintext token ids and logits — never *operator* blindness.
  Its proving latency (~13 min for one pass, multiplied per decode step for an
  autoregressive response) is also far off block cadence and off the milliseconds
  of native inference. zkML is therefore not a building block for operator
  blindness; it is the natural mechanism for the *commitment-side* redesign of §6,
  and a candidate for opt-in, issuer-borne use if its latency falls.
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
approach uniform and carry no exploitable frequency signal. The confidentiality
then hinges on one hard requirement, stated plainly: the plaintext↔representation
codec is held **only by the requesting party**, and the served model operates
**natively over the encoded representation** — accepting encoded input and
emitting encoded output, with the operator never possessing plaintext or the
codec. This is the crux and the principal open problem. If the operator (or the
model it runs) holds the codec, the operator can decode, and only chain-observer
blindness is achieved; operator blindness requires a model *trained to compute
over the encoded symbols directly*, so that even given the full proof an operator
must invert a learned compressor over a near-uniform stream to recover semantics.
The target is a *tunable work factor* for reconstruction, not the brittle
constant-factor bump that obfuscation buys.

#### Research prospects (each to be specified in a future Standards-Track TIP)

1. **Rethink what is committed on chain (longer-dated; the crux).** The lever is
   not heavier cryptography wrapped around a fixed cleartext commitment — §3 shows
   HE/MPC are non-competitive. It is changing *what the proof commits to*. A clean
   shape: hidden inference witness → a public hiding commitment and an
   inference-derived nonce → a public `SHA256d(commitment) < target` check that
   consensus performs directly → a succinct proof (e.g. zkML) that the public
   commitment came from a valid execution of the registered model. The double-SHA
   target test should stay *outside* the proof: with a public commitment,
   consensus hashes it directly; placing the comparison in-circuit is cheap
   (~50–60k constraints, §3) but unnecessary unless the preimage relation must
   itself stay hidden. Touches: proof-of-inference object and the replay verifier.
   Caveat: this delivers chain-observer blindness only — it must be combined with
   an operator-blinding primitive (the Property A/B native codec, or TEE) to reach
   the goal of this TIP.
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
4. **Opt-in zk-committed inference, issuer-borne (longer-dated; contingent on zkML
   latency falling).** Should zkML proving become cheap enough, a model issuer
   could *opt in* to a blind, zk-committed inference mode for its model and bear
   the extra proving cost and block-cadence latency itself, rather than imposing it
   network-wide. This confines the off-cadence cost (§3) to the issuers who want
   the property and leaves the default fast path unchanged. It still delivers only
   chain-observer blindness unless paired with prospect (1)/(3). Touches: model
   registration (a per-model mode flag and a one-time weight-commitment reference)
   and the proof object; no change to non-participating models.

### 6. What would have to be true

The cost analysis forces a single conclusion: **the viable lever is what gets
committed on chain, not how heavily it is wrapped.** HE and MPC remain
non-competitive (§3). zkML is the exception worth stating precisely: it is
*feasible* at state-of-the-art sizes (zkLLM proves a 13B model with a ~188 kB
proof) and is the natural mechanism for the commitment-side half — but it
delivers *chain-observer* blindness, not *operator* blindness, because the prover
runs the model over plaintext, and its proving latency is off-cadence. So the
on-chain commitment can be made to carry no exploitable semantics; making the
*operator* blind is the separate, harder half.

Concretely, an operator-blindness mechanism is only worth proposing if it
simultaneously: (a) preserves cheap public verifiability — the determinism of the
seeded sampler must remain checkable from whatever is committed, without imposing
the per-inference, off-cadence prover cost of zkML on the default path; (b) keeps
the reconstruction work factor large and tunable under the §2 attacks applied to
the *committed compressed* footprint; (c) stays cost-competitive with cleartext
inference within a small constant; and (d) keeps the operator itself blind —
computing over encoded inputs without ever holding plaintext or the codec — which
no purely commitment-side tool, zkML included, achieves on its own. No known
construction meets all four. Identifying one is the point of the prospects above.

## Rationale

The framing is deliberately split into a negative result and a positive
direction because the most likely failure mode for this problem is shipping
obfuscation (a private tokenizer or a secret rotation) and believing it is
confidentiality. §2 records, with citations, why that is unsound, so the project
does not relearn it.

TEE already exists and already solves operator confidentiality where its trust
model is acceptable; it is described in §4 as the existing baseline, not proposed
as research. HE and MPC are rejected on cost (§3), not on security. zkML is not
rejected but re-scoped: it is feasible at scale and is the natural commitment-side
mechanism (chain-observer blindness), yet it does not by itself blind the operator
and is off-cadence — so it is treated as a building block for the on-chain half,
optionally issuer-borne, not as the answer.

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
- **A succinct proof hides from the verifier, not the prover.** zkML (e.g.
  zkLLM) is zero-knowledge toward the chain and other nodes, but the miner
  generating the proof holds the model and the plaintext to run the forward pass.
  Committing a zk proof instead of cleartext tokens therefore upgrades
  *chain-observer* confidentiality only; it MUST NOT be described as protecting
  content from the operator.
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
- Sun, Zhang et al., *zkLLM: Zero Knowledge Proofs for Large Language Models*, ACM CCS 2024, arXiv:2404.16109
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

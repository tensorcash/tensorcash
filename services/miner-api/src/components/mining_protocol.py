"""Phase 3 — typed broker-mining wire protocol (worker side).

Mirrors the broker's pydantic schema at
``the Compute Broker``. Two services live
in different submodules so the schema is duplicated; structural
JSON compatibility is what matters, not Python object sharing.

This module is dataclasses-only (zero deps) so it imports cleanly under
the worker's minimal runtime.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums (string-valued)
# ---------------------------------------------------------------------------

# Per-job mining behaviour.
MINING_MODE_DISABLED = "disabled"
MINING_MODE_DUMMY_ONLY = "dummy_only"
MINING_MODE_REQUEST_ATTACHED = "request_attached"
_VALID_MINING_MODES = frozenset({
    MINING_MODE_DISABLED, MINING_MODE_DUMMY_ONLY, MINING_MODE_REQUEST_ATTACHED,
})

# Where the broker forwards a solved block. The worker never decides;
# echoing this lets the worker refuse a request that contradicts its
# local config (e.g. broker said `disabled` but sent template anyway).
SUBMIT_POLICY_CORE_NODE = "tensorcash_core_node"
SUBMIT_POLICY_CLIENT = "client_core_node"
SUBMIT_POLICY_RETURN_TO_CLIENT = "return_to_client"
SUBMIT_POLICY_DISABLED = "disabled"
_VALID_SUBMIT_POLICIES = frozenset({
    SUBMIT_POLICY_CORE_NODE, SUBMIT_POLICY_CLIENT,
    SUBMIT_POLICY_RETURN_TO_CLIENT, SUBMIT_POLICY_DISABLED,
})


class MiningProtocolError(ValueError):
    """Raised on schema validation failures.

    Worker callers translate this into an ``error="invalid_payload"``
    MineResult so the broker's lease can close cleanly.
    """


# ---------------------------------------------------------------------------
# Nested objects
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MiningModelMeta:
    name: str
    commit: str
    registry_height: Optional[int] = None
    model_hash: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Any) -> "MiningModelMeta":
        if not isinstance(raw, dict):
            raise MiningProtocolError(f"model: expected object, got {type(raw).__name__}")
        try:
            return cls(
                name=str(raw["name"]).strip(),
                commit=str(raw["commit"]).strip(),
                registry_height=(int(raw["registry_height"]) if raw.get("registry_height") is not None else None),
                model_hash=(str(raw["model_hash"]).strip() if raw.get("model_hash") is not None else None),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MiningProtocolError(f"model: {exc}") from exc


@dataclass(frozen=True)
class MiningTemplate:
    template_id: str
    request_id: int
    block_hash: str
    header_prefix: str
    target: str
    bits: int
    expires_at: int
    # Slice 11: broker-supplied UNADJUSTED share target. The
    # worker applies the chosen model's difficulty adjustment
    # before mining/sampling to get ``adjusted_share_target``:
    #   adjusted_share_target = floor(base_share_target * N / D)
    # where N is the chain ``ModelDifficultyNormalizer`` consensus
    # constant and D is ``model.difficulty`` from the broker-pinned
    # registry snapshot. The proof FlatBuffer remains bound to the
    # model-adjusted chain (block) target — share is a
    # CLASSIFICATION, never re-mining at a different difficulty.
    #
    # Optional so an older broker (pre-slice-11) still parses
    # cleanly. The current broker always sends ``base_share_target``
    # via its outbound JSON (see
    # ``the Compute Broker``).
    # The pre-slice-11 field name was ``share_target``; both names
    # are accepted on the wire for compatibility during rollout.
    base_share_target: Optional[str] = None
    share_shift_bits: Optional[int] = None

    @classmethod
    def from_dict(cls, raw: Any) -> "MiningTemplate":
        if not isinstance(raw, dict):
            raise MiningProtocolError(f"template: expected object, got {type(raw).__name__}")
        try:
            # Accept the new ``base_share_target`` field first; fall
            # back to the legacy ``share_target`` key during rollout
            # so a worker running new code against an older broker
            # build still parses cleanly. Once every broker is past
            # slice 11 the fallback can be dropped.
            base_share_target = (
                raw.get("base_share_target") or raw.get("share_target")
            )
            share_shift_bits = raw.get("share_shift_bits")
            return cls(
                template_id=str(raw["template_id"]),
                request_id=int(raw["request_id"]),
                block_hash=str(raw["block_hash"]),
                header_prefix=str(raw["header_prefix"]),
                target=str(raw["target"]),
                bits=int(raw["bits"]),
                expires_at=int(raw["expires_at"]),
                base_share_target=str(base_share_target) if base_share_target else None,
                share_shift_bits=int(share_shift_bits) if share_shift_bits is not None else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MiningProtocolError(f"template: {exc}") from exc

    def __post_init__(self):
        # Header prefix must be 76 bytes hex == 152 chars per the broker
        # contract. Catching this on the worker stops malformed jobs from
        # reaching the FlatBuffer construction step.
        if len(self.header_prefix) != 152:
            raise MiningProtocolError(
                f"template.header_prefix must be 152 hex chars (76 bytes); got {len(self.header_prefix)}"
            )
        try:
            bytes.fromhex(self.header_prefix)
        except ValueError as exc:
            raise MiningProtocolError(f"template.header_prefix not valid hex: {exc}") from exc
        if self.request_id < 1:
            raise MiningProtocolError(f"template.request_id must be >= 1; got {self.request_id}")


_MAX_TARGET_256 = (1 << 256) - 1


def derive_adjusted_share_target(
    *,
    base_share_target_hex: str,
    normalizer: int,
    difficulty: int,
) -> str:
    """Slice 11 — compute the model-adjusted share threshold.

    Mirrors the broker's
    ``VerifyServiceShareClient._compute_adjusted_target`` (see
    ``the Compute Broker``)
    BIT-EXACTLY. Worker and broker MUST produce the same value
    given the same inputs; if they ever diverge the broker's
    share-mode verify-service round-trip would reject the
    worker's emissions as ``above_share_target``.

    Formula matches bcore consensus
    (``src/node/miner.cpp`` ~L233): bcore's split-and-recombine
    in ``arith_uint256`` is an overflow workaround, not a
    different equation. Python ints give the same answer in one
    step.

    Saturation: cap at ``2**256 - 1`` (the absolute 256-bit upper
    bound). bcore caps at the chain's ``powLimit`` for the
    BLOCK-target equivalent; share thresholds are off-chain
    accounting and not subject to that bound.

    Raises ``MiningProtocolError`` on any input that prevents a
    well-formed adjusted target — caller is expected to skip
    share emission for that proof, not silently substitute a
    default.
    """
    if not base_share_target_hex or not isinstance(base_share_target_hex, str):
        raise MiningProtocolError(
            "derive_adjusted_share_target: base_share_target_hex must be non-empty hex"
        )
    try:
        base = int(base_share_target_hex, 16)
    except ValueError as exc:
        raise MiningProtocolError(
            f"derive_adjusted_share_target: base_share_target_hex not valid hex: {exc}"
        ) from exc
    if normalizer is None or int(normalizer) <= 0:
        raise MiningProtocolError(
            f"derive_adjusted_share_target: normalizer must be > 0; got {normalizer!r}. "
            "Source from the chain's ModelDifficultyNormalizer (env "
            "MODEL_DIFFICULTY_NORMALIZER)."
        )
    if difficulty is None or int(difficulty) <= 0:
        raise MiningProtocolError(
            f"derive_adjusted_share_target: difficulty must be > 0; got {difficulty!r}"
        )
    adjusted = (base * int(normalizer)) // int(difficulty)
    if adjusted > _MAX_TARGET_256:
        adjusted = _MAX_TARGET_256
    return f"{adjusted:064x}"


@dataclass(frozen=True)
class MiningPolicy:
    submit_policy: str = SUBMIT_POLICY_CORE_NODE
    max_parallel: int = 1
    user_inference_blocks_on_mining: bool = False

    @classmethod
    def from_dict(cls, raw: Any) -> "MiningPolicy":
        if raw is None:
            return cls()
        if not isinstance(raw, dict):
            raise MiningProtocolError(f"policy: expected object, got {type(raw).__name__}")
        sp = str(raw.get("submit_policy", SUBMIT_POLICY_CORE_NODE))
        if sp not in _VALID_SUBMIT_POLICIES:
            raise MiningProtocolError(f"policy.submit_policy unsupported: {sp!r}")
        try:
            return cls(
                submit_policy=sp,
                max_parallel=int(raw.get("max_parallel", 1)),
                user_inference_blocks_on_mining=bool(raw.get("user_inference_blocks_on_mining", False)),
            )
        except (TypeError, ValueError) as exc:
            raise MiningProtocolError(f"policy: {exc}") from exc


# ---------------------------------------------------------------------------
# Top-level MINE_REQUEST / MINE_RESULT
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MineRequest:
    job_id: str
    work_unit_id: int
    wallet_id: str
    network: str
    mode: str
    model: MiningModelMeta
    template: MiningTemplate
    policy: MiningPolicy = field(default_factory=MiningPolicy)
    type: str = "MINE_REQUEST"

    @classmethod
    def from_dict(cls, raw: Any) -> "MineRequest":
        if not isinstance(raw, dict):
            raise MiningProtocolError(f"MINE_REQUEST: expected object, got {type(raw).__name__}")
        if raw.get("type", "MINE_REQUEST") != "MINE_REQUEST":
            raise MiningProtocolError(f"MINE_REQUEST: type must be 'MINE_REQUEST'; got {raw.get('type')!r}")
        try:
            mode = str(raw["mode"])
            if mode not in _VALID_MINING_MODES:
                raise MiningProtocolError(f"MINE_REQUEST.mode unsupported: {mode!r}")
            inst = cls(
                job_id=str(raw["job_id"]),
                work_unit_id=int(raw["work_unit_id"]),
                wallet_id=str(raw["wallet_id"]),
                network=str(raw["network"]),
                mode=mode,
                model=MiningModelMeta.from_dict(raw["model"]),
                template=MiningTemplate.from_dict(raw["template"]),
                policy=MiningPolicy.from_dict(raw.get("policy")),
            )
        except KeyError as exc:
            raise MiningProtocolError(f"MINE_REQUEST: missing field {exc}") from exc
        except (TypeError, ValueError) as exc:
            raise MiningProtocolError(f"MINE_REQUEST: {exc}") from exc
        if inst.work_unit_id != inst.template.request_id:
            raise MiningProtocolError(
                f"MINE_REQUEST.work_unit_id ({inst.work_unit_id}) does not match template.request_id "
                f"({inst.template.request_id})"
            )
        return inst


@dataclass
class MineResult:
    job_id: str
    work_unit_id: int
    wallet_id: str
    network: str
    template_id: str
    request_id: int
    type: str = "MINE_RESULT"
    agent_id: Optional[str] = None
    worker_id: Optional[str] = None
    model_identifier: Optional[str] = None
    nonce: Optional[int] = None
    vdf_tick: Optional[int] = None
    solution_b64: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        # Drop Nones so the wire shape matches the broker side.
        return {k: v for k, v in out.items() if v is not None}

    @classmethod
    def from_request(
        cls,
        request: MineRequest,
        *,
        worker_id: Optional[str] = None,
        agent_id: Optional[str] = None,
    ) -> "MineResult":
        """Build a MineResult template from an in-flight MineRequest.

        Pre-populates the correlation fields. Callers fill in
        ``solution_b64``/``error`` and the optional metadata fields.
        """
        return cls(
            job_id=request.job_id,
            work_unit_id=request.work_unit_id,
            wallet_id=request.wallet_id,
            network=request.network,
            template_id=request.template.template_id,
            request_id=request.template.request_id,
            agent_id=agent_id,
            worker_id=worker_id,
        )


@dataclass
class MineShare:
    """Worker → broker hashrate-sampling share. Same FlatBuffer
    payload (``proof_b64``) the worker would send for a block-level
    solution, but generated when the digest met the broker-supplied
    ``share_target`` instead of the chain ``target``.

    ``achieved_hash`` is the canonical PoW digest in display-endian
    hex (matches the bcore convention). ``share_target`` is echoed
    from the lease so the broker can detect protocol violations
    (worker reporting a different target than the one it was
    instructed to mine against).
    """

    job_id: str
    work_unit_id: int
    wallet_id: str
    network: str
    template_id: str
    request_id: int
    nonce: int
    achieved_hash: str
    share_target: str
    type: str = "MINE_SHARE"
    agent_id: Optional[str] = None
    worker_id: Optional[str] = None
    model_identifier: Optional[str] = None
    vdf_tick: Optional[int] = None
    share_difficulty_weight: Optional[str] = None
    proof_b64: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        out = asdict(self)
        return {k: v for k, v in out.items() if v is not None}

    @classmethod
    def from_request(
        cls,
        request: MineRequest,
        *,
        nonce: int,
        achieved_hash: str,
        share_target: str,
        worker_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        proof_b64: Optional[str] = None,
        vdf_tick: Optional[int] = None,
    ) -> "MineShare":
        return cls(
            job_id=request.job_id,
            work_unit_id=request.work_unit_id,
            wallet_id=request.wallet_id,
            network=request.network,
            template_id=request.template.template_id,
            request_id=request.template.request_id,
            nonce=nonce,
            achieved_hash=achieved_hash,
            share_target=share_target,
            agent_id=agent_id,
            worker_id=worker_id,
            vdf_tick=vdf_tick,
            proof_b64=proof_b64,
        )


__all__ = [
    "MineRequest",
    "MineResult",
    "MineShare",
    "MiningModelMeta",
    "MiningPolicy",
    "MiningProtocolError",
    "MiningTemplate",
    "MINING_MODE_DISABLED",
    "MINING_MODE_DUMMY_ONLY",
    "MINING_MODE_REQUEST_ATTACHED",
    "SUBMIT_POLICY_CORE_NODE",
    "SUBMIT_POLICY_CLIENT",
    "SUBMIT_POLICY_DISABLED",
    "SUBMIT_POLICY_RETURN_TO_CLIENT",
]

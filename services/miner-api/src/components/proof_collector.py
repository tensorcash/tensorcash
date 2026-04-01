"""
ZMQ PULL listener to collect MiningResponse FlatBuffers from workers,
extract completion_id from MiningResponse, and cache blobs
by completion_id for a limited TTL.

W4 Mining Sidecar:
- Optional solution_callback for broker mode
- Extracts req_id from MiningResponse to match with MINE_REQUEST job_id
- Forwards solutions to broker via worker_client.MINE_RESULT
"""
import random
import threading
import logging
import zmq
import json
import re
from typing import Optional, Callable, Tuple

from proof import MiningResponse as FBMiningResponse
from proof import Proof as FBProof

from . import constants
from .proof_cache import ProofCache

logger = logging.getLogger(__name__)


def _extract_completion_id(mining_buf: bytes) -> Optional[str]:
    try:
        mr = FBMiningResponse.MiningResponse.GetRootAs(mining_buf, 0)
        # First try to get completion_id directly from MiningResponse
        completion_id = mr.CompletionId()
        if completion_id:
            return completion_id.decode('utf-8') if isinstance(completion_id, bytes) else completion_id

        # Fallback: try to extract from extra_flags for backward compatibility
        proof = mr.PowBlob()
        if proof is None:
            return None
        extra = proof.ExtraFlags()
        if not extra:
            return None
        try:
            data = json.loads(extra)
            cid = data.get("completion_id")
            if isinstance(cid, str) and cid:
                return cid
        except Exception:
            # not JSON? attempt to parse key=value
            for part in extra.split(";"):
                if part.strip().startswith("completion_id="):
                    return part.split("=", 1)[1].strip()
    except Exception as e:
        logger.debug(f"Failed to extract completion_id: {e}")
        return None
    return None


def _extract_proof_purpose(mining_buf: bytes) -> Optional[str]:
    """Extract the explicit ``proof_purpose`` marker from
    ``Proof.extra_flags`` (a JSON dict serialized from the writer's
    ``model_config_diff``). Mining proofs from older workers don't carry
    it — None means mining."""
    try:
        mr = FBMiningResponse.MiningResponse.GetRootAs(mining_buf, 0)
        proof = mr.PowBlob()
        if proof is None:
            return None
        extra = proof.ExtraFlags()
        if not extra:
            return None
        if isinstance(extra, bytes):
            extra = extra.decode("utf-8")
        try:
            data = json.loads(extra)
            purpose = data.get("proof_purpose")
            if isinstance(purpose, str) and purpose:
                return purpose
        except Exception:
            # not JSON? attempt to parse key=value (parity with
            # _extract_completion_id's fallback format)
            for part in extra.split(";"):
                if part.strip().startswith("proof_purpose="):
                    return part.split("=", 1)[1].strip()
    except Exception as e:
        logger.debug(f"Failed to extract proof_purpose: {e}")
    return None


def _extract_req_id(mining_buf: bytes) -> Optional[int]:
    """Extract req_id from MiningResponse FlatBuffer for W4 mining sidecar."""
    try:
        mr = FBMiningResponse.MiningResponse.GetRootAs(mining_buf, 0)
        return mr.ReqId()
    except Exception as e:
        logger.debug(f"Failed to extract req_id: {e}")
        return None


def _extract_model_identifier(mining_buf: bytes) -> Optional[str]:
    """Extract model_identifier from embedded PowBlob, if present."""
    try:
        mr = FBMiningResponse.MiningResponse.GetRootAs(mining_buf, 0)
        proof = mr.PowBlob()
        if proof is None:
            return None
        model_identifier = proof.ModelIdentifier()
        if not model_identifier:
            return None
        return (
            model_identifier.decode("utf-8")
            if isinstance(model_identifier, bytes)
            else str(model_identifier)
        ).strip()
    except Exception as e:
        logger.debug(f"Failed to extract model_identifier: {e}")
        return None


def _normalize_model_identifier(ident: Optional[str]) -> str:
    """Normalize a "<name-or-path>@<commit>" model identifier to
    "<org>/<name>@<commit>".

    A worker that loads the model offline from the local HF cache
    (HF_HUB_OFFLINE=1, snapshot pre-seeded under /models/hub on the
    egress-locked cGPU worker) stamps the resolved snapshot PATH into the
    proof, e.g.
        /models/hub/models--Qwen--Qwen3-8B/snapshots/<sha>@<sha>
    while the chain-registered identity (and the worker's
    expected_model_identifier) is "Qwen/Qwen3-8B@<sha>". Recover the repo
    id from the HF "models--<org>--<name>" cache-dir convention so the two
    forms compare equal — otherwise every mining proof is dropped as a
    model mismatch and nothing is ever credited.
    """
    if not ident:
        return ""
    name, sep, commit = ident.rpartition("@")
    if not sep:
        name, commit = ident, ""
    m = re.search(r"models--([^/@]+)", name)
    if m:
        name = m.group(1).replace("--", "/")
    return f"{name}@{commit}" if commit else name


def _extract_proof_nonce(mining_buf: bytes) -> Optional[int]:
    """Worker doesn't currently stamp nonce in the proof FlatBuffer
    (the BlockHeader carries it). For MINE_SHARE wire shape we need
    the per-share nonce; if it's not on the proof, the caller falls
    back to a derivation from the digest. Returns None when absent."""
    try:
        mr = FBMiningResponse.MiningResponse.GetRootAs(mining_buf, 0)
        return mr.Nonce()
    except Exception:
        return None


def _extract_proof_hash_hex(mining_buf: bytes) -> Optional[str]:
    """Extract the embedded Proof.hash as a lowercase hex string. This
    is the canonical achieved digest the worker computed; the broker
    re-verifies via the verification service."""
    try:
        mr = FBMiningResponse.MiningResponse.GetRootAs(mining_buf, 0)
        proof = mr.PowBlob()
        if proof is None:
            return None
        n = proof.HashLength()
        if n <= 0:
            return None
        out = bytearray(n)
        for i in range(n):
            out[i] = proof.Hash(i)
        return bytes(out).hex()
    except Exception as e:
        logger.debug(f"Failed to extract proof hash: {e}")
        return None


def _extract_is_solution(mining_buf: bytes) -> Optional[bool]:
    """Slice 11.4 — pull the proof's ``is_solution`` bit.

    The sampler stamps this:
      - ``True`` when the digest meets the chain/model-adjusted
        BLOCK target → broker chain-submits as MineResult.
      - ``False`` when the digest only meets the easier share
        target (sub-block emission) → broker accounts as MineShare.

    Returns ``None`` on FlatBuffer parse failure so the caller can
    fall back to the legacy "treat as block solution" path during
    rollout instead of dropping the proof silently.
    """
    try:
        mr = FBMiningResponse.MiningResponse.GetRootAs(mining_buf, 0)
        proof = mr.PowBlob()
        if proof is None:
            return None
        if not hasattr(proof, "IsSolution"):
            return None
        return bool(proof.IsSolution())
    except Exception as exc:
        logger.debug(f"Failed to extract is_solution: {exc}")
        return None


# ProofCollector receives block-tier proofs and fires the solution
# callback. Shares are emitted by the worker sampler on a separate
# path (worker_client._send_mine_share_typed) — they do not arrive
# through this PULL socket.
SolutionCallback = Callable[[int, bytes], None]


class ProofCollector:
    def __init__(self, cache: ProofCache, context=None):
        self.cache = cache
        self.context = context
        self.port = constants.PROOF_COLLECTOR_PORT
        self.recv_timeout = 1000
        self.running = False
        self._ctx = None
        self._sock = None
        self._thread = None
        # W4: Optional callback for broker mode to forward solutions
        self._solution_callback: Optional[SolutionCallback] = None

    def set_solution_callback(self, callback: Optional[SolutionCallback]):
        """
        W4: Set callback to receive solutions for broker mode.

        The callback receives (req_id, mining_buf) for each MiningResponse.
        This allows worker_client to forward solutions as MINE_RESULT.

        ProofCollector handles block-tier proofs only. Shares are
        emitted on a separate path from the worker sampler — see
        worker_client._send_mine_share_typed.
        """
        self._solution_callback = callback
        if callback:
            logger.info("Solution callback registered for broker mode")

    def start(self):
        if self.running:
            logger.warning("ProofCollector already running")
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info(f"ProofCollector started on port {self.port}")

    def stop(self):
        logger.info("Stopping ProofCollector...")
        self.running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3.0)
        logger.info("ProofCollector stopped")

    def _run(self):
        try:
            self._ctx = zmq.Context()
            self._sock = self._ctx.socket(zmq.PULL)
            self._sock.bind(f"tcp://*:{self.port}")
            self._sock.setsockopt(zmq.RCVTIMEO, self.recv_timeout)
            logger.info(f"ProofCollector listening on tcp://*:{self.port}")

            while self.running:
                try:
                    buf = self._sock.recv()
                except zmq.error.Again:
                    continue
                except Exception as e:
                    logger.error(f"ZMQ recv error: {e}")
                    continue

                # Audit proofs are completion-audit artifacts, not mining
                # results: cache by completion_id and short-circuit BEFORE
                # every mining assumption below — cooldown drop, stale
                # req_id, expected-model filter, solution-cooldown
                # activation, and the solution callback. They must never
                # pause mining nor reach MINE_RESULT/MINE_SHARE.
                if _extract_proof_purpose(buf) == "audit":
                    cid = _extract_completion_id(buf)
                    if cid:
                        self.cache.put(cid, buf)
                        logger.debug(f"Cached audit proof for completion_id={cid}")
                    else:
                        logger.debug("Audit proof without completion_id; dropped")
                    continue

                # When mining is paused (solution cooldown / force model switch guard),
                # drop proofs to avoid forwarding stale or parasitic results.
                if self.context and self.context.is_mining_paused():
                    logger.info("Dropping proof while mining is paused (cooldown active)")
                    continue

                req_id = _extract_req_id(buf)
                if self.context and req_id is not None:
                    current_req_id = int(self.context.read().request_id)
                    if current_req_id and int(req_id) != current_req_id:
                        logger.info(
                            "Dropping stale proof: req_id=%s, current_req_id=%s",
                            req_id,
                            current_req_id,
                        )
                        continue

                if self.context:
                    expected_identifier = (self.context.get_expected_model_identifier() or "").strip()
                else:
                    expected_identifier = ""
                proof_identifier = (_extract_model_identifier(buf) or "").strip()
                # Normalize both sides: a worker loading the model offline
                # from /models/hub stamps the snapshot PATH instead of the
                # HF repo id, which would otherwise mismatch the expected
                # "<name>@<commit>" and drop every mining proof.
                if (expected_identifier and proof_identifier
                        and _normalize_model_identifier(proof_identifier)
                        != _normalize_model_identifier(expected_identifier)):
                    logger.info(
                        "Dropping stale proof: model_identifier=%s, expected=%s",
                        proof_identifier,
                        expected_identifier,
                    )
                    continue

                # Cache by completion_id (existing behavior)
                cid = _extract_completion_id(buf)
                if cid:
                    logger.info(f"[DEBUG proof_collector] Received proof with completion_id: {cid}")
                    self.cache.put(cid, buf)
                else:
                    logger.debug("Received proof without completion_id; not caching")

                # ProofCollector does NOT classify proofs as solution
                # vs share. The classification happens at the worker's
                # sampler layer, where both adjusted thresholds are
                # in scope alongside the computed digest. Proofs that
                # arrive here are block-tier solutions — shares are
                # emitted by the worker via a separate WS path.
                # (Slice-10 attempted to classify by Proof.target
                # equality; that didn't work because Proof.target is
                # always the model-adjusted block target. Removed.)
                if (
                    self.context
                    and constants.MINING_SOLUTION_COOLDOWN_SEC > 0
                    and not self.context.is_mining_paused()
                ):
                    # Poisson-distributed cooldown: on mainnet, block
                    # arrivals are memoryless so the inter-solution gap
                    # follows an exponential distribution. Drawing from
                    # Exp(1/mean) gives realistic variance while keeping
                    # the configured value as the long-run average.
                    cooldown = random.expovariate(
                        1.0 / constants.MINING_SOLUTION_COOLDOWN_SEC
                    )
                    self.context.activate_solution_cooldown(
                        cooldown,
                        reason="solution_received",
                    )

                if req_id is None:
                    logger.debug("Received proof without req_id; not forwarding to broker")
                elif self._solution_callback is not None:
                    try:
                        self._solution_callback(req_id, buf)
                    except Exception as e:
                        logger.error(f"Solution callback error for req_id={req_id}: {e}")

        except Exception as e:
            logger.exception(f"ProofCollector fatal error: {e}")
        finally:
            if self._sock:
                self._sock.close()
            if self._ctx:
                self._ctx.term()

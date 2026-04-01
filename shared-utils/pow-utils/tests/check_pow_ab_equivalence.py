#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Strict A/B equivalence checker for PoW sampling proof outputs.

Compares two proof directories (old branch vs new branch) by unpacking
MiningResponse FlatBuffers and validating:
- exact fields (token ids, hashes, metadata)
- float arrays/scalars with configurable tolerances

This is intentionally strict for PoW sampler verification.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import re
import sys
from pathlib import Path
from typing import Any

try:
    import numpy as np  # type: ignore
except Exception:
    np = None

_PFUNPACK: Any = None


def _get_pfunpack():
    global _PFUNPACK
    if _PFUNPACK is not None:
        return _PFUNPACK
    try:
        import pfunpack  # type: ignore
    except Exception as exc:  # pragma: no cover
        raise RuntimeError(
            "failed to import pfunpack. Build it first from this directory, e.g.:\n"
            "  ./build_pfunpack.sh\n"
            f"Import error: {exc}"
        ) from exc
    _PFUNPACK = pfunpack
    return _PFUNPACK


FILENAME_RE = re.compile(r"^proof_(?P<seq>-?\d+)_(?P<step>-?\d+)_(?P<ts>\d+)\.bin$")


EXACT_TOP_LEVEL_FIELDS = (
    "req_id",
    "difficulty",
    "completion_id",
    "nonce",
    "adjusted_bits",
    "pow_blob_hash",
)

EXACT_POW_FIELDS = (
    "version",
    "tick",
    "is_solution",
    "model_identifier",
    "compute_precision",
    "top_k",
    "target",
    "vdf",
    "hash",
    "block_hash",
    "header_prefix",
)

EXACT_POW_ARRAY_FIELDS = (
    "chosen_tokens",
    "prompt_tokens",
    "topk_indices",
    "pad_mask",
)

FLOAT_POW_SCALAR_FIELDS = (
    "temperature",
    "top_p",
    "repetition_penalty",
)

FLOAT_POW_ARRAY_FIELDS = (
    "chosen_probs",
    "sampling_u",
    "softmax_normalizers",
    "topk_logits",
    "logsumexp_stats",
)


@dataclasses.dataclass
class ProofFile:
    path: Path
    seq_hint: int
    step_hint: int
    ts_hint: int
    unpacked: dict[str, Any]

    @property
    def req_id(self) -> str:
        return str(self.unpacked.get("req_id", ""))

    @property
    def completion_id(self) -> str:
        return str(self.unpacked.get("completion_id", ""))


def parse_filename_hints(path: Path) -> tuple[int, int, int]:
    m = FILENAME_RE.match(path.name)
    if not m:
        return (-1, -1, -1)
    return (int(m.group("seq")), int(m.group("step")), int(m.group("ts")))


def _to_builtin(value: Any) -> Any:
    if np is not None:
        if isinstance(value, np.ndarray):
            return value.tolist()
        if isinstance(value, np.generic):
            return value.item()
    if hasattr(value, "tolist"):
        try:
            return value.tolist()
        except Exception:
            pass
    if isinstance(value, tuple):
        return [_to_builtin(v) for v in value]
    if isinstance(value, list):
        return [_to_builtin(v) for v in value]
    return value


def _normalize_scalar(value: Any) -> Any:
    if np is not None and isinstance(value, np.generic):
        return value.item()
    return value


def _shape(value: Any) -> tuple[int, ...]:
    val = _to_builtin(value)
    if not isinstance(val, list):
        return ()
    if len(val) == 0:
        return (0,)
    return (len(val),) + _shape(val[0])


def _find_first_mismatch_exact(a: Any, b: Any, path: tuple[int, ...] = ()) -> tuple[int, ...] | None:
    a = _to_builtin(a)
    b = _to_builtin(b)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return path
        for i, (av, bv) in enumerate(zip(a, b)):
            mm = _find_first_mismatch_exact(av, bv, path + (i,))
            if mm is not None:
                return mm
        return None
    return None if _normalize_scalar(a) == _normalize_scalar(b) else path


def _iter_float_deltas(a: Any, b: Any, path: tuple[int, ...] = ()):
    a = _to_builtin(a)
    b = _to_builtin(b)
    if isinstance(a, list) and isinstance(b, list):
        for i, (av, bv) in enumerate(zip(a, b)):
            yield from _iter_float_deltas(av, bv, path + (i,))
        return
    fa = float(a)
    fb = float(b)
    yield path, fa, fb, abs(fa - fb)


def _allclose_scalar(a: float, b: float, atol: float, rtol: float) -> bool:
    return math.isclose(a, b, rel_tol=rtol, abs_tol=atol)


def load_proof_file(path: Path) -> ProofFile:
    raw = path.read_bytes()
    unpacked = _get_pfunpack().unpack_mining_response(raw)
    seq_hint, step_hint, ts_hint = parse_filename_hints(path)
    return ProofFile(
        path=path,
        seq_hint=seq_hint,
        step_hint=step_hint,
        ts_hint=ts_hint,
        unpacked=unpacked,
    )


def load_dir(proof_dir: Path) -> list[ProofFile]:
    files = sorted(proof_dir.glob("*.bin"))
    out: list[ProofFile] = []
    for f in files:
        out.append(load_proof_file(f))
    return out


def group_records(records: list[ProofFile]) -> dict[tuple[str, str], list[ProofFile]]:
    grouped: dict[tuple[str, str], list[ProofFile]] = {}
    for rec in records:
        key = (rec.req_id, rec.completion_id)
        grouped.setdefault(key, []).append(rec)

    for key in grouped:
        grouped[key].sort(key=lambda r: (r.step_hint, r.ts_hint, r.path.name))
    return grouped


def diff_exact(path: str, a: Any, b: Any, diffs: list[str]) -> None:
    if _normalize_scalar(a) != _normalize_scalar(b):
        diffs.append(f"{path}: {a!r} != {b!r}")


def diff_exact_array(path: str, a: Any, b: Any, diffs: list[str]) -> None:
    shape_a = _shape(a)
    shape_b = _shape(b)
    if shape_a != shape_b:
        diffs.append(f"{path}: shape {shape_a} != {shape_b}")
        return
    first = _find_first_mismatch_exact(a, b)
    if first is not None:
        diffs.append(f"{path}: arrays differ, first mismatch at {first}")


def diff_float_scalar(path: str, a: Any, b: Any, atol: float, rtol: float, diffs: list[str]) -> None:
    fa = float(a)
    fb = float(b)
    if not _allclose_scalar(fa, fb, atol=atol, rtol=rtol):
        diffs.append(f"{path}: {fa:.12g} != {fb:.12g} (atol={atol}, rtol={rtol})")


def diff_float_array(path: str, a: Any, b: Any, atol: float, rtol: float, diffs: list[str]) -> None:
    shape_a = _shape(a)
    shape_b = _shape(b)
    if shape_a != shape_b:
        diffs.append(f"{path}: shape {shape_a} != {shape_b}")
        return

    max_delta = -1.0
    max_idx: tuple[int, ...] = ()
    max_a = 0.0
    max_b = 0.0
    any_fail = False
    for idx, fa, fb, delta in _iter_float_deltas(a, b):
        if delta > max_delta:
            max_delta = delta
            max_idx = idx
            max_a = fa
            max_b = fb
        if not _allclose_scalar(fa, fb, atol=atol, rtol=rtol):
            any_fail = True

    if any_fail:
        diffs.append(
            f"{path}: max |delta|={max_delta:.12g} at {max_idx}, "
            f"a={max_a:.12g}, b={max_b:.12g}"
        )


def compare_pair(
    old: ProofFile,
    new: ProofFile,
    *,
    atol: float,
    rtol: float,
    ignore_timestamp: bool,
) -> list[str]:
    diffs: list[str] = []

    for field in EXACT_TOP_LEVEL_FIELDS:
        diff_exact(f"top.{field}", old.unpacked.get(field), new.unpacked.get(field), diffs)

    old_pb = old.unpacked.get("pow_blob")
    new_pb = new.unpacked.get("pow_blob")
    if old_pb is None or new_pb is None:
        diffs.append("top.pow_blob: missing in one side")
        return diffs

    for field in EXACT_POW_FIELDS:
        diff_exact(f"pow_blob.{field}", old_pb.get(field), new_pb.get(field), diffs)

    if not ignore_timestamp:
        diff_exact("pow_blob.timestamp", old_pb.get("timestamp"), new_pb.get("timestamp"), diffs)

    for field in EXACT_POW_ARRAY_FIELDS:
        diff_exact_array(f"pow_blob.{field}", old_pb.get(field), new_pb.get(field), diffs)

    for field in FLOAT_POW_SCALAR_FIELDS:
        diff_float_scalar(
            f"pow_blob.{field}",
            old_pb.get(field),
            new_pb.get(field),
            atol,
            rtol,
            diffs,
        )

    for field in FLOAT_POW_ARRAY_FIELDS:
        diff_float_array(
            f"pow_blob.{field}",
            old_pb.get(field),
            new_pb.get(field),
            atol,
            rtol,
            diffs,
        )

    return diffs


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--old-dir", required=True, type=Path, help="Proof dir from old branch run")
    p.add_argument("--new-dir", required=True, type=Path, help="Proof dir from new branch run")
    p.add_argument("--atol", type=float, default=1e-7, help="Absolute tolerance for float checks")
    p.add_argument("--rtol", type=float, default=0.0, help="Relative tolerance for float checks")
    p.add_argument(
        "--ignore-timestamp",
        action="store_true",
        default=False,
        help="Ignore pow_blob.timestamp comparison (recommended)",
    )
    p.add_argument(
        "--expected-proofs-per-group",
        type=int,
        default=None,
        help="Optional exact count expected per (req_id, completion_id) group",
    )
    p.add_argument(
        "--max-diff-lines",
        type=int,
        default=200,
        help="Cap total diff lines in output",
    )
    p.add_argument(
        "--dump-summary-json",
        type=Path,
        default=None,
        help="Optional path to write machine-readable summary JSON",
    )
    args = p.parse_args()

    if not args.old_dir.exists() or not args.new_dir.exists():
        print("ERROR: both --old-dir and --new-dir must exist", file=sys.stderr)
        return 2

    try:
        old_records = load_dir(args.old_dir)
        new_records = load_dir(args.new_dir)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    old_groups = group_records(old_records)
    new_groups = group_records(new_records)

    summary: dict[str, Any] = {
        "old_dir": str(args.old_dir),
        "new_dir": str(args.new_dir),
        "old_files": len(old_records),
        "new_files": len(new_records),
        "groups_old": len(old_groups),
        "groups_new": len(new_groups),
        "failures": [],
    }

    failures: list[str] = []
    old_keys = set(old_groups.keys())
    new_keys = set(new_groups.keys())
    only_old = sorted(old_keys - new_keys)
    only_new = sorted(new_keys - old_keys)

    if only_old:
        failures.append(f"group keys present only in old: {only_old}")
    if only_new:
        failures.append(f"group keys present only in new: {only_new}")

    shared_keys = sorted(old_keys & new_keys)
    for key in shared_keys:
        old_list = old_groups[key]
        new_list = new_groups[key]
        if len(old_list) != len(new_list):
            failures.append(f"group {key}: count mismatch old={len(old_list)} new={len(new_list)}")
            continue

        if args.expected_proofs_per_group is not None and len(old_list) != args.expected_proofs_per_group:
            failures.append(
                f"group {key}: count={len(old_list)} expected={args.expected_proofs_per_group}"
            )

        for idx, (old_rec, new_rec) in enumerate(zip(old_list, new_list)):
            diffs = compare_pair(
                old_rec,
                new_rec,
                atol=args.atol,
                rtol=args.rtol,
                ignore_timestamp=args.ignore_timestamp,
            )
            if diffs:
                header = (
                    f"group={key} idx={idx}\n"
                    f"  old={old_rec.path}\n"
                    f"  new={new_rec.path}"
                )
                failures.append(header)
                failures.extend(f"    - {d}" for d in diffs)

    if len(failures) > args.max_diff_lines:
        failures = failures[: args.max_diff_lines] + [
            f"... truncated at {args.max_diff_lines} lines ..."
        ]

    summary["failures"] = failures
    summary["ok"] = not failures

    print("=== PoW A/B Equivalence Check ===")
    print(f"old files: {len(old_records)}")
    print(f"new files: {len(new_records)}")
    print(f"groups old/new: {len(old_groups)}/{len(new_groups)}")
    print(f"float tolerances: atol={args.atol} rtol={args.rtol}")
    print(f"ignore_timestamp: {args.ignore_timestamp}")

    if failures:
        print("\nFAIL")
        for line in failures:
            print(line)
    else:
        print("\nPASS: strict equivalence checks passed")

    if args.dump_summary_json is not None:
        args.dump_summary_json.write_text(json.dumps(summary, indent=2))
        print(f"\nWrote summary: {args.dump_summary_json}")

    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())

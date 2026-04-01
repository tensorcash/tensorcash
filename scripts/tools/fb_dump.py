#!/usr/bin/env python3
"""
FlatBuffer MiningResponse inspector.

Usage:
  python scripts/tools/fb_dump.py path/to/file.bin
  cat file.bin | python scripts/tools/fb_dump.py -

Prints a JSON summary including completion_id (from Proof.extra_flags),
hashes, sizes, and key sampling fields.
"""
import sys
import json
from pathlib import Path

def _add_schema_to_path():
    # Attempt to locate shared-utils/fb-schemas relative to repo root
    here = Path(__file__).resolve()
    root = here.parents[2]  # scripts/tools -> scripts -> repo
    schemas = root / 'shared-utils' / 'fb-schemas'
    if schemas.exists():
        sys.path.insert(0, str(schemas))

def _parse(buf: bytes) -> dict:
    from proof import MiningResponse as FBMiningResponse
    mr = FBMiningResponse.MiningResponse.GetRootAs(buf, 0)
    d = {}
    d['req_id'] = int(mr.ReqId())
    d['nonce'] = int(mr.Nonce())
    d['adjusted_bits'] = int(mr.AdjustedBits())
    d['difficulty'] = int(mr.Difficulty())
    d['pow_blob_hash'] = bytes(mr.PowBlobHashAsNumpy()).hex() if not mr.PowBlobHashIsNone() else ''
    proof = mr.PowBlob()
    if proof is None:
        d['proof'] = None
        return d
    pd = {}
    pd['version'] = int(proof.Version())
    pd['tick'] = int(proof.Tick())
    pd['timestamp'] = int(proof.Timestamp())
    pd['is_solution'] = bool(proof.IsSolution())
    pd['target'] = bytes(proof.TargetAsNumpy()).hex() if proof.TargetAsNumpy() is not None else ''
    pd['vdf'] = bytes(proof.VdfAsNumpy()).hex() if proof.VdfAsNumpy() is not None else ''
    pd['hash'] = bytes(proof.HashAsNumpy()).hex() if proof.HashAsNumpy() is not None else ''
    pd['block_hash'] = bytes(proof.BlockHashAsNumpy()).hex() if proof.BlockHashAsNumpy() is not None else ''
    pd['header_prefix'] = bytes(proof.HeaderPrefixAsNumpy()).hex() if proof.HeaderPrefixAsNumpy() is not None else ''
    pd['model_identifier'] = proof.ModelIdentifier() or ''
    pd['compute_precision'] = proof.ComputePrecision() or ''
    pd['ipfs_cid'] = proof.IpfsCid() or ''
    pd['extra_flags'] = proof.ExtraFlags() or ''
    # Try to extract completion_id from extra_flags JSON
    completion_id = None
    try:
        if pd['extra_flags']:
            obj = json.loads(pd['extra_flags'])
            completion_id = obj.get('completion_id')
    except Exception:
        pass
    d['completion_id'] = completion_id
    d['proof'] = pd
    return d

def main():
    _add_schema_to_path()
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    arg = sys.argv[1]
    if arg == '-':
        data = sys.stdin.buffer.read()
    else:
        data = Path(arg).read_bytes()
    out = _parse(data)
    print(json.dumps(out, indent=2))

if __name__ == '__main__':
    main()


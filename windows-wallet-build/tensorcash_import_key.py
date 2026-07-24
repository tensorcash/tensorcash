#!/usr/bin/env python3
"""
TensorCash WIF/HEX importer for Bitcoin Core-derived descriptor wallets.

What it does:
1. Accepts a WIF or 32-byte private-key HEX.
2. Validates/normalizes the key.
3. Derives the compressed public key and native SegWit address (default HRP: tc).
4. Connects to TensorCash JSON-RPC.
5. Creates or loads a descriptor wallet.
6. Imports wpkh(PRIVATE_KEY) through importdescriptors.
7. Verifies that the expected address belongs to the wallet.
8. Creates a native Core wallet backup file accepted by GUI "Restore wallet".

Dependencies:
    pip install ecdsa
Optional:
    pip install base58
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

try:
    import ecdsa
except ImportError:
    print("Missing dependency: ecdsa\nInstall it with: pip install ecdsa", file=sys.stderr)
    raise SystemExit(2)

SECP256K1_ORDER = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
BECH32_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


class RpcError(RuntimeError):
    def __init__(self, code: Optional[int], message: str, data: Any = None):
        super().__init__(f"RPC error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data


@dataclass(frozen=True)
class KeyData:
    secret: bytes
    compressed: bool
    wif: str
    wif_version: int
    pubkey: bytes
    pubkey_hash: bytes
    address: str


def sha256(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def hash160(data: bytes) -> bytes:
    h = hashlib.new("ripemd160")
    h.update(sha256(data))
    return h.digest()


def b58encode(raw: bytes) -> str:
    alphabet = b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    value = int.from_bytes(raw, "big")
    out = bytearray()
    while value:
        value, rem = divmod(value, 58)
        out.append(alphabet[rem])
    leading_zeroes = len(raw) - len(raw.lstrip(b"\x00"))
    return (alphabet[:1] * leading_zeroes + bytes(reversed(out))).decode("ascii")


def b58decode(text: str) -> bytes:
    alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    value = 0
    for char in text:
        try:
            digit = alphabet.index(char)
        except ValueError as exc:
            raise ValueError(f"Invalid Base58 character: {char!r}") from exc
        value = value * 58 + digit
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big") if value else b""
    leading_ones = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_ones + raw


def b58check_encode(payload: bytes) -> str:
    return b58encode(payload + sha256(sha256(payload))[:4])


def b58check_decode(text: str) -> bytes:
    raw = b58decode(text.strip())
    if len(raw) < 5:
        raise ValueError("WIF is too short")
    payload, checksum = raw[:-4], raw[-4:]
    expected = sha256(sha256(payload))[:4]
    if checksum != expected:
        raise ValueError("Invalid WIF checksum")
    return payload


def bech32_polymod(values: list[int]) -> int:
    generators = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for value in values:
        top = chk >> 25
        chk = ((chk & 0x1FFFFFF) << 5) ^ value
        for i, generator in enumerate(generators):
            if (top >> i) & 1:
                chk ^= generator
    return chk


def bech32_hrp_expand(hrp: str) -> list[int]:
    return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]


def bech32_create_checksum(hrp: str, data: list[int], spec_constant: int = 1) -> list[int]:
    values = bech32_hrp_expand(hrp) + data
    polymod = bech32_polymod(values + [0] * 6) ^ spec_constant
    return [(polymod >> (5 * (5 - i))) & 31 for i in range(6)]


def bech32_encode(hrp: str, data: list[int], spec_constant: int = 1) -> str:
    combined = data + bech32_create_checksum(hrp, data, spec_constant)
    return hrp + "1" + "".join(BECH32_CHARSET[d] for d in combined)


def convertbits(data: bytes, from_bits: int, to_bits: int, pad: bool = True) -> list[int]:
    acc = 0
    bits = 0
    result: list[int] = []
    maxv = (1 << to_bits) - 1
    max_acc = (1 << (from_bits + to_bits - 1)) - 1
    for value in data:
        if value < 0 or value >> from_bits:
            raise ValueError("Invalid convertbits input")
        acc = ((acc << from_bits) | value) & max_acc
        bits += from_bits
        while bits >= to_bits:
            bits -= to_bits
            result.append((acc >> bits) & maxv)
    if pad:
        if bits:
            result.append((acc << (to_bits - bits)) & maxv)
    elif bits >= from_bits or ((acc << (to_bits - bits)) & maxv):
        raise ValueError("Invalid incomplete bit group")
    return result


def segwit_v0_address(hrp: str, witness_program: bytes) -> str:
    if len(witness_program) not in (20, 32):
        raise ValueError("SegWit v0 witness program must be 20 or 32 bytes")
    data = [0] + convertbits(witness_program, 8, 5, True)
    return bech32_encode(hrp.lower(), data, spec_constant=1)


def compressed_pubkey(secret: bytes) -> bytes:
    signing_key = ecdsa.SigningKey.from_string(secret, curve=ecdsa.SECP256k1)
    verifying_key = signing_key.verifying_key
    raw = verifying_key.to_string()
    x, y = raw[:32], raw[32:]
    return (b"\x02" if (int.from_bytes(y, "big") % 2 == 0) else b"\x03") + x


def parse_key(
    wif: Optional[str],
    private_hex: Optional[str],
    hrp: str,
    default_wif_version: int,
) -> KeyData:
    if bool(wif) == bool(private_hex):
        raise ValueError("Provide exactly one of --wif or --hex")

    if wif:
        payload = b58check_decode(wif)
        if len(payload) == 34 and payload[-1] == 0x01:
            version = payload[0]
            secret = payload[1:-1]
            compressed = True
        elif len(payload) == 33:
            version = payload[0]
            secret = payload[1:]
            compressed = False
        else:
            raise ValueError(
                f"Unsupported WIF payload length: {len(payload)} bytes "
                "(expected 33 uncompressed or 34 compressed)"
            )
        normalized_wif = wif.strip()
    else:
        clean_hex = private_hex.strip().lower()
        if clean_hex.startswith("0x"):
            clean_hex = clean_hex[2:]
        if len(clean_hex) != 64:
            raise ValueError("--hex must contain exactly 32 bytes / 64 hex characters")
        try:
            secret = bytes.fromhex(clean_hex)
        except ValueError as exc:
            raise ValueError("--hex contains non-hexadecimal characters") from exc
        version = default_wif_version
        compressed = True
        normalized_wif = b58check_encode(bytes([version]) + secret + b"\x01")

    scalar = int.from_bytes(secret, "big")
    if not 1 <= scalar < SECP256K1_ORDER:
        raise ValueError("Private key scalar is outside the secp256k1 valid range")

    # Native SegWit wpkh requires a compressed public key.
    if not compressed:
        raise ValueError(
            "The supplied WIF is uncompressed. It cannot correspond to a native "
            "SegWit tc1... address. Supply the compressed WIF for this key."
        )

    pubkey = compressed_pubkey(secret)
    pubkey_hash = hash160(pubkey)
    address = segwit_v0_address(hrp, pubkey_hash)
    return KeyData(
        secret=secret,
        compressed=compressed,
        wif=normalized_wif,
        wif_version=version,
        pubkey=pubkey,
        pubkey_hash=pubkey_hash,
        address=address,
    )


class JsonRpc:
    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str,
        wallet: Optional[str] = None,
        timeout: float = 30.0,
    ):
        base = f"http://{host}:{port}"
        if wallet is not None:
            base += "/wallet/" + urllib.parse.quote(wallet, safe="")
        self.url = base
        token = base64.b64encode(f"{username}:{password}".encode()).decode()
        self.headers = {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }
        self.timeout = timeout
        self.request_id = 0

    def call(self, method: str, params: Any = None) -> Any:
        self.request_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self.request_id,
            "method": method,
            "params": [] if params is None else params,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers=self.headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(body)
                error = parsed.get("error") or {}
                raise RpcError(error.get("code"), error.get("message", body), error.get("data"))
            except json.JSONDecodeError:
                raise RuntimeError(f"RPC HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Cannot connect to TensorCash RPC at {self.url}: {exc.reason}") from exc

        parsed = json.loads(body)
        if parsed.get("error"):
            error = parsed["error"]
            raise RpcError(error.get("code"), error.get("message", "Unknown RPC error"), error.get("data"))
        return parsed.get("result")


def wait_for_rpc(rpc: JsonRpc, seconds: float) -> None:
    deadline = time.monotonic() + seconds
    last_error: Optional[Exception] = None
    while time.monotonic() < deadline:
        try:
            rpc.call("getblockchaininfo")
            return
        except Exception as exc:
            last_error = exc
            time.sleep(0.5)
    raise RuntimeError(f"RPC did not become ready within {seconds:g} seconds: {last_error}")


def wallet_exists_in_dir(root_rpc: JsonRpc, wallet_name: str) -> bool:
    result = root_rpc.call("listwalletdir")
    wallets = result.get("wallets", []) if isinstance(result, dict) else []
    return any(item.get("name") == wallet_name for item in wallets)


def ensure_wallet(
    root_rpc: JsonRpc,
    host: str,
    port: int,
    username: str,
    password: str,
    wallet_name: str,
    timeout: float,
) -> JsonRpc:
    loaded = root_rpc.call("listwallets")
    if wallet_name not in loaded:
        if wallet_exists_in_dir(root_rpc, wallet_name):
            try:
                root_rpc.call("loadwallet", {"filename": wallet_name, "load_on_startup": True})
            except RpcError as exc:
                # -35 is commonly "wallet already loaded"; tolerate races/build differences.
                if "already loaded" not in exc.message.lower():
                    raise
        else:
            # Descriptor wallet with private keys enabled.
            try:
                root_rpc.call(
                    "createwallet",
                    {
                        "wallet_name": wallet_name,
                        "disable_private_keys": False,
                        "blank": True,
                        "passphrase": "",
                        "avoid_reuse": False,
                        "descriptors": True,
                        "load_on_startup": True,
                    },
                )
            except RpcError as exc:
                # Some forks may not expose load_on_startup as a named parameter.
                if "unknown named parameter" in exc.message.lower():
                    root_rpc.call(
                        "createwallet",
                        {
                            "wallet_name": wallet_name,
                            "disable_private_keys": False,
                            "blank": True,
                            "passphrase": "",
                            "avoid_reuse": False,
                            "descriptors": True,
                        },
                    )
                elif "already exists" in exc.message.lower():
                    root_rpc.call("loadwallet", {"filename": wallet_name})
                else:
                    raise

    wallet_rpc = JsonRpc(host, port, username, password, wallet_name, timeout)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            wallet_rpc.call("getwalletinfo")
            return wallet_rpc
        except Exception:
            time.sleep(0.25)
    raise RuntimeError(f"Wallet {wallet_name!r} was created/loaded but its RPC endpoint did not become ready")


def descriptor_with_checksum(root_rpc: JsonRpc, descriptor_without_checksum: str) -> str:
    info = root_rpc.call("getdescriptorinfo", [descriptor_without_checksum])
    checksum = info.get("checksum")
    if not checksum:
        # Fallback for builds returning only the canonical descriptor.
        returned = info.get("descriptor", "")
        if "#" in returned:
            checksum = returned.rsplit("#", 1)[1]
    if not checksum:
        raise RuntimeError(f"getdescriptorinfo did not return a checksum: {info!r}")
    return f"{descriptor_without_checksum}#{checksum}"


def import_descriptor(
    root_rpc: JsonRpc,
    wallet_rpc: JsonRpc,
    wif: str,
    label: str,
    timestamp: int | str,
    rescan: bool,
) -> None:
    descriptor = descriptor_with_checksum(root_rpc, f"wpkh({wif})")
    # ponytail: TensorCash rejects "label" on non-internal descriptors.
    request = {
        "desc": descriptor,
        "timestamp": timestamp,
        "active": False,
        "internal": False,
    }
    result = wallet_rpc.call("importdescriptors", [[request]])
    if not isinstance(result, list) or not result:
        raise RuntimeError(f"Unexpected importdescriptors response: {result!r}")

    item = result[0]
    if not item.get("success"):
        error = item.get("error") or {}
        raise RpcError(error.get("code"), error.get("message", "Descriptor import failed"), error.get("data"))

    warnings = item.get("warnings") or []
    for warning in warnings:
        print(f"WARNING from importdescriptors: {warning}")

    # importdescriptors normally performs the timestamp-based rescan itself.
    # Optional explicit rescan is useful for forks/builds that skip it.
    if rescan:
        start_height = 0
        print("Starting explicit rescan from height 0. This can take a long time...")
        wallet_rpc.call("rescanblockchain", [start_height])


def verify_wallet_address(wallet_rpc: JsonRpc, address: str) -> dict[str, Any]:
    info = wallet_rpc.call("getaddressinfo", [address])
    if not info.get("ismine"):
        raise RuntimeError(
            "Import RPC returned success, but getaddressinfo says ismine=false. "
            f"Response: {json.dumps(info, indent=2)}"
        )
    if not info.get("solvable"):
        raise RuntimeError(
            "Address is marked as mine but not solvable; private-key import is incomplete. "
            f"Response: {json.dumps(info, indent=2)}"
        )
    return info


def make_backup(wallet_rpc: JsonRpc, output_path: Path) -> Path:
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(
            f"Refusing to overwrite existing backup: {output_path}\n"
            "Choose another --backup path or remove the old file deliberately."
        )
    wallet_rpc.call("backupwallet", [str(output_path)])
    if not output_path.exists():
        # The RPC server may run in another OS/container namespace.
        print(
            "WARNING: backupwallet reported success, but the file is not visible "
            "to this Python process. The path is relative to the machine/filesystem "
            "where TensorCash Core is running."
        )
    return output_path


def parse_timestamp(value: str) -> int | str:
    if value.lower() == "now":
        return "now"
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timestamp must be Unix time or 'now'") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("timestamp cannot be negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import a TensorCash WIF/HEX into a descriptor wallet and create a Restore-compatible backup."
    )
    key_group = parser.add_mutually_exclusive_group(required=True)
    key_group.add_argument("--wif", help="Private key in Wallet Import Format")
    key_group.add_argument("--hex", dest="private_hex", help="32-byte private key as 64 hex characters")

    parser.add_argument("--expected-address", help="Abort unless the derived address exactly matches this value")
    parser.add_argument("--wallet", default="imported-wif", help="Wallet name (default: imported-wif)")
    parser.add_argument("--label", default="imported-private-key", help="Address label")
    parser.add_argument("--backup", default="tensorcash-imported-wallet.dat", help="Output native wallet backup")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=39240)
    parser.add_argument("--rpc-user", default="tc")
    parser.add_argument("--rpc-password", default="tc")
    parser.add_argument("--hrp", default="tc", help="Bech32 HRP for mainnet addresses (default: tc)")
    parser.add_argument(
        "--wif-version",
        type=lambda x: int(x, 0),
        default=128,
        help="WIF version byte used only with --hex (default: 128 / 0x80)",
    )
    parser.add_argument(
        "--timestamp",
        type=parse_timestamp,
        default=0,
        help="Key birth time: Unix timestamp or now. Default 0 scans full chain.",
    )
    parser.add_argument(
        "--explicit-rescan",
        action="store_true",
        help="After import, additionally call rescanblockchain from height 0.",
    )
    parser.add_argument("--timeout", type=float, default=30.0, help="Per-RPC timeout in seconds")
    parser.add_argument("--rpc-wait", type=float, default=15.0, help="How long to wait for RPC startup")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        key = parse_key(args.wif, args.private_hex, args.hrp, args.wif_version)
        print("Key validation: OK")
        print(f"WIF version: 0x{key.wif_version:02x}")
        print(f"Compressed pubkey: {key.pubkey.hex()}")
        print(f"Derived address: {key.address}")

        if args.expected_address and key.address.lower() != args.expected_address.lower():
            raise RuntimeError(
                "Derived address does not match --expected-address.\n"
                f"Derived:  {key.address}\n"
                f"Expected: {args.expected_address}\n"
                "Nothing was sent to RPC."
            )

        root_rpc = JsonRpc(
            args.host,
            args.port,
            args.rpc_user,
            args.rpc_password,
            wallet=None,
            timeout=args.timeout,
        )
        wait_for_rpc(root_rpc, args.rpc_wait)

        network_info = root_rpc.call("getnetworkinfo")
        print(f"Connected to node version: {network_info.get('subversion', network_info.get('version'))}")

        # ponytail: wait for IBD to finish before importing (rescan needs full chain)
        while True:
            info = root_rpc.call("getblockchaininfo")
            ibd = info.get("initialblockdownload", True)
            progress = info.get("verificationprogress", 0)
            if not ibd or progress > 0.999:
                break
            print(f"  Syncing... {progress*100:.1f}%", end="\r")
            time.sleep(5)
        print(f"\n  Sync complete ({info.get('blocks')} blocks)")

        wallet_rpc = ensure_wallet(
            root_rpc,
            args.host,
            args.port,
            args.rpc_user,
            args.rpc_password,
            args.wallet,
            args.timeout,
        )
        wallet_info = wallet_rpc.call("getwalletinfo")
        if not wallet_info.get("descriptors", False):
            raise RuntimeError(
                f"Wallet {args.wallet!r} is not a descriptor wallet. "
                "Use a new wallet name so the script can create the correct wallet type."
            )
        if wallet_info.get("private_keys_enabled") is False:
            raise RuntimeError(f"Wallet {args.wallet!r} has private keys disabled")

        import_descriptor(
            root_rpc,
            wallet_rpc,
            key.wif,
            args.label,
            args.timestamp,
            args.explicit_rescan,
        )

        address_info = verify_wallet_address(wallet_rpc, key.address)
        print("Wallet ownership verification: OK")
        print(f"Descriptor: {address_info.get('desc', '(not returned)')}")

        backup_path = make_backup(wallet_rpc, Path(args.backup))
        print()
        print("SUCCESS")
        print(f"Wallet name: {args.wallet}")
        print(f"Imported address: {key.address}")
        print(f"Native Restore-compatible backup: {backup_path}")
        print("The backup is a native Bitcoin Core/TensorCash wallet database, not JSON or a seed file.")
        return 0

    except (ValueError, RuntimeError, FileExistsError, RpcError) as exc:
        print(f"\nFAILED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

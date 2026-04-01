#!/usr/bin/env python3
"""
Local, private genesis key VERIFICATION / proof-of-control.

Run this on YOUR machine to prove that a passphrase reproduces a given pubkey
AND that you can sign (i.e. spend) with the corresponding private key.

It does two independent checks:

  1. RE-DERIVE  — prompts for the passphrase (+ optional salt), derives the
     keypair with the exact same scheme as `derive_genesis_key.py`, and confirms
     the derived pubkey equals the pubkey you paste in. Proves the passphrase
     regenerates the committed key.

  2. SIGN -> VERIFY — signs a challenge with the derived private key and verifies
     the signature against the pubkey. Proves you hold a private key that
     controls that pubkey (i.e. could produce a valid P2PK spending signature).

Run:
    source venv/bin/activate
    python3 services/miner-api/verify_genesis_key.py
"""

import getpass
import hashlib
import sys

from derive_genesis_key import derive  # same derivation, single source of truth


def main():
    expected_pub = input("Expected pubkey (paste, 130 hex chars): ").strip().lower()
    if expected_pub.startswith("0x"):
        expected_pub = expected_pub[2:]
    try:
        raw = bytes.fromhex(expected_pub)
    except ValueError:
        sys.exit("ERROR: pubkey is not valid hex")
    if not (len(raw) == 65 and raw[0] == 0x04):
        sys.exit("ERROR: expected a 65-byte uncompressed pubkey starting with 0x04")

    passphrase = getpass.getpass("Genesis passphrase (hidden): ")
    if not passphrase:
        sys.exit("ERROR: empty passphrase")
    salt = getpass.getpass("Optional extra salt (Enter for none): ")

    priv_hex, pub_hex = derive(passphrase, salt)

    # --- Check 1: re-derivation matches ---
    match = pub_hex == expected_pub
    print()
    print("1) RE-DERIVE :", "MATCH ✓" if match else "MISMATCH ✗")
    if not match:
        print("   derived:", pub_hex)
        print("   expected:", expected_pub)
        sys.exit("ABORT: passphrase/salt does not reproduce the expected pubkey")

    # --- Check 2: sign -> verify roundtrip (proof of control / spendability) ---
    from ecdsa import SECP256k1, SigningKey, VerifyingKey
    from ecdsa.util import sigencode_der, sigdecode_der

    sk = SigningKey.from_string(bytes.fromhex(priv_hex), curve=SECP256k1)
    vk = VerifyingKey.from_string(bytes.fromhex(pub_hex)[1:], curve=SECP256k1)

    challenge = b"TensorCash genesis proof-of-control"
    digest = hashlib.sha256(challenge).digest()
    signature = sk.sign_digest(digest, sigencode=sigencode_der)
    ok = vk.verify_digest(signature, digest, sigdecode=sigdecode_der)

    print("2) SIGN/VERIFY:", "VALID ✓ (you control the private key)" if ok else "INVALID ✗")
    print()
    print("   challenge :", challenge.decode())
    print("   signature :", signature.hex())
    print()
    print("PROVEN: this passphrase reproduces the pubkey and can sign for it.")


if __name__ == "__main__":
    main()

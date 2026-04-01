#!/usr/bin/env python3
"""
Local, private genesis key derivation.

Run this on YOUR machine. It prompts for a passphrase (hidden — never echoed,
never stored in shell history), derives a secp256k1 keypair, and prints:

  - the uncompressed pubkey (paste THIS into the build / chat)
  - the private key (keep OFFLINE; do NOT commit, do NOT paste anywhere)

The derivation is identical to
`components.genesis.derive_tensor_keypair_from_sentence`, so the same passphrase
(+ optional salt) always reproduces the same key.

  privkey_int = SHA256("TensorCash/GenesisKey" | passphrase | salt)  mod (n-1) + 1
  pubkey      = 0x04 || X || Y            (uncompressed, 65 bytes, for P2PK)

Security notes:
  * The secrecy of the key is ENTIRELY the secrecy of your passphrase. Use a
    high-entropy passphrase you do not reuse. Anyone who knows it can regenerate
    the private key.
  * The public coinbase headline (genesis.SEED_PHRASE / pszTimestamp) is a
    SEPARATE, public value and is not involved here.

Run:
    source venv/bin/activate
    python3 services/miner-api/derive_genesis_key.py
"""

import getpass
import hashlib
import sys

# secp256k1 group order
SECP256K1_N = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16
)
DOMAIN = "TensorCash/GenesisKey"


def derive(passphrase: str, salt: str = ""):
    material = f"{DOMAIN}|{passphrase}|{salt}".encode("utf-8")
    seed = hashlib.sha256(material).digest()
    priv_int = int.from_bytes(seed, "big") % (SECP256K1_N - 1) + 1
    priv_bytes = priv_int.to_bytes(32, "big")

    try:
        from ecdsa import SECP256k1, SigningKey
    except ImportError:
        sys.exit("ERROR: pip install ecdsa  (use the tensorcash root venv)")

    sk = SigningKey.from_string(priv_bytes, curve=SECP256k1)
    pub_bytes = b"\x04" + sk.get_verifying_key().to_string()  # uncompressed
    return priv_bytes.hex(), pub_bytes.hex()


def main():
    passphrase = getpass.getpass("Genesis passphrase (hidden): ")
    if not passphrase:
        sys.exit("ERROR: empty passphrase")
    confirm = getpass.getpass("Confirm passphrase: ")
    if passphrase != confirm:
        sys.exit("ERROR: passphrases do not match")
    salt = getpass.getpass("Optional extra salt (Enter for none): ")

    priv_hex, pub_hex = derive(passphrase, salt)

    print()
    print("=" * 74)
    print("PUBKEY  (paste this — public, safe to share):")
    print(f"  {pub_hex}")
    print()
    print("PRIVKEY (KEEP OFFLINE — do NOT commit or paste anywhere):")
    print(f"  {priv_hex}")
    print("=" * 74)
    print("Reproduce the same key any time with the same passphrase (+ salt).")


if __name__ == "__main__":
    main()

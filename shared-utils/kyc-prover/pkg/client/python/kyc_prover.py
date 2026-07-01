#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
TensorCash KYC Prover Client

Python client for the KYC proving service.
Used in functional tests to generate real ZK proofs.
"""

import json
import requests
from typing import Dict, List, Tuple, Optional
from dataclasses import dataclass


@dataclass
class WitnessData:
    """Private witness data for proof generation"""
    secret: str
    pubkey_hash: str
    country: int
    age: int
    merkle_proof: List[str]
    merkle_index: int
    merkle_leaf_hash: str

    def to_dict(self) -> dict:
        return {
            "secret": self.secret,
            "pubkey_hash": self.pubkey_hash,
            "country": self.country,
            "age": self.age,
            "merkle_proof": self.merkle_proof,
            "merkle_index": self.merkle_index,
            "merkle_leaf_hash": self.merkle_leaf_hash,
        }


class KYCProverClient:
    """Client for KYC proving service"""

    def __init__(self, base_url: str = "http://localhost:8080"):
        self.base_url = base_url.rstrip("/")

    def prove(
        self,
        chain_separator: str,
        asset_id: str,
        compliance_root: str,
        tfr_anchor: str,
        witness: WitnessData,
    ) -> Tuple[str, str]:
        """
        Generate a ZK proof.

        Args:
            chain_separator: Hex string (0x-prefixed)
            asset_id: Hex string (0x-prefixed)
            compliance_root: Hex string (0x-prefixed)
            tfr_anchor: Hex string (0x-prefixed)
            witness: Private witness data

        Returns:
            Tuple of (proof_hex, public_inputs_hex)

        Raises:
            Exception if proof generation fails
        """
        request_data = {
            "chain_separator": chain_separator,
            "asset_id": asset_id,
            "compliance_root": compliance_root,
            "tfr_anchor": tfr_anchor,
            "witness": witness.to_dict(),
        }

        response = requests.post(
            f"{self.base_url}/prove",
            json=request_data,
            headers={"Content-Type": "application/json"},
        )

        if response.status_code != 200:
            raise Exception(f"Proof generation failed: {response.text}")

        data = response.json()

        if not data.get("success"):
            raise Exception(f"Proof generation failed: {data.get('error', 'unknown error')}")

        return data["proof_hex"], data["public_inputs_hex"]

    def verify(self, proof_hex: str, public_inputs_hex: str, vk_hex: str) -> bool:
        """
        Verify a proof locally (for testing).

        Args:
            proof_hex: Proof bytes (hex, 0x-prefixed)
            public_inputs_hex: Public inputs (hex, 0x-prefixed)
            vk_hex: Verification key (hex, 0x-prefixed)

        Returns:
            True if proof is valid, False otherwise
        """
        request_data = {
            "proof_hex": proof_hex,
            "public_inputs_hex": public_inputs_hex,
            "vk_hex": vk_hex,
        }

        response = requests.post(
            f"{self.base_url}/verify",
            json=request_data,
            headers={"Content-Type": "application/json"},
        )

        if response.status_code != 200:
            return False

        data = response.json()
        return data.get("valid", False)

    def health_check(self) -> bool:
        """Check if service is running"""
        try:
            response = requests.get(f"{self.base_url}/health", timeout=5)
            return response.status_code == 200
        except:
            return False


# Example usage for tests
def example_usage():
    """
    Example: Generate a proof for testing using golden vectors.

    Requires: golden vectors must be generated first with:
        cd ../../ && ./scripts/generate_vectors.sh
    """
    import json
    import os

    client = KYCProverClient()

    # Check service is running
    if not client.health_check():
        raise Exception("KYC prover service is not running")

    # Load golden vector (REQUIRED for valid proof)
    script_dir = os.path.dirname(os.path.abspath(__file__))
    vectors_path = os.path.join(script_dir, "..", "..", "..", "vectors", "golden_vectors.json")

    if not os.path.exists(vectors_path):
        raise FileNotFoundError(
            f"Golden vectors not found at {vectors_path}. "
            f"Generate them with: cd ../../ && ./scripts/generate_vectors.sh"
        )

    with open(vectors_path) as f:
        vectors = json.load(f)

    # Find valid vector
    golden = None
    for v in vectors:
        if v["name"] == "valid":
            golden = v
            break

    if not golden:
        raise ValueError("'valid' vector not found in golden vectors")

    # Use witness from golden vector
    w = golden["witness"]
    witness = WitnessData(
        secret=w["secret"],
        pubkey_hash=w["pubkey_hash"],
        country=w["country"],
        age=w["age"],
        merkle_proof=w["merkle_proof"],
        merkle_index=w["merkle_index"],
        merkle_leaf_hash=w["merkle_leaf_hash"],
    )

    # Generate proof with public inputs from golden vector
    proof_hex, inputs_hex = client.prove(
        chain_separator=w["chain_separator"],
        asset_id=w["asset_id"],
        compliance_root=w["compliance_root"],
        tfr_anchor=w["tfr_anchor"],
        witness=witness,
    )

    print(f"✓ Using golden vector (VALID witness)")
    print(f"Proof: {proof_hex[:66]}... ({len(proof_hex)} chars)")
    print(f"Inputs: {inputs_hex[:66]}... ({len(inputs_hex)} chars)")

    return proof_hex, inputs_hex


if __name__ == "__main__":
    example_usage()

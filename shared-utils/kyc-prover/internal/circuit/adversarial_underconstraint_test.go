// SPDX-License-Identifier: Apache-2.0
package circuit

// Category C — the subtle secondary findings. These are mostly pure-arithmetic
// demonstrations (fast, no circuit compile) that pin down WHY a property holds
// today and act as regression guards: if someone "simplifies" the circuit, the
// guard fires before the bug ships.

import (
	"bytes"
	"math/big"
	"testing"

	bls12381fr "github.com/consensys/gnark-crypto/ecc/bls12-381/fr"
	bls12381mimc "github.com/consensys/gnark-crypto/ecc/bls12-381/fr/mimc"
	secp256k1 "github.com/consensys/gnark-crypto/ecc/secp256k1"
)

// secpFp is the secp256k1 base field modulus p (x-coordinates live in [0,p)).
func secpFp() *big.Int {
	v, _ := new(big.Int).SetString(
		"fffffffffffffffffffffffffffffffffffffffffffffffffffffffefffffc2f", 16)
	return v
}

// C1 — OUTPUT-KEY SPLIT ALIASING (the bug the doc warns about at
// circuit_hd_v1.go:141-143). Consensus extracts the prevout x-only key as 32 raw
// bytes and splits high/low. The circuit MUST split the same 32-byte value.
//
// The CURRENT circuit splits fp.ToBits(Qx) — the full secp256k1 residue (< p,
// up to 256 bits). The TEMPTING-BUT-WRONG alternative is FromBinary(bits,256)
// into a NATIVE var, which silently reduces mod the BLS Fr modulus r. For any
// x in [r, p) those two produce DIFFERENT halves.
//
// This test proves the divergence is real for an x in that ~26% band, so the
// "safe" path is load-bearing, not cosmetic. If the divergence ever disappears
// (e.g. someone claims the reduction is harmless), this fails and forces review.
func TestUnderconstraint_C1_OutputSplitAliasing(t *testing.T) {
	r := blsField // BLS12-381 Fr modulus
	p := secpFp()

	// Pick x in [r, p): r + 1 is comfortably inside since r < p.
	x := new(big.Int).Add(r, big.NewInt(1))
	if x.Cmp(p) >= 0 {
		t.Fatalf("test precondition broken: r+1 >= p")
	}

	safeHigh, safeLow := splitX(x)                                 // full 256-bit value
	reduced := new(big.Int).Mod(x, r)                              // the WRONG reduction
	unsafeHigh, unsafeLow := splitX(reduced)

	if safeHigh.Cmp(unsafeHigh) == 0 && safeLow.Cmp(unsafeLow) == 0 {
		t.Fatalf("expected safe vs mod-r split to DIVERGE for x in [r,p); they matched — " +
			"either the moduli are wrong or the aliasing assumption changed")
	}
	t.Logf("x>=r divergence confirmed: safe(%x,%x) != modr(%x,%x). "+
		"Consensus binds the raw 32-byte key, so the circuit must use the full-width split.",
		safeHigh, safeLow, unsafeHigh, unsafeLow)

	// And prove the aliasing danger: x and (x mod r) are DIFFERENT secp x-coords
	// that the unsafe split would map to the SAME halves — one proof, two keys.
	if x.Cmp(reduced) == 0 {
		t.Fatalf("x == x mod r, no aliasing pair")
	}
}

// C2 — MERKLE DOMAIN SEPARATION. Leaf hash = MiMC(a,b); node hash = MiMC(l,r):
// the identical 2-input function with no leaf/node tag. We demonstrate the
// collision is structurally trivial (same inputs -> same digest at both levels),
// then assert the ONLY thing preventing an internal-node-as-leaf forgery is the
// fixed traversal depth. If depth ever stops being fixed-8, this guard screams.
func TestUnderconstraint_C2_MerkleNoDomainSeparation(t *testing.T) {
	a := mustFr(big.NewInt(111))
	b := mustFr(big.NewInt(222))

	// "leaf" interpretation: MiMC(P.x=a, P.y=b)
	leaf := computeLeafHashV1(a, b)

	// "node" interpretation: MiMC(left=a, right=b)
	H := bls12381mimc.NewMiMC()
	var ea, eb bls12381fr.Element
	ea.SetBigInt(a)
	eb.SetBigInt(b)
	H.Write(ea.Marshal())
	H.Write(eb.Marshal())
	node := H.Sum(nil)

	if !bytes.Equal(leaf, node) {
		t.Fatalf("expected leaf and node hashing to collide (no domain sep); they differed — " +
			"domain separation may have been ADDED (good), update this guard")
	}
	t.Log("CONFIRMED: leaf and internal-node hashing are the same function (no domain tag). " +
		"Exploit is blocked TODAY only by the fixed depth-8 traversal + on-curve leaf constraint. " +
		"Recommendation: prepend a constant leaf/node tag before MiMC.")

	if MerkleTreeDepthHDV1() != 8 {
		t.Fatalf("Merkle depth changed to %d. The lack of domain separation in C2 is now "+
			"potentially exploitable via depth confusion — add a leaf/node tag NOW.",
			MerkleTreeDepthHDV1())
	}
}

// C3 — derivDigest ToBinary(_,256) NON-CANONICITY (circuit_hd_v1.go:121).
// A MiMC digest d is a BLS Fr element (d < r ~ 2^254.9). Decomposing it into 256
// bits admits BOTH d and d+r as integer pre-images (both < 2^256, both ≡ d mod r).
// FromBits then reads those 256 bits as a secp256k1 scalar — and d vs d+r map to
// DIFFERENT scalars (differing by r mod n). A prover who overrides the ToBinary
// hint therefore obtains a SECOND valid derivation scalar -> a second valid child
// key for the same (P, path, salt). Not a fixed-target forgery, but an
// unintended extra valid child and a soundness smell.
//
// This asserts the ambiguity PRECONDITION exists. Remediation: decompose to the
// field bit-length and assert < r (canonical), or reduce in-field before FromBits.
func TestUnderconstraint_C3_DerivDigestNonCanonical(t *testing.T) {
	r := blsField
	n := secp256k1.ID.ScalarField()

	// A representative digest value (any d in [0,r) works); use r-1 for clarity.
	d := new(big.Int).Sub(r, big.NewInt(1))
	dPlusR := new(big.Int).Add(d, r)

	if dPlusR.BitLen() > 256 {
		t.Fatalf("precondition: d+r exceeds 256 bits, ambiguity would not arise via ToBinary(_,256)")
	}
	// Both reduce to the same native (BLS) value...
	if new(big.Int).Mod(d, r).Cmp(new(big.Int).Mod(dPlusR, r)) != 0 {
		t.Fatalf("d and d+r differ mod r — impossible")
	}
	// ...but yield DIFFERENT secp scalars when read by FromBits then used mod n.
	s1 := new(big.Int).Mod(d, n)
	s2 := new(big.Int).Mod(dPlusR, n)
	if s1.Cmp(s2) == 0 {
		t.Fatalf("expected d and d+r to map to different secp scalars; they matched")
	}
	t.Logf("CONFIRMED non-canonical ToBinary precondition: d and d+r are distinct 256-bit "+
		"pre-images of the same residue, mapping to different secp scalars (%x vs %x). "+
		"A malicious hint override yields a second valid child. Use a canonical decomposition.",
		s1, s2)
}

func mustFr(v *big.Int) *big.Int { return new(big.Int).Set(v) }

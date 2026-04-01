// SPDX-License-Identifier: Apache-2.0
package circuit

// Category B — full-circuit forgery attempts.
//
// Each test crafts a witness that encodes a statement the KYC gate must forbid,
// then asserts the R1CS solver REJECTS it. A solve here = a non-KYC'd key can be
// made to pass consensus. assertSat baselines confirm the harness itself is honest.

import (
	"math/big"
	"testing"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/test"
)

func solve(assignment *TensorCashKYCCircuitHDV1) error {
	return test.IsSolved(&TensorCashKYCCircuitHDV1{}, assignment, ecc.BLS12_381.ScalarField())
}

func assertSat(t *testing.T, a *TensorCashKYCCircuitHDV1, why string) {
	t.Helper()
	if err := solve(a); err != nil {
		t.Fatalf("BASELINE BROKEN — expected SAT (%s) but solver rejected: %v", why, err)
	}
}

func assertUnsat(t *testing.T, a *TensorCashKYCCircuitHDV1, claim string) {
	t.Helper()
	if err := solve(a); err == nil {
		t.Fatalf("SOUNDNESS BREAK — solver ACCEPTED a false statement: %s", claim)
	}
}

// Sanity: the honest witness must solve, otherwise every negative test below is
// meaningless (they'd "pass" for the wrong reason).
func TestAdversarial_BaselineValidSolves(t *testing.T) {
	w := buildValidParamWitness("baseline", 0, 0, 0, big.NewInt(12345))
	assertSat(t, assignmentFrom(t, w), "honest HDv1 witness")
}

// B1 — THE headline forgery. Attacker has a key K they can sign for and wants the
// proof to bind OutputKey to K while claiming descent from an enrolled parent.
//
// Variant 1: bind to K (set output halves + child = K) but keep enrolled P.
//            Q=K must equal P + H(P,path,salt)·G — it does not. Must reject.
func TestAdversarial_B1_ForgeOutputToAttackerKey_ChildSwapped(t *testing.T) {
	w := buildValidParamWitness("b1a", 0, 0, 0, big.NewInt(7))
	_, kx, ky := keypair("attacker_spendable_key")
	high, low := splitX(kx)

	bad := clone(w)
	bad.ChildPubkeyX = BigIntToHex(kx) // attacker's key as the "child"
	bad.ChildPubkeyY = BigIntToHex(ky)
	bad.OutputKeyHigh = BigIntToHex(high) // binds consensus to a key attacker controls
	bad.OutputKeyLow = BigIntToHex(low)

	assertUnsat(t, assignmentFrom(t, bad),
		"output bound to attacker key K, but K is not P + H(P,path,salt)·G")
}

// Variant 2: keep the honest child Q in the witness (so Q=P+R holds) but lie in
// the output halves, pointing consensus at attacker key K. The output-binding
// AssertIsEqual must reject the mismatch between halves(Qx) and K halves.
func TestAdversarial_B1_ForgeOutputToAttackerKey_HalvesLie(t *testing.T) {
	w := buildValidParamWitness("b1b", 0, 0, 0, big.NewInt(8))
	_, kx, _ := keypair("attacker_spendable_key_2")
	high, low := splitX(kx)

	bad := clone(w)
	bad.OutputKeyHigh = BigIntToHex(high)
	bad.OutputKeyLow = BigIntToHex(low)

	assertUnsat(t, assignmentFrom(t, bad),
		"output halves point at K but the in-circuit child is a different point")
}

// Variant 3: only ONE half lies (the cheapest tamper). Must still reject.
func TestAdversarial_B1_ForgeOutput_SingleHalf(t *testing.T) {
	w := buildValidParamWitness("b1c", 0, 0, 0, big.NewInt(9))
	bad := clone(w)
	h, _ := new(big.Int).SetString(trim0x(bad.OutputKeyHigh), 16)
	bad.OutputKeyHigh = BigIntToHex(new(big.Int).Add(h, big.NewInt(1)))
	assertUnsat(t, assignmentFrom(t, bad), "OutputKeyHigh off by one from halves(Qx)")
}

// B2 — forge Merkle inclusion of a parent that is not in the committed tree.
// Swap the parent for another valid curve point; leaf changes, so the path no
// longer reaches the pinned root. Must reject. (Also covers "different master".)
func TestAdversarial_B2_NonEnrolledParent(t *testing.T) {
	w := buildValidParamWitness("b2", 0, 0, 0, big.NewInt(11))
	_, px, py := keypair("rogue_parent_not_in_tree")

	bad := clone(w)
	bad.MasterPubkeyX = BigIntToHex(px)
	bad.MasterPubkeyY = BigIntToHex(py)
	// ComplianceRoot stays pinned to the real on-chain root.
	assertUnsat(t, assignmentFrom(t, bad),
		"rogue parent's leaf does not hash to the committed compliance root")
}

// B2b — tamper a sibling but keep the pinned root. Classic fake Merkle proof.
func TestAdversarial_B2_TamperedSibling(t *testing.T) {
	w := buildValidParamWitness("b2b", 0, 0, 0, big.NewInt(12))
	bad := clone(w)
	s0, _ := new(big.Int).SetString(trim0x(bad.MerkleSiblings[0]), 16)
	bad.MerkleSiblings[0] = BigIntToHex(new(big.Int).Add(s0, big.NewInt(1)))
	assertUnsat(t, assignmentFrom(t, bad), "tampered sibling cannot reach the pinned root")
}

// B3 — break the parent-binding of the derivation. Change Salt without rebuilding
// the commitment: the in-circuit recomputed commitment diverges from the supplied
// one. Must reject. (If it solved, derivation wouldn't actually bind to P/path.)
func TestAdversarial_B3_DerivationCommitmentMismatch(t *testing.T) {
	w := buildValidParamWitness("b3", 0, 0, 0, big.NewInt(13))
	bad := clone(w)
	salt, _ := new(big.Int).SetString(trim0x(bad.Salt), 16)
	bad.Salt = BigIntToHex(new(big.Int).Add(salt, big.NewInt(1)))
	assertUnsat(t, assignmentFrom(t, bad), "salt changed but commitment not recomputed")
}

// B3b — supply an inconsistent child key (not P+R) while keeping honest output
// halves that match the supplied child. On-curve passes, but AssertIsEqual(Q,P+R)
// must fail. This is the "I picked my own child point" attack.
func TestAdversarial_B3_ChildNotEqualPplusR(t *testing.T) {
	w := buildValidParamWitness("b3b", 0, 0, 0, big.NewInt(14))
	_, kx, ky := keypair("self_chosen_child")
	high, low := splitX(kx)
	bad := clone(w)
	bad.ChildPubkeyX = BigIntToHex(kx)
	bad.ChildPubkeyY = BigIntToHex(ky)
	bad.OutputKeyHigh = BigIntToHex(high) // make output binding self-consistent...
	bad.OutputKeyLow = BigIntToHex(low)   // ...so only the Q=P+R check can catch it
	assertUnsat(t, assignmentFrom(t, bad),
		"self-chosen child is on-curve and matches its own halves, but != P+R")
}

// B4 — public-input guards. Zero chain separator / asset id must be rejected
// (AssertIsDifferent). A wildcard chain/asset would enable cross-chain replay.
func TestAdversarial_B4_ZeroChainSeparator(t *testing.T) {
	w := buildValidParamWitness("b4a", 0, 0, 0, big.NewInt(15))
	bad := clone(w)
	bad.ChainSeparator = BigIntToHex(big.NewInt(0))
	assertUnsat(t, assignmentFrom(t, bad), "zero chain separator must be rejected")
}

func TestAdversarial_B4_ZeroAssetID(t *testing.T) {
	w := buildValidParamWitness("b4b", 0, 0, 0, big.NewInt(16))
	bad := clone(w)
	bad.AssetID = BigIntToHex(big.NewInt(0))
	assertUnsat(t, assignmentFrom(t, bad), "zero asset id must be rejected")
}

// B5 — range/packing guards on derivation params.
// PathVector must decompose in 96 bits; a value >= 2^96 must fail ToBinary(_,96).
func TestAdversarial_B5_PathVectorOverflow(t *testing.T) {
	w := buildValidParamWitness("b5a", 0, 0, 0, big.NewInt(17))
	bad := clone(w)
	over := new(big.Int).Lsh(big.NewInt(1), 96) // 2^96
	bad.PathVector = BigIntToHex(over)
	assertUnsat(t, assignmentFrom(t, bad), "PathVector >= 2^96 must overflow the 96-bit unpack")
}

// Salt must be <= 0xffffffff. Build a fully-valid witness AT 2^32 (so only the
// range constraint can object) and assert rejection. Baseline at 2^32-1 solves.
func TestAdversarial_B5_SaltRangeBoundary(t *testing.T) {
	ok := buildValidParamWitness("b5b", 0, 0, 0, big.NewInt(0xffffffff))
	assertSat(t, assignmentFrom(t, ok), "salt == 2^32-1 is in range")

	bad := buildValidParamWitness("b5c", 0, 0, 0, new(big.Int).Lsh(big.NewInt(1), 32)) // 2^32
	assertUnsat(t, assignmentFrom(t, bad), "salt == 2^32 must exceed the uint32 bound")
}

// B6 — off-curve parent. If AssertIsOnCurve(P) is weak (e.g. broken limb range
// checks in the fork), an attacker could enroll a structured/low-order point.
// Provide a point that is NOT on secp256k1; must reject.
func TestAdversarial_B6_OffCurveParent(t *testing.T) {
	w := buildValidParamWitness("b6", 0, 0, 0, big.NewInt(19))
	bad := clone(w)
	// Take a valid x but flip y to (y+1): overwhelmingly off-curve.
	py, _ := new(big.Int).SetString(trim0x(bad.MasterPubkeyY), 16)
	bad.MasterPubkeyY = BigIntToHex(new(big.Int).Add(py, big.NewInt(1)))
	assertUnsat(t, assignmentFrom(t, bad), "off-curve parent (y+1) must be rejected")
}

// B6b — off-curve CHILD. Same idea on Q; AssertIsOnCurve(Q) must catch it
// independent of the P+R equality.
func TestAdversarial_B6_OffCurveChild(t *testing.T) {
	w := buildValidParamWitness("b6b", 0, 0, 0, big.NewInt(20))
	bad := clone(w)
	qy, _ := new(big.Int).SetString(trim0x(bad.ChildPubkeyY), 16)
	bad.ChildPubkeyY = BigIntToHex(new(big.Int).Add(qy, big.NewInt(1)))
	assertUnsat(t, assignmentFrom(t, bad), "off-curve child (y+1) must be rejected")
}

func trim0x(s string) string {
	if len(s) >= 2 && (s[:2] == "0x" || s[:2] == "0X") {
		return s[2:]
	}
	return s
}

var _ = frontend.Variable(nil)

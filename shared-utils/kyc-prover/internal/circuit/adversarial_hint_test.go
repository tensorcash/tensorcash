// SPDX-License-Identifier: Apache-2.0
package circuit

// Malicious-prover / non-canonical-limb harness for the output-key binding.
//
// Reviewer's correct critique: the earlier emulated test used emulated.ValueOf,
// which always produces canonical limbs — it does NOT probe what a malicious
// prover can do by hand-crafting an emulated.Element with overflowed limbs.
//
// gnark's own source (std/math/emulated/element.go) documents the defense: an
// Element carries an unexported `internal` flag; witness/user-constructed
// elements have internal=false, so Field methods MUST call enforceWidth to
// range-check each limb. The `Limbs` field IS exported, so we CAN build a
// non-canonical element directly and check whether that enforcement actually
// fires on the exact code path the KYC circuit uses for the output-key split:
//
//     q := fp.Reduce(&Qx); bits := fp.ToBits(q); halves -> AssertIsEqual(public)
//
// If a crafted Qx with out-of-range limbs is ACCEPTED, the attacker can decouple
// the bound output halves from the true child x-coordinate => output-binding
// forgery (bind the proof to a key they can sign for). If it is REJECTED, the
// binding is sound against this class.

import (
	"math/big"
	"testing"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/frontend"
	stdbits "github.com/consensys/gnark/std/math/bits"
	"github.com/consensys/gnark/std/math/emulated"
	"github.com/consensys/gnark/test"
)

// outputBindProbe is a faithful copy of circuit_hd_v1.go:144-155 (Section 4).
type outputBindProbe struct {
	Qx      emulated.Element[Secp256k1Fp]
	KeyHigh frontend.Variable `gnark:",public"`
	KeyLow  frontend.Variable `gnark:",public"`
}

func (c *outputBindProbe) Define(api frontend.API) error {
	fp, err := emulated.NewField[Secp256k1Fp](api)
	if err != nil {
		return err
	}
	q := fp.Reduce(&c.Qx)
	childXBits := fp.ToBits(q)
	lowBits := childXBits[0:128]
	highBits := childXBits[128:256]
	computedLow := stdbits.FromBinary(api, lowBits, stdbits.WithNbDigits(128))
	computedHigh := stdbits.FromBinary(api, highBits, stdbits.WithNbDigits(128))
	api.AssertIsEqual(computedHigh, c.KeyHigh)
	api.AssertIsEqual(computedLow, c.KeyLow)
	return nil
}

func fpParams() (nbLimbs int, bitsPerLimb uint, modulus *big.Int) {
	var fp Secp256k1Fp
	return int(fp.NbLimbs()), fp.BitsPerLimb(), fp.Modulus()
}

// canonicalLimbs decomposes v into nbLimbs little-endian limbs of bitsPerLimb.
func canonicalLimbs(v *big.Int) []frontend.Variable {
	nbLimbs, bits, _ := fpParams()
	mask := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), bits), big.NewInt(1))
	out := make([]frontend.Variable, nbLimbs)
	tmp := new(big.Int).Set(v)
	for i := 0; i < nbLimbs; i++ {
		out[i] = new(big.Int).And(tmp, mask)
		tmp.Rsh(tmp, bits)
	}
	return out
}

func solveProbe(qx emulated.Element[Secp256k1Fp], high, low *big.Int) error {
	return test.IsSolved(
		&outputBindProbe{},
		&outputBindProbe{Qx: qx, KeyHigh: high, KeyLow: low},
		ecc.BLS12_381.ScalarField(),
	)
}

// Baseline: canonical limbs, honest halves -> must SOLVE.
func TestHint_OutputBind_BaselineSolves(t *testing.T) {
	_, x, _ := keypair("hint_baseline_key")
	high, low := splitX(x)
	if err := solveProbe(emulated.ValueOf[Secp256k1Fp](x), high, low); err != nil {
		t.Fatalf("baseline output-bind probe rejected honest input: %v", err)
	}
}

// Sanity: canonical limbs, WRONG halves -> must REJECT (the AssertIsEqual works).
func TestHint_OutputBind_WrongHalvesRejected(t *testing.T) {
	_, x, _ := keypair("hint_wrong_key")
	_, kx, _ := keypair("hint_attacker_key")
	high, low := splitX(kx) // attacker's halves, not x's
	if err := solveProbe(emulated.ValueOf[Secp256k1Fp](x), high, low); err == nil {
		t.Fatal("probe accepted halves that don't match the value")
	}
}

// THE TEST: hand-crafted NON-CANONICAL limbs.
// Encode the same integer x but overflow limb[0] by +2^bitsPerLimb and
// compensate limb[1] by -1 (integer value unchanged). internal=false, so the
// circuit must enforceWidth and REJECT the out-of-range limb[0].
//
// If this SOLVES, witness limb widths are NOT enforced on the Reduce/ToBits
// path the KYC output binding uses -> investigate immediately, it is the
// gateway to an output-binding forgery.
func TestHint_OutputBind_NonCanonicalLimbsRejected(t *testing.T) {
	nbLimbs, bits, _ := fpParams()
	if nbLimbs < 2 {
		t.Skip("need >=2 limbs")
	}
	_, x, _ := keypair("hint_noncanon_key")
	high, low := splitX(x)

	limbs := canonicalLimbs(x)
	base := new(big.Int).Lsh(big.NewInt(1), bits) // 2^bitsPerLimb
	l0 := new(big.Int).Add(limbs[0].(*big.Int), base)
	l1 := new(big.Int).Sub(limbs[1].(*big.Int), big.NewInt(1))
	limbs[0] = l0 // now >= 2^bitsPerLimb (out of range)
	limbs[1] = l1 // integer value of the element is unchanged

	bad := emulated.Element[Secp256k1Fp]{Limbs: limbs}
	err := solveProbe(bad, high, low)
	if err == nil {
		t.Fatal("SOUNDNESS ALERT — overflowed witness limbs were accepted on the " +
			"Reduce/ToBits output-binding path. enforceWidth is not firing; an attacker " +
			"may be able to decouple the bound output halves from the true child key.")
	}
	t.Logf("non-canonical limbs correctly rejected: %v", err)
}

// Stronger variant: overflowed limbs AND attacker-chosen halves. Even if width
// enforcement had a gap, this asserts the full forgery (bind to a key K the
// attacker controls) does not go through.
func TestHint_OutputBind_NonCanonicalForgeryRejected(t *testing.T) {
	nbLimbs, bits, _ := fpParams()
	if nbLimbs < 2 {
		t.Skip("need >=2 limbs")
	}
	_, x, _ := keypair("hint_forge_realchild")
	_, kx, _ := keypair("hint_forge_attackerkey")
	high, low := splitX(kx) // bind to attacker's spendable key

	limbs := canonicalLimbs(x)
	base := new(big.Int).Lsh(big.NewInt(1), bits)
	limbs[0] = new(big.Int).Add(limbs[0].(*big.Int), base)
	limbs[1] = new(big.Int).Sub(limbs[1].(*big.Int), big.NewInt(1))

	bad := emulated.Element[Secp256k1Fp]{Limbs: limbs}
	if err := solveProbe(bad, high, low); err == nil {
		t.Fatal("SOUNDNESS BREAK — output bound to attacker key K via non-canonical limbs.")
	}
}

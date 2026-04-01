// SPDX-License-Identifier: Apache-2.0
package circuit

// Category A — direct assault on the hand-patched gnark fork
// (tensorcash/gnark v0.9.1-plain-rangecheck).
//
// The fork disables the commitment-backed rangecheck Committer path in
// std/rangecheck/rangecheck.go so proofs stay vanilla-Groth16 (192 bytes,
// gamma_abc == 6). If that patch ALSO weakened the fallback bit-decomposition
// checker, then every emulated-secp256k1 constraint in the KYC circuit
// (AssertIsOnCurve, point Add, AssertIsEqual, ToBits output split) silently
// becomes forgeable — a total, no-trusted-setup break.
//
// rangecheck.New(api).Check(v, n) is the single primitive the whole emulated
// stack rests on. We hit it directly: it MUST reject any v >= 2^n. If IsSolved
// accepts an out-of-range value, the fork nooped the checker. Maximum embarrassment.

import (
	"math/big"
	"testing"

	"github.com/consensys/gnark-crypto/ecc"
	"github.com/consensys/gnark/frontend"
	"github.com/consensys/gnark/std/math/emulated"
	"github.com/consensys/gnark/std/rangecheck"
	"github.com/consensys/gnark/test"
)

// rangeCircuit asserts V fits in NbBits bits via the (forked) rangecheck gadget.
type rangeCircuit struct {
	V      frontend.Variable
	NbBits int
}

func (c *rangeCircuit) Define(api frontend.API) error {
	rc := rangecheck.New(api)
	rc.Check(c.V, c.NbBits)
	return nil
}

func solveRange(nbBits int, v *big.Int) error {
	return test.IsSolved(
		&rangeCircuit{NbBits: nbBits},
		&rangeCircuit{NbBits: nbBits, V: v},
		ecc.BLS12_381.ScalarField(),
	)
}

// A1 — the decisive fork test. For a spread of bit widths, in-range values must
// solve and out-of-range values (2^n, 2^n+1, and a big multiple) must be rejected.
func TestForkRangeCheck_RejectsOutOfRange(t *testing.T) {
	widths := []int{4, 8, 16, 32, 64, 96, 128, 200, 252}
	for _, n := range widths {
		n := n
		t.Run(itoa(n)+"bits", func(t *testing.T) {
			maxIn := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), uint(n)), big.NewInt(1)) // 2^n - 1
			if err := solveRange(n, maxIn); err != nil {
				t.Fatalf("in-range value 2^%d-1 was rejected (checker too strict / broken): %v", n, err)
			}
			if err := solveRange(n, big.NewInt(0)); err != nil {
				t.Fatalf("zero rejected at %d bits: %v", n, err)
			}

			over := new(big.Int).Lsh(big.NewInt(1), uint(n)) // 2^n
			if err := solveRange(n, over); err == nil {
				t.Fatalf("SOUNDNESS BREAK — rangecheck accepted 2^%d in a %d-bit range. "+
					"The fork's fallback checker is not constraining limb width; "+
					"all emulated secp256k1 arithmetic in the KYC circuit is forgeable.", n, n)
			}
			over1 := new(big.Int).Add(over, big.NewInt(1)) // 2^n + 1
			if err := solveRange(n, over1); err == nil {
				t.Fatalf("SOUNDNESS BREAK — rangecheck accepted 2^%d+1 in a %d-bit range.", n, n)
			}
			big2 := new(big.Int).Lsh(big.NewInt(1), uint(n+40)) // way over
			if err := solveRange(n, big2); err == nil {
				t.Fatalf("SOUNDNESS BREAK — rangecheck accepted 2^%d in a %d-bit range.", n+40, n)
			}
		})
	}
}

// A2 — exercise the SAME gadget the circuit actually uses for the output-key
// split: 128-bit halves. If a 128-bit check is loose, an attacker could fit a
// >128-bit half and alias the output key. The circuit decomposes via fp.ToBits
// (256) then re-packs 128-bit halves; here we pin the 128-bit width directly.
func TestForkRangeCheck_OutputHalfWidth128(t *testing.T) {
	in := new(big.Int).Sub(new(big.Int).Lsh(big.NewInt(1), 128), big.NewInt(1)) // 2^128-1
	if err := solveRange(128, in); err != nil {
		t.Fatalf("128-bit in-range value rejected: %v", err)
	}
	if err := solveRange(128, new(big.Int).Lsh(big.NewInt(1), 128)); err == nil {
		t.Fatal("SOUNDNESS BREAK — a 129-bit value passed a 128-bit range check (output-key aliasing risk).")
	}
}

// emulatedOnCurve is a minimal probe over the SAME emulated curve the KYC circuit
// uses. The on-curve assertion's soundness depends entirely on the fork's limb
// range checks (via emulated reduction). An off-curve point must be rejected.
type emulatedReduceProbe struct {
	X emulated.Element[emulated.Secp256k1Fp]
	// Asserts X reduces to a canonical element equal to a public constant.
	Want emulated.Element[emulated.Secp256k1Fp]
}

func (c *emulatedReduceProbe) Define(api frontend.API) error {
	fp, err := emulated.NewField[emulated.Secp256k1Fp](api)
	if err != nil {
		return err
	}
	r := fp.Reduce(&c.X)
	fp.AssertIsEqual(r, &c.Want)
	return nil
}

// A3 — emulated reduction end-to-end. A canonical value equal to Want solves;
// a different value must not. This confirms the emulated layer (which is where
// AssertIsOnCurve / Add / equality all bottom out) actually pins residues.
func TestForkEmulated_ReduceBindsResidue(t *testing.T) {
	want := big.NewInt(0x1234abcd)
	good := &emulatedReduceProbe{
		X:    emulated.ValueOf[emulated.Secp256k1Fp](want),
		Want: emulated.ValueOf[emulated.Secp256k1Fp](want),
	}
	if err := test.IsSolved(&emulatedReduceProbe{}, good, ecc.BLS12_381.ScalarField()); err != nil {
		t.Fatalf("emulated reduce of a canonical value was rejected: %v", err)
	}

	bad := &emulatedReduceProbe{
		X:    emulated.ValueOf[emulated.Secp256k1Fp](big.NewInt(0x1234abce)), // off by one
		Want: emulated.ValueOf[emulated.Secp256k1Fp](want),
	}
	if err := test.IsSolved(&emulatedReduceProbe{}, bad, ecc.BLS12_381.ScalarField()); err == nil {
		t.Fatal("SOUNDNESS BREAK — emulated reduce equated two different residues.")
	}
}

func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	neg := n < 0
	if neg {
		n = -n
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	if neg {
		i--
		b[i] = '-'
	}
	return string(b[i:])
}

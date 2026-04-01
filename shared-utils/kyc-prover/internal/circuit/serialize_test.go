// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"bytes"
	"encoding/binary"
	"testing"

	bls12381 "github.com/consensys/gnark/backend/groth16/bls12-381"
)

// helper to build a prover and valid witness for serialization tests
func buildTestProver(t *testing.T) (*Prover, *ValidWitnessData) {
	t.Helper()

	setup, err := Setup()
	if err != nil {
		t.Fatalf("setup failed: %v", err)
	}

	prover, err := NewProver(setup.ProvingKey, setup.VerificationKey)
	if err != nil {
		t.Fatalf("new prover failed: %v", err)
	}

	witness, err := GenerateValidWitness("serialize_roundtrip")
	if err != nil {
		t.Fatalf("generate witness failed: %v", err)
	}

	return prover, witness
}

func TestSerializeProofCustomMatchesMarshal(t *testing.T) {
	prover, witness := buildTestProver(t)

	proof, _, err := prover.ProveRaw(witness.ToProveRequest())
	if err != nil {
		t.Fatalf("ProveRaw failed: %v", err)
	}

	proofBytes, err := SerializeProofCustom(proof)
	if err != nil {
		t.Fatalf("SerializeProofCustom failed: %v", err)
	}

	proofBLS, ok := proof.(*bls12381.Proof)
	if !ok {
		t.Fatalf("unexpected proof type %T", proof)
	}

	expected := make([]byte, 0, 48+96+48)
	ar := proofBLS.Ar.Bytes()
	expected = append(expected, ar[:]...)
	bs := proofBLS.Bs.Bytes()
	expected = append(expected, bs[:]...)
	krs := proofBLS.Krs.Bytes()
	expected = append(expected, krs[:]...)

	if !bytes.Equal(proofBytes, expected) {
		t.Fatalf("custom proof bytes mismatch\nexpected: %x\nactual:   %x", expected, proofBytes)
	}
}

func TestSerializeVKCustomMatchesMarshal(t *testing.T) {
	prover, _ := buildTestProver(t)

	vkBytes, err := SerializeVKCustom(prover.vk)
	if err != nil {
		t.Fatalf("SerializeVKCustom failed: %v", err)
	}

	vkBLS, ok := prover.vk.(*bls12381.VerifyingKey)
	if !ok {
		t.Fatalf("unexpected vk type %T", prover.vk)
	}

	nbPublic := prover.vk.NbPublicWitness()
	if got := binary.LittleEndian.Uint16(vkBytes[:2]); got != uint16(nbPublic) {
		t.Fatalf("gamma_abc count mismatch: expected %d, got %d", nbPublic, got)
	}

	offset := 2

	alpha := vkBLS.G1.Alpha.Bytes()
	if !bytes.Equal(vkBytes[offset:offset+48], alpha[:]) {
		t.Fatalf("alpha mismatch")
	}
	offset += 48

	beta := vkBLS.G2.Beta.Bytes()
	if !bytes.Equal(vkBytes[offset:offset+96], beta[:]) {
		t.Fatalf("beta mismatch")
	}
	offset += 96

	gamma := vkBLS.G2.Gamma.Bytes()
	if !bytes.Equal(vkBytes[offset:offset+96], gamma[:]) {
		t.Fatalf("gamma mismatch")
	}
	offset += 96

	delta := vkBLS.G2.Delta.Bytes()
	if !bytes.Equal(vkBytes[offset:offset+96], delta[:]) {
		t.Fatalf("delta mismatch")
	}
	offset += 96

	if len(vkBLS.G1.K) < int(nbPublic)+1 {
		t.Fatalf("gamma_abc slice too short: have %d need %d", len(vkBLS.G1.K), int(nbPublic)+1)
	}

	for i := 0; i <= int(nbPublic); i++ {
		ki := vkBLS.G1.K[i].Bytes()
		if !bytes.Equal(vkBytes[offset:offset+48], ki[:]) {
			t.Fatalf("gamma_abc[%d] mismatch", i)
		}
		offset += 48
	}

	if offset != len(vkBytes) {
		t.Fatalf("unexpected trailing bytes: consumed %d of %d", offset, len(vkBytes))
	}
}

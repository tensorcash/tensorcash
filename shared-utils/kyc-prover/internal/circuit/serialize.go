// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"encoding/binary"
	"fmt"

	"github.com/consensys/gnark/backend/groth16"
	bls12381 "github.com/consensys/gnark/backend/groth16/bls12-381"
)

// SerializeVKCustom serializes a Groth16 verifying key in BLST-compatible format
// Format: [gamma_count:2][alpha:48][beta:96][gamma:96][delta:96][K array:48*(n+1)]

func SerializeVKCustom(vk groth16.VerifyingKey) ([]byte, error) {
	vkBLS, ok := vk.(*bls12381.VerifyingKey)
	if !ok {
		return nil, fmt.Errorf("vk is not a BLS12-381 verifying key")
	}

	nbPublic := vk.NbPublicWitness()

	const g1Size = 48
	const g2Size = 96

	customSize := 2 + g1Size + 3*g2Size + (int(nbPublic)+1)*g1Size
	result := make([]byte, customSize)

	binary.LittleEndian.PutUint16(result[0:2], uint16(nbPublic))
	offset := 2

    alpha := vkBLS.G1.Alpha.Bytes()
    copy(result[offset:], alpha[:])
    offset += g1Size

    beta := vkBLS.G2.Beta.Bytes()
    copy(result[offset:], beta[:])
    offset += g2Size

    gamma := vkBLS.G2.Gamma.Bytes()
    copy(result[offset:], gamma[:])
    offset += g2Size

    delta := vkBLS.G2.Delta.Bytes()
    copy(result[offset:], delta[:])
    offset += g2Size

	if len(vkBLS.G1.K) < int(nbPublic)+1 {
		return nil, fmt.Errorf("verifying key gamma_abc length mismatch: have %d, want %d", len(vkBLS.G1.K), int(nbPublic)+1)
	}

	for i := 0; i <= int(nbPublic); i++ {
        ki := vkBLS.G1.K[i].Bytes()
        copy(result[offset:], ki[:])
        offset += g1Size
    }

	return result, nil
}

// SerializeProofCustom serializes a Groth16 proof in BLST-compatible format
// Format: [A:48][B:96][C:48] = 192 bytes total (matches C++ BLST verifier expectation)
func SerializeProofCustom(proof groth16.Proof) ([]byte, error) {
	proofBLS, ok := proof.(*bls12381.Proof)
	if !ok {
		return nil, fmt.Errorf("proof is not a BLS12-381 proof")
	}

	const (
		g1CompressedSize = 48
		g2CompressedSize = 96
	)

	result := make([]byte, g1CompressedSize+g2CompressedSize+g1CompressedSize)
	offset := 0

    ar := proofBLS.Ar.Bytes()
    copy(result[offset:], ar[:])
    offset += g1CompressedSize

    bs := proofBLS.Bs.Bytes()
    copy(result[offset:], bs[:])
    offset += g2CompressedSize

    krs := proofBLS.Krs.Bytes()
    copy(result[offset:], krs[:])

	return result, nil
}

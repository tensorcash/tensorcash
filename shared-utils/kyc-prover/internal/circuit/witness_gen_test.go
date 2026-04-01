// SPDX-License-Identifier: Apache-2.0
package circuit

import (
	"math/big"
	"testing"
)

func TestGenerateValidWitness(t *testing.T) {
	witness, err := GenerateValidWitness("test_seed")
	if err != nil {
		t.Fatalf("GenerateValidWitness failed: %v", err)
	}

	// Check all fields are populated
	if witness.Secret == "" {
		t.Error("Secret is empty")
	}
	if witness.PubkeyHash == "" {
		t.Error("PubkeyHash is empty")
	}
	if witness.Country != 840 {
		t.Errorf("Country should be 840, got %d", witness.Country)
	}
	if witness.Age < 18 {
		t.Errorf("Age should be >= 18, got %d", witness.Age)
	}
	if len(witness.MerkleProof) != 8 {
		t.Errorf("MerkleProof should have 8 elements, got %d", len(witness.MerkleProof))
	}
	if witness.MerkleLeafHash == "" {
		t.Error("MerkleLeafHash is empty")
	}
}

func TestGenerateValidWitnessDeterministic(t *testing.T) {
	// Same seed should produce same witness
	w1, err := GenerateValidWitness("deterministic_seed")
	if err != nil {
		t.Fatalf("Failed to generate first witness: %v", err)
	}

	w2, err := GenerateValidWitness("deterministic_seed")
	if err != nil {
		t.Fatalf("Failed to generate second witness: %v", err)
	}

	if w1.Secret != w2.Secret {
		t.Error("Secrets differ with same seed (not deterministic)")
	}
	if w1.PubkeyHash != w2.PubkeyHash {
		t.Error("PubkeyHash differs with same seed (not deterministic)")
	}
}

func TestGenerateInvalidWitness(t *testing.T) {
	tests := []struct {
		name        string
		invalidType InvalidWitnessType
	}{
		{"invalid_secret", InvalidSecret},
		{"invalid_age", InvalidAge},
		{"invalid_country", InvalidCountry},
		{"invalid_merkle", InvalidMerkleProof},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			witness, err := GenerateInvalidWitness("test_seed", tt.invalidType)
			if err != nil {
				t.Fatalf("GenerateInvalidWitness failed: %v", err)
			}

			// Check that appropriate field was corrupted
			switch tt.invalidType {
			case InvalidAge:
				if witness.Age >= 18 {
					t.Errorf("Invalid age witness should have age < 18, got %d", witness.Age)
				}
			case InvalidCountry:
				if witness.Country == 840 {
					t.Error("Invalid country witness should not have country = 840")
				}
			}
		})
	}
}

func TestHexToBigInt(t *testing.T) {
	tests := []struct {
		name    string
		input   string
		wantErr bool
	}{
		{"with_0x_prefix", "0x1234", false},
		{"without_0x_prefix", "1234", false},
		{"empty", "", false},
		{"invalid_hex", "xyz", true},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result, err := HexToBigInt(tt.input)
			if (err != nil) != tt.wantErr {
				t.Errorf("HexToBigInt() error = %v, wantErr %v", err, tt.wantErr)
				return
			}
			if !tt.wantErr && result == nil {
				t.Error("HexToBigInt() returned nil without error")
			}
		})
	}
}

func TestBigIntToHex(t *testing.T) {
	tests := []struct {
		name  string
		input *big.Int
		want  string
	}{
		{
			"zero",
			big.NewInt(0),
			"0x0000000000000000000000000000000000000000000000000000000000000000",
		},
		{
			"small_number",
			big.NewInt(255),
			"0x00000000000000000000000000000000000000000000000000000000000000ff",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			result := BigIntToHex(tt.input)
			if result != tt.want {
				t.Errorf("BigIntToHex() = %v, want %v", result, tt.want)
			}

			// Verify it's 0x-prefixed and 66 chars (0x + 64 hex chars)
			if len(result) != 66 {
				t.Errorf("BigIntToHex() length = %d, want 66", len(result))
			}
			if result[:2] != "0x" {
				t.Error("BigIntToHex() should have 0x prefix")
			}
		})
	}
}

func TestHexRoundTrip(t *testing.T) {
	// Test that BigIntToHex and HexToBigInt are inverses
	original := big.NewInt(12345678)

	hex := BigIntToHex(original)
	decoded, err := HexToBigInt(hex)
	if err != nil {
		t.Fatalf("HexToBigInt failed: %v", err)
	}

	if original.Cmp(decoded) != 0 {
		t.Errorf("Round trip failed: got %v, want %v", decoded, original)
	}
}

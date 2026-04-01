# SPDX-License-Identifier: Apache-2.0
"""Test uint256_arithmetics for Bitcoin-compatible difficulty calculations."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from uint256_arithmetics import get_compact, set_compact, adjust_nbits_by_multiplier
    HAS_UINT256 = True
except ImportError:
    HAS_UINT256 = False


@pytest.mark.skipif(not HAS_UINT256, reason="uint256_arithmetics not available")
class TestUint256Arithmetics:
    """Test 256-bit arithmetic operations for difficulty adjustments."""
    
    def test_get_set_compact_roundtrip(self):
        """Test that get_compact/set_compact roundtrip preserves values."""
        # Test various target values (256-bit integers)
        test_values = [
            0x00000000ffff0000000000000000000000000000000000000000000000000000,  # Bitcoin genesis
            0x00000000000404cb000000000000000000000000000000000000000000000000,  # Higher difficulty
            0x00000000000000001b7b74000000000000000000000000000000000000000000,  # Even higher
            0x0000000000000000000000000000000000000000000000000000000000000001,  # Minimum
            0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff,  # Maximum
        ]
        
        for original in test_values:
            # Convert to compact
            compact = get_compact(original)
            assert isinstance(compact, int)
            assert 0 <= compact <= 0xffffffff  # 32-bit value
            
            # Convert back (returns tuple: value, negative, overflow)
            recovered, negative, overflow = set_compact(compact)
            
            # Check no errors
            assert not negative, f"Unexpected negative flag for {original:064x}"
            assert not overflow, f"Unexpected overflow flag for {original:064x}"
            
            # For very large/small values, some precision loss is expected
            # The compact format only has 24 bits of precision
            if original > 0:
                # Calculate relative error
                error = abs(recovered - original) / original
                # Allow up to 1% error due to compact format limitations
                assert error < 0.01, \
                    f"Roundtrip failed: {original:064x} -> {compact:08x} -> {recovered:064x}, error: {error:.2%}"
    
    def test_get_compact_known_values(self):
        """Test get_compact with known Bitcoin values."""
        # Known test vectors from Bitcoin
        test_cases = [
            # (target, expected_compact)
            (0x00000000ffff0000000000000000000000000000000000000000000000000000, 0x1d00ffff),
            (0x00000000000404cb000000000000000000000000000000000000000000000000, 0x1b0404cb),
            (0x0000000000000000000000000000000000000000000000000000000000000000, 0x00000000),  # Zero
        ]
        
        for target, expected_compact in test_cases:
            compact = get_compact(target)
            assert compact == expected_compact, \
                f"get_compact({target:064x}) = {compact:08x}, expected {expected_compact:08x}"
    
    def test_set_compact_known_values(self):
        """Test set_compact with known Bitcoin values."""
        test_cases = [
            # (compact, expected_target)
            (0x1d00ffff, 0x00000000ffff0000000000000000000000000000000000000000000000000000),
            (0x1b0404cb, 0x00000000000404cb000000000000000000000000000000000000000000000000),
            (0x00000000, 0x0000000000000000000000000000000000000000000000000000000000000000),  # Zero
        ]
        
        for compact, expected_target in test_cases:
            target, negative, overflow = set_compact(compact)
            assert not negative and not overflow
            # Allow small differences due to precision
            if expected_target > 0:
                error = abs(target - expected_target) / expected_target
                assert error < 0.001, \
                    f"set_compact({compact:08x}) = {target:064x}, expected {expected_target:064x}"
            else:
                assert target == expected_target
    
    def test_adjust_difficulty_harder(self):
        """Test making difficulty harder (smaller target)."""
        # Base difficulty (Bitcoin genesis)
        base_nbits = 0x1d00ffff
        
        # Make 2x harder (multiply target by 1, divide by 2)
        result = adjust_nbits_by_multiplier(base_nbits, 1, 2)
        
        # New target should be approximately half
        base_target = 0x00000000ffff0000000000000000000000000000000000000000000000000000
        new_target = int.from_bytes(result['target_bytes'], byteorder='big')
        expected = base_target // 2
        
        assert abs(new_target - expected) < expected * 0.1  # Within 10% (compact format loses precision)
        assert not result['overflow']
        assert not result['negative']
    
    def test_adjust_difficulty_easier(self):
        """Test making difficulty easier (larger target)."""
        base_nbits = 0x1d00ffff
        
        # Make 4x easier (multiply target by 4, divide by 1)
        result = adjust_nbits_by_multiplier(base_nbits, 4, 1)
        
        # Target should be approximately 4x
        base_target = 0x00000000ffff0000000000000000000000000000000000000000000000000000
        new_target = int.from_bytes(result['target_bytes'], byteorder='big')
        expected = base_target * 4
        
        assert abs(new_target - expected) < expected * 0.1  # Within 10%
        assert not result['overflow']
        assert not result['negative']
    
    def test_difficulty_adjustment_limits(self):
        """Test Bitcoin's difficulty adjustment limits (4x max change)."""
        base_nbits = 0x1d00ffff
        base_target = 0x00000000ffff0000000000000000000000000000000000000000000000000000
        
        # Maximum increase (4x easier)
        result_easier = adjust_nbits_by_multiplier(base_nbits, 4, 1)
        max_easier = int.from_bytes(result_easier['target_bytes'], byteorder='big')
        assert max_easier <= base_target * 4.2  # Allow precision loss
        assert max_easier >= base_target * 3.8
        
        # Maximum decrease (4x harder)
        result_harder = adjust_nbits_by_multiplier(base_nbits, 1, 4)
        max_harder = int.from_bytes(result_harder['target_bytes'], byteorder='big')
        assert max_harder >= base_target // 4.2
        assert max_harder <= base_target // 3.8
    
    def test_edge_cases(self):
        """Test edge cases and boundary conditions."""
        # Test with maximum value
        max_val = 0xffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff
        compact = get_compact(max_val)
        recovered, negative, overflow = set_compact(compact)
        
        # Should handle without crashing
        assert isinstance(recovered, int)
        
        # Test with minimum positive value
        min_val = 1
        compact = get_compact(min_val)
        recovered, negative, overflow = set_compact(compact)
        assert recovered >= 0
        
        # Test with zero
        zero_compact = get_compact(0)
        assert zero_compact == 0
        recovered, negative, overflow = set_compact(0)
        assert recovered == 0
        assert not negative
        assert not overflow
    
    def test_property_compact_preserves_ordering(self):
        """Property test: compact format should preserve ordering."""
        # Generate random test values
        import random
        
        values = []
        for exp in [10, 20, 30, 40, 50, 60]:  # Various magnitudes
            val = random.randint(2**exp, 2**(exp+5))
            values.append(val)
        
        values.sort()
        
        # Convert to compact
        compacts = [get_compact(v) for v in values]
        
        # Check that ordering is mostly preserved
        # (some adjacent values might have same compact due to precision loss)
        inversions = 0
        for i in range(len(compacts) - 1):
            if compacts[i] > compacts[i+1]:
                inversions += 1
        
        # Allow some inversions due to precision loss
        assert inversions <= len(compacts) // 4, \
            f"Too many ordering inversions: {inversions}/{len(compacts)}"
    
    def test_property_roundtrip_stability(self):
        """Property test: multiple roundtrips should stabilize."""
        import random
        
        for _ in range(10):
            # Random 256-bit value
            original = random.randint(1, 2**256 - 1)
            
            # First roundtrip
            compact1 = get_compact(original)
            recovered1, _, _ = set_compact(compact1)
            
            # Second roundtrip
            compact2 = get_compact(recovered1)
            recovered2, _, _ = set_compact(compact2)
            
            # Should stabilize after first roundtrip
            assert compact1 == compact2, \
                f"Compact not stable: {compact1:08x} -> {compact2:08x}"
            assert abs(recovered1 - recovered2) < max(recovered1, recovered2) * 0.001, \
                f"Value not stable: {recovered1:064x} -> {recovered2:064x}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
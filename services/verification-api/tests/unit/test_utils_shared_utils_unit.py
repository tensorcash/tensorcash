# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for shared_utils.py utility functions
"""

import pytest
import numpy as np
import base64
import re
from unittest.mock import patch, MagicMock, mock_open
import sys
import os

# Add the source directory to Python path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../src'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../../../../shared-utils/fb-schemas'))

# Mock the heavy dependencies before importing shared_utils
with patch.dict('sys.modules', {
    'proof': MagicMock(),
    'proof.Proof': MagicMock(),
    'proof.FloatArray': MagicMock(),
    'proof.UIntArray': MagicMock(),
    'proof.MiningResponse': MagicMock(),
    'config.constants': MagicMock(),
    'chiavdf': MagicMock(),
    'torch': MagicMock(),
    'torch.distributions': MagicMock(),
    'torch.distributions.normal': MagicMock(),
    'huggingface_hub': MagicMock(),
    'sklearn.covariance': MagicMock(),
    'sklearn.decomposition': MagicMock(),
}):
    # Import the actual functions from shared_utils
    from utils.shared_utils import (
        string_to_bytes,
        validate_by_quantiles,
        validate_by_quantiles_higher, 
        validate_by_quantiles_lower
    )


class TestStringToBytes:
    """Test the string_to_bytes function"""
    
    def test_bytes_input(self):
        """Test that bytes input is returned unchanged"""
        data = b"hello world"
        result = string_to_bytes(data)
        assert result == data
    
    def test_hex_string(self):
        """Test hex string conversion"""
        hex_str = "48656c6c6f20576f726c64"  # "Hello World" in hex
        expected = b"Hello World"
        result = string_to_bytes(hex_str)
        assert result == expected
    
    def test_hex_with_whitespace(self):
        """Test hex string with whitespace is cleaned"""
        hex_str = "48 65 6c 6c 6f\n20 57 6f 72 6c 64"
        expected = b"Hello World"
        result = string_to_bytes(hex_str)
        assert result == expected
    
    def test_base64_string(self):
        """Test base64 string conversion"""
        b64_str = base64.b64encode(b"Hello World").decode()
        expected = b"Hello World"
        result = string_to_bytes(b64_str)
        assert result == expected
    
    def test_base64_with_whitespace(self):
        """Test base64 string with whitespace"""
        b64_str = " " + base64.b64encode(b"Hello World").decode() + " \n"
        expected = b"Hello World"
        result = string_to_bytes(b64_str)
        assert result == expected
    
    def test_invalid_input_type(self):
        """Test that non-string/bytes input raises ValueError"""
        with pytest.raises(ValueError, match="Expected string or bytes"):
            string_to_bytes(123)
    
    def test_invalid_hex(self):
        """Test invalid hex string raises ValueError"""
        with pytest.raises(ValueError, match="Failed to decode string"):
            string_to_bytes("invalid_hex_string")
    
    def test_odd_length_hex_fallback_to_base64(self):
        """Test that odd-length strings fall back to base64"""
        # Create a valid base64 string that would fail hex
        b64_str = base64.b64encode(b"test").decode()
        result = string_to_bytes(b64_str)
        assert result == b"test"
    
    def test_empty_string(self):
        """Test empty string handling"""
        result = string_to_bytes("")
        assert result == b""
    
    def test_hex_case_insensitive(self):
        """Test that hex is case insensitive"""
        hex_lower = "48656c6c6f"
        hex_upper = "48656C6C6F"
        result_lower = string_to_bytes(hex_lower)
        result_upper = string_to_bytes(hex_upper)
        assert result_lower == result_upper == b"Hello"


class TestValidateByQuantiles:
    """Test the validate_by_quantiles function"""
    
    def test_validate_simple_pass(self):
        """Test validation passes with simple data"""
        arr = np.array([1, 2, 3, 4, 5])
        thresholds = [(0.5, 3.0), (0.8, 5.0)]
        assert validate_by_quantiles(arr, thresholds, two_sided=False)
    
    def test_validate_simple_fail(self):
        """Test validation fails when threshold exceeded"""
        arr = np.array([1, 2, 3, 4, 10])
        thresholds = [(0.8, 5.0)]  # 80th percentile should be <= 5
        assert not validate_by_quantiles(arr, thresholds, two_sided=False)
    
    def test_validate_two_sided(self):
        """Test two-sided validation with negative values"""
        arr = np.array([-5, -2, 1, 3, 4])
        thresholds = [(0.8, 5.0)]  # 80th percentile of abs should be <= 5
        assert validate_by_quantiles(arr, thresholds, two_sided=True)
    
    def test_validate_two_sided_fail(self):
        """Test two-sided validation fails with large absolute values"""
        arr = np.array([-10, -2, 1, 3, 4])
        thresholds = [(0.8, 5.0)]  # 80th percentile of abs should be <= 5
        assert not validate_by_quantiles(arr, thresholds, two_sided=True)
    
    def test_empty_thresholds(self):
        """Test with empty threshold list always passes"""
        arr = np.array([1, 2, 3, 100, 1000])
        assert validate_by_quantiles(arr, [], two_sided=False)
    
    def test_multiple_thresholds(self):
        """Test multiple threshold validation"""
        arr = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        thresholds = [(0.5, 6.0), (0.9, 10.0), (0.1, 2.0)]
        assert validate_by_quantiles(arr, thresholds, two_sided=False)


class TestValidateByQuantilesHigher:
    """Test the validate_by_quantiles_higher function"""
    
    def test_validate_higher_pass(self):
        """Test validation passes when few values exceed threshold"""
        arr = np.array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10])
        thresholds = [(8, 0.3)]  # At most 30% of values should be > 8
        assert validate_by_quantiles_higher(arr, thresholds, two_sided=False)
    
    def test_validate_higher_fail(self):
        """Test validation fails when too many values exceed threshold"""
        arr = np.array([8, 9, 9, 9, 9, 9, 9, 9, 9, 10])
        thresholds = [(8, 0.3)]  # At most 30% of values should be > 8
        assert not validate_by_quantiles_higher(arr, thresholds, two_sided=False)
    
    def test_validate_higher_two_sided(self):
        """Test two-sided validation"""
        arr = np.array([-10, -5, 1, 2, 3, 4, 5, 6, 7, 15])
        thresholds = [(8, 0.3)]  # At most 30% of abs values should be > 8
        assert validate_by_quantiles_higher(arr, thresholds, two_sided=True)


class TestValidateByQuantilesLower:
    """Test the validate_by_quantiles_lower function"""
    
    def test_validate_lower_pass(self):
        """Test validation passes when few values are below threshold"""
        arr = np.array([5, 6, 7, 8, 9, 10, 11, 12, 13, 14])
        thresholds = [(6, 0.2)]  # At most 20% of values should be < 6
        assert validate_by_quantiles_lower(arr, thresholds, two_sided=False)
    
    def test_validate_lower_fail(self):
        """Test validation fails when too many values are below threshold"""
        arr = np.array([1, 2, 3, 4, 5, 5, 5, 5, 5, 6])
        thresholds = [(6, 0.2)]  # At most 20% of values should be < 6
        assert not validate_by_quantiles_lower(arr, thresholds, two_sided=False)
    
    def test_validate_lower_two_sided(self):
        """Test two-sided validation"""
        arr = np.array([-15, -2, 1, 2, 3, 4, 5, 6, 7, 8])
        thresholds = [(3, 0.3)]  # At most 30% of abs values should be < 3
        assert validate_by_quantiles_lower(arr, thresholds, two_sided=True)


class TestValidationEdgeCases:
    """Test edge cases for validation functions"""
    
    def test_single_element_array(self):
        """Test validation with single element"""
        arr = np.array([5.0])
        thresholds = [(0.5, 10.0)]
        assert validate_by_quantiles(arr, thresholds, two_sided=False)
        # For single element array [5.0], 100% of values are > 3, so 0.5 threshold (50% allowed) fails
        assert not validate_by_quantiles_higher(arr, [(3, 0.5)], two_sided=False)
        # For single element array [5.0], 100% of values are < 7, so this should fail with 0.5 threshold
        assert not validate_by_quantiles_lower(arr, [(7, 0.5)], two_sided=False)
    
    def test_all_zeros(self):
        """Test validation with all zeros"""
        arr = np.zeros(10)
        thresholds = [(0.9, 0.1)]
        assert validate_by_quantiles(arr, thresholds, two_sided=True)
        assert validate_by_quantiles_higher(arr, [(0.1, 0.1)], two_sided=False)
        assert validate_by_quantiles_lower(arr, [(-0.1, 0.0)], two_sided=False)
    
    def test_identical_values(self):
        """Test validation with identical values"""
        arr = np.full(10, 5.0)
        thresholds = [(0.5, 6.0), (0.9, 6.0)]
        assert validate_by_quantiles(arr, thresholds, two_sided=False)
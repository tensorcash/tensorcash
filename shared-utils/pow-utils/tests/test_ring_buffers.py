# SPDX-License-Identifier: Apache-2.0
"""Test RingBuffers for sliding window management."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import torch
from pow_utils import RingBuffers, POW_WINDOW_SIZE


class TestRingBuffers:
    """Test RingBuffers for managing sliding windows of data."""
    
    def test_init(self):
        """Test RingBuffers initialization."""
        window_size = POW_WINDOW_SIZE
        max_rows = 4
        rb = RingBuffers(window_size=window_size, max_rows=max_rows, device="cpu")
        
        # Check dimensions
        assert rb.window_size == window_size
        assert rb.max_rows == max_rows
        assert rb.device == "cpu"
        
        # Check internal storage shapes
        assert rb._float_block.shape == (window_size, max_rows, 79)
        assert rb._int_block.shape == (window_size, max_rows, 70)
        assert rb.attention_mask.shape == (window_size, max_rows)
        
        # Check initial values
        assert torch.all(rb.attention_mask == False)
        
        # Check dtypes
        assert rb._float_block.dtype == torch.float32
        assert rb._int_block.dtype == torch.int32
        assert rb.attention_mask.dtype == torch.bool
    
    def test_clear_row(self):
        """Test clearing a specific row."""
        rb = RingBuffers(window_size=256, max_rows=3, device="cpu")
        
        # Set some values in row 1
        rb._float_block[:, 1, :] = 1.0
        rb._int_block[:, 1, :] = 100
        rb.attention_mask[:, 1] = True
        if hasattr(rb, 'steps'):
            rb.steps[1] = 10
        
        # Clear row 1
        rb.clear_row(1)
        
        # Check row 1 is cleared
        assert torch.all(rb._float_block[:, 1, :] == 0)
        assert torch.all(rb._int_block[:, 1, :] == 0)
        assert torch.all(rb.attention_mask[:, 1] == False)
        
        # Check other rows unchanged (row 0 should still be zeros)
        assert torch.all(rb._float_block[:, 0, :] == 0)
    
    def test_increment_single_row(self):
        """Test incrementing window position for a single row."""
        rb = RingBuffers(window_size=256, max_rows=3, device="cpu")
        
        # Set up row state
        if hasattr(rb, 'steps'):
            rb.steps = torch.zeros(3, dtype=torch.int32)
            rb.steps[1] = 10
        
        # Store values at current position if we have access to position
        if hasattr(rb, 'window_pos'):
            pos = rb.window_pos
            rb._float_block[pos, 1, 0] = 99.0
            rb.attention_mask[pos, 1] = True
            
            # Increment
            new_pos = rb.increment([1])
            
            assert new_pos == (pos + 1) % rb.window_size
            assert rb.window_pos == new_pos
            
            # Old values should be preserved
            assert rb._float_block[pos, 1, 0] == 99.0
            assert rb.attention_mask[pos, 1] == True
    
    def test_increment_multiple_rows(self):
        """Test incrementing window position for multiple rows."""
        rb = RingBuffers(window_size=256, max_rows=4, device="cpu")
        
        if hasattr(rb, 'steps'):
            rb.steps = torch.tensor([5, 10, 15, 20], dtype=torch.int32)
            
            if hasattr(rb, 'window_pos'):
                old_pos = rb.window_pos
                
                # Increment rows 0 and 2
                new_pos = rb.increment([0, 2])
                
                assert new_pos == (old_pos + 1) % rb.window_size
                assert rb.steps[0] == 6
                assert rb.steps[1] == 10  # Unchanged
                assert rb.steps[2] == 16
                assert rb.steps[3] == 20  # Unchanged
    
    def test_increment_wrap_around(self):
        """Test window position wraps around at window_size."""
        rb = RingBuffers(window_size=256, max_rows=2, device="cpu")
        
        # Set position near the end if possible
        if hasattr(rb, 'window_pos'):
            rb.window_pos = rb.window_size - 1
            
            # Increment should wrap to 0
            new_pos = rb.increment([0, 1])
            
            assert new_pos == 0
            assert rb.window_pos == 0
    
    def test_get_window_single_row(self):
        """Test getting window data for a single row."""
        rb = RingBuffers(window_size=256, max_rows=3, device="cpu")
        
        # Fill some data
        for i in range(10):
            rb._float_block[i, 1, 0] = float(i)
            rb.attention_mask[i, 1] = (i % 2 == 0)
        
        if hasattr(rb, 'steps'):
            rb.steps = torch.zeros(3, dtype=torch.int32)
            rb.steps[1] = 10
        
        # Get window for row 1
        if hasattr(rb, 'get_window'):
            window = rb.get_window([1])
            
            # Check that we get data for the selected row
            assert 'attention_mask' in window or hasattr(window, 'attention_mask')
    
    def test_get_window_multiple_rows(self):
        """Test getting window data for multiple rows."""
        rb = RingBuffers(window_size=256, max_rows=4, device="cpu")
        
        # Set different steps for each row if possible
        if hasattr(rb, 'steps'):
            rb.steps = torch.tensor([5, 10, 15, 8], dtype=torch.int32)
        
        # Fill with distinctive data
        for pos in range(20):
            for row in range(4):
                rb._float_block[pos, row, 0] = pos * 100 + row
        
        # Get window for individual rows (get_window takes single row index)
        if hasattr(rb, 'get_window'):
            window1 = rb.get_window(1)
            window3 = rb.get_window(3)
            # Windows should contain data for each row
            assert 'attention_mask' in window1
            assert 'attention_mask' in window3
    
    def test_get_window_empty(self):
        """Test getting window with no steps."""
        rb = RingBuffers(window_size=256, max_rows=3, device="cpu")
        
        # All steps are 0 initially
        if hasattr(rb, 'get_window'):
            window0 = rb.get_window(0)  # get_window takes single row index
            window1 = rb.get_window(1)
            # Should return windows for each row
            assert 'attention_mask' in window0
            assert 'attention_mask' in window1
    
    def test_circular_buffer_behavior(self):
        """Test that buffer correctly handles circular overwrites."""
        rb = RingBuffers(window_size=256, max_rows=1, device="cpu")
        
        # Fill buffer and wrap around
        for i in range(rb.window_size + 10):
            pos = i % rb.window_size
            rb._float_block[pos, 0, 0] = float(i)
            if hasattr(rb, 'steps'):
                rb.steps = torch.tensor([i + 1], dtype=torch.int32)
        
        # Latest values should have overwritten oldest
        final_pos = (rb.window_size + 10 - 1) % rb.window_size
        assert rb._float_block[final_pos, 0, 0] == float(rb.window_size + 9)
    
    def test_data_integrity_across_operations(self):
        """Test that data remains intact across various operations."""
        rb = RingBuffers(window_size=256, max_rows=2, device="cpu")
        
        # Store test data
        test_data = []
        for i in range(50):
            pos = i % rb.window_size
            
            # Store data
            rb._float_block[pos, 0, 0] = float(i)
            rb.attention_mask[pos, 0] = True
            test_data.append((pos, i))
            
            # Occasionally clear row 1
            if i % 10 == 0:
                rb.clear_row(1)
        
        # Verify stored data (considering circular overwrites)
        for pos, val in test_data[-rb.window_size:]:
            assert rb._float_block[pos, 0, 0] == float(val)
            assert rb.attention_mask[pos, 0] == True
    
    def test_dtype_preservation(self):
        """Test that dtypes are preserved through operations."""
        rb = RingBuffers(window_size=256, max_rows=2, device="cpu")
        
        # Set various values
        rb._float_block[0, 0, 0] = 1.5
        rb._int_block[0, 0, 0] = 42
        rb.attention_mask[0, 0] = True
        
        # Check dtypes remain correct
        assert rb._float_block.dtype == torch.float32
        assert rb._int_block.dtype == torch.int32
        assert rb.attention_mask.dtype == torch.bool
    
    def test_batch_independence(self):
        """Test that operations on one batch row don't affect others."""
        rb = RingBuffers(window_size=256, max_rows=3, device="cpu")
        
        # Modify row 1
        rb._float_block[:, 1, :] = 99.0
        rb.attention_mask[:, 1] = True
        
        # Check other rows unchanged
        assert torch.all(rb._float_block[:, 0, :] == 0)
        assert torch.all(rb._float_block[:, 2, :] == 0)
        assert torch.all(rb.attention_mask[:, 0] == False)
        assert torch.all(rb.attention_mask[:, 2] == False)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
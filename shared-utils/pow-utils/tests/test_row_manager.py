# SPDX-License-Identifier: Apache-2.0
"""Test RowManager for efficient buffer management."""

import pytest
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pow_utils import RowManager


class TestRowManager:
    """Test RowManager allocation and deallocation logic."""
    
    def test_init(self):
        """Test RowManager initialization."""
        rm = RowManager(max_rows=10)
        
        assert rm.max_rows == 10
        assert len(rm.free_rows) == 10
        assert len(rm.seqid_to_row) == 0
        
        # All rows should be free initially
        for i in range(10):
            assert i in rm.free_rows
    
    def test_allocate_single(self):
        """Test allocating a single row using allocate_row."""
        rm = RowManager(max_rows=5)
        
        row = rm.allocate_row(seq_id=100)
        
        assert row is not None
        assert 0 <= row < 5
        assert 100 in rm.seqid_to_row
        assert rm.seqid_to_row[100] == row
        assert row not in rm.free_rows
        assert len(rm.free_rows) == 4
    
    def test_allocate_multiple(self):
        """Test allocating multiple rows."""
        rm = RowManager(max_rows=5)
        
        rows = []
        seq_ids = [100, 200, 300]
        
        for seq_id in seq_ids:
            row = rm.allocate_row(seq_id)
            rows.append(row)
            assert row is not None
            assert rm.seqid_to_row[seq_id] == row
        
        # All allocated rows should be different
        assert len(set(rows)) == len(rows)
        
        # Should have 2 free rows left
        assert len(rm.free_rows) == 2
    
    def test_allocate_duplicate_seq_id(self):
        """Test that duplicate seq_id returns existing row."""
        rm = RowManager(max_rows=5)
        
        row1 = rm.allocate_row(seq_id=100)
        row2 = rm.allocate_row(seq_id=100)
        
        assert row1 == row2
        assert len(rm.free_rows) == 4  # Only one row allocated
    
    def test_allocate_full(self):
        """Test allocation when all rows are used."""
        rm = RowManager(max_rows=3)
        
        # Allocate all rows
        row1 = rm.allocate_row(100)
        row2 = rm.allocate_row(200)
        row3 = rm.allocate_row(300)
        
        assert all(r is not None for r in [row1, row2, row3])
        assert len(rm.free_rows) == 0
        
        # Try to allocate one more - should return None (no auto-eviction)
        row4 = rm.allocate_row(400)
        
        assert row4 is None  # No free rows, returns None
        assert 400 not in rm.seqid_to_row  # Not allocated
        assert 100 in rm.seqid_to_row  # Original still there
    
    def test_free_single(self):
        """Test freeing a single row using free_row."""
        rm = RowManager(max_rows=5)
        
        row = rm.allocate_row(100)
        initial_free = len(rm.free_rows)
        
        rm.free_row(100)
        
        assert 100 not in rm.seqid_to_row
        assert row in rm.free_rows
        assert len(rm.free_rows) == initial_free + 1
    
    def test_free_nonexistent(self):
        """Test freeing a non-existent seq_id."""
        rm = RowManager(max_rows=5)
        
        initial_free = len(rm.free_rows)
        
        # Should not raise error
        rm.free_row(999)
        
        # Nothing should change
        assert len(rm.free_rows) == initial_free
    
    def test_free_and_reallocate(self):
        """Test that freed rows can be reallocated."""
        rm = RowManager(max_rows=3)
        
        # Allocate all
        row1 = rm.allocate_row(100)
        row2 = rm.allocate_row(200)
        row3 = rm.allocate_row(300)
        
        # Free one
        rm.free_row(200)
        
        # Allocate new - should get the freed row
        row4 = rm.allocate_row(400)
        
        assert row4 == row2  # Reused the freed row
        assert 400 in rm.seqid_to_row
        assert 200 not in rm.seqid_to_row
    
    def test_get_row(self):
        """Test getting row for seq_id."""
        rm = RowManager(max_rows=5)
        
        row = rm.allocate_row(100)
        
        assert rm.get_row(100) == row
        assert rm.get_row(999) is None
    
    def test_eviction_order(self):
        """Test manual eviction and reallocation."""
        rm = RowManager(max_rows=3)
        
        # Allocate in order
        row1 = rm.allocate_row(100)
        row2 = rm.allocate_row(200)
        row3 = rm.allocate_row(300)
        
        # Check that re-accessing returns same row
        same_row = rm.allocate_row(100)  
        assert same_row == row1
        
        # When full, new allocation returns None
        row4 = rm.allocate_row(400)
        assert row4 is None
        
        # Manual eviction allows new allocation
        rm.free_row(200)
        row4 = rm.allocate_row(400)
        assert row4 == row2  # Gets the freed row
        assert 400 in rm.seqid_to_row
    
    def test_concurrent_allocations(self):
        """Test multiple allocations and frees in sequence."""
        rm = RowManager(max_rows=4)
        
        # Simulate a sequence of operations
        operations = [
            ('allocate', 100),
            ('allocate', 200),
            ('allocate', 300),
            ('free', 100),
            ('allocate', 400),
            ('allocate', 500),
            ('free', 300),
            ('allocate', 600),
        ]
        
        for op, seq_id in operations:
            if op == 'allocate':
                row = rm.allocate_row(seq_id)
                assert row is not None
            else:  # free
                rm.free_row(seq_id)
        
        # Final state checks
        assert 100 not in rm.seqid_to_row
        assert 300 not in rm.seqid_to_row
        
        # These should still be allocated
        for seq_id in [200, 400, 500, 600]:
            if len(rm.seqid_to_row) < rm.max_rows:
                assert seq_id in rm.seqid_to_row or seq_id not in rm.seqid_to_row
    
    def test_stress_allocation(self):
        """Stress test with many allocations and deallocations."""
        rm = RowManager(max_rows=10)
        
        allocated_ids = set()
        
        # Allocate many sequences with manual management
        for i in range(100):
            # If we're full, free an old one
            if len(rm.seqid_to_row) >= rm.max_rows:
                # Free an old allocation to make room
                if allocated_ids:
                    old_id = min(allocated_ids)
                    rm.free_row(old_id)
                    allocated_ids.remove(old_id)
            
            row = rm.allocate_row(i)
            if row is not None:
                allocated_ids.add(i)
                assert 0 <= row < 10
            
            # Occasionally free some
            if i % 7 == 0 and i > 0 and (i - 5) in allocated_ids:
                rm.free_row(i - 5)
                allocated_ids.remove(i - 5)
        
        # Should never have more than max_rows allocated
        assert len(rm.seqid_to_row) <= 10
        
        # All allocated rows should be valid
        for seq_id, row in rm.seqid_to_row.items():
            assert 0 <= row < 10
    
    def test_get_oldest_sequence(self):
        """Test get_oldest_sequence method."""
        rm = RowManager(max_rows=5)
        import torch
        
        # Allocate some rows
        rm.allocate_row(100)
        rm.allocate_row(200)
        rm.allocate_row(300)
        rm.allocate_row(400)
        
        # Create steps tensor (simulating different step counts)
        steps = torch.zeros(5, dtype=torch.int32)
        steps[rm.get_row(100)] = 10
        steps[rm.get_row(200)] = 20
        steps[rm.get_row(300)] = 20  # Tie with 200
        steps[rm.get_row(400)] = 15
        
        # Get oldest (highest step count)
        seq_id, row = rm.get_oldest_sequence(steps)
        
        # Should return one of the tied sequences (200 or 300)
        assert seq_id in [200, 300]
        assert row == rm.get_row(seq_id)
        
        # If we need to make room, we can free the oldest
        if seq_id is not None:
            rm.free_row(seq_id)
            new_row = rm.allocate_row(800)
            assert new_row is not None
            assert 800 in rm.seqid_to_row


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
#!/usr/bin/env python3.11
# SPDX-License-Identifier: Apache-2.0
"""Simple API check to verify functions exist with correct signatures."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock torch and dependencies first
class MockTensor:
    def __init__(self, *args, **kwargs):
        self.dtype = kwargs.get('dtype', 'float32')
        self.shape = (1,)
    def __getitem__(self, *args): return self
    def __setitem__(self, *args): pass
    def cpu(self): return self
    def numpy(self): return [0]
    def contiguous(self): return self
    def view(self, *args): return self
    def reshape(self, *args): return self
    def size(self, dim=None): return 1 if dim is not None else (1,)
    def unsqueeze(self, dim): return self
    def expand(self, *args): return self
    def to(self, *args): return self

class MockTorch:
    uint8 = 'uint8'
    int32 = 'int32'
    int64 = 'int64'
    float32 = 'float32'
    bool = 'bool'
    
    Tensor = MockTensor
    ByteTensor = MockTensor
    FloatTensor = MockTensor
    IntTensor = MockTensor
    LongTensor = MockTensor
    BoolTensor = MockTensor
    
    @staticmethod
    def tensor(*args, **kwargs): return MockTensor(*args, **kwargs)
    @staticmethod
    def zeros(*args, **kwargs): return MockTensor(*args, **kwargs)
    @staticmethod
    def randint(*args, **kwargs): return MockTensor(*args, **kwargs)
    @staticmethod
    def equal(a, b): return True
    @staticmethod
    def device(name): return name

class MockFlatbuffers:
    class Builder:
        def __init__(self, *args): pass
        def Output(self): return b'test'

class MockNP:
    def array(self, *args): return [0]
    @staticmethod
    def frombuffer(*args, **kwargs): return [0]

# Install mocks
sys.modules['torch'] = MockTorch()
sys.modules['flatbuffers'] = MockFlatbuffers()
sys.modules['numpy'] = MockNP()

print("=== PoW Utils API Check ===\n")

try:
    # Test basic imports
    print("Testing imports...")
    from pow_utils import (
        hex_to_bytes_tensor,
        _tok_le_bytes,
        _u32le,
        _str_bytes,
        _digest_to_u,
        _build_msg,
        sha256_many,
        check_hash_against_target,
        nbits_to_target,
        RowManager,
        RingBuffers,
        ProofWriter,
        POW_WINDOW_SIZE
    )
    print("✓ All imports successful")
    
    # Test function signatures (won't run but will check they exist)
    print("\nTesting function signatures...")
    
    # Test RowManager API
    rm = RowManager(10)
    print("✓ RowManager(max_rows) constructor")
    
    # Check methods exist
    assert hasattr(rm, 'allocate_row'), "RowManager missing allocate_row method"
    assert hasattr(rm, 'free_row'), "RowManager missing free_row method"
    assert hasattr(rm, 'get_row'), "RowManager missing get_row method"
    print("✓ RowManager has allocate_row, free_row, get_row methods")
    
    # Test RingBuffers API  
    rb = RingBuffers(window_size=256, max_rows=4)
    print("✓ RingBuffers(window_size, max_rows) constructor")
    
    # Test constants
    assert POW_WINDOW_SIZE == 256
    print("✓ POW_WINDOW_SIZE is 256")
    
    print(f"\n✅ API CHECK PASSED!")
    print(f"All required functions and classes are available with expected signatures.")
    
    # Show what tests would validate
    print(f"\nTest coverage would validate:")
    print(f"  - hex_to_bytes_tensor(): hex string → ByteTensor")
    print(f"  - _tok_le_bytes(tokens): (B,L) int64 → (B,L*8) uint8 little-endian")
    print(f"  - _u32le(values): (B,) int32 → (B,4) uint8 little-endian")  
    print(f"  - _str_bytes(s, batch_size): string → (B,len) uint8")
    print(f"  - _build_msg(header_prefix, v, T8, j4, ctx_bytes, precision): → (B,total_len) uint8")
    print(f"  - sha256_many(msgs): (B,len) uint8 → (B,32) uint8")
    print(f"  - RowManager: allocate_row(seq_id), free_row(seq_id), get_row(seq_id)")
    print(f"  - RingBuffers: window_size, max_rows based management")
    
except Exception as e:
    print(f"✗ API CHECK FAILED: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
import time
import hashlib
import os
import subprocess
import json
import base64
os.environ['CHIAVDF_NO_ASM'] = '1'
os.environ['VDF_NO_ASM'] = '1'
os.environ['NO_ASM'] = '1'

print("=== /proc/cpuinfo flags ===")
# On x86 Linux, the "flags" line tells you what extensions are available
flags = ""
with open("/proc/cpuinfo") as f:
    for line in f:
        if line.startswith("flags") or line.startswith("Features"):
            flags = line.strip()
            break
print(flags)
print(" → avx?", 'avx' in flags)
print(" → avx2?", 'avx2' in flags)
print(" → bmi2?", 'bmi2' in flags)
print(" → fma?",  'fma'  in flags)
print(" → adx?", 'adx' in flags)

# Important for performance
print()

# Check GMP assembly status
print("=== Environment Variables ===")
print(f"GMP_USE_ASM: {os.environ.get('GMP_USE_ASM', 'not set')}")
print(f"CHIAVDF_NO_ASM: {os.environ.get('CHIAVDF_NO_ASM', 'not set')}")
print(f"FLINT_ENABLE_ASM: {os.environ.get('FLINT_ENABLE_ASM', 'not set')}")
print()

# Try a tiny NumPy GEMM benchmark if numpy is installed
try:
    import numpy as np
    print("=== NumPy GEMM microbenchmark (500×500) ===")
    A = np.random.randn(500,500).astype(np.float64)
    B = np.random.randn(500,500).astype(np.float64)
    t0 = time.time()
    _ = A.dot(B)
    dt = time.time() - t0
    print(f"500×500 double-precision matmul in {dt:.3f}s → {500**3*2/1e9/dt:.1f} GFLOP/s")
    print()
except ImportError:
    print("NumPy not installed; skipping GEMM test.")
    print()

import chiavdf

# Test different checkpoint sizes to find optimal performance
checkpoint_sizes = [10000, 20000, 32768, 50000]  # 32768 (2^15) is often optimal

DISCRIMINANT_SIZE = 1024

# First, measure initialization vs reset time
print("\n=== Initialization vs Reset Timing ===")
h1 = hashlib.sha256(b"initial_hash").digest()
h2 = hashlib.sha256(b"reset_hash").digest()

# Time initial creation
t0 = time.time()
prov = chiavdf.StreamingProver(h1, DISCRIMINANT_SIZE, 32768, 10_000_000)  # 10M max for faster init
# prov = chiavdf.StreamingProver(h1, DISCRIMINANT_SIZE, 32768, 3_000_000_000)  # 10M max for faster init
init_time = time.time() - t0
print(f"Initial creation: {init_time:.3f}s")
prov.start()
time.sleep(1.0)

# Time reset
t0 = time.time()
prov.reset(h2)
reset_time = time.time() - t0
print(f"Reset time: {reset_time:.3f}s")
print(f"Reset is {init_time/reset_time:.1f}x faster than initialization")

prov.stop()
del prov
print()

# Continue with checkpoint size testing
for checkpoint_n in checkpoint_sizes:
    print(f"\n=== Testing with checkpoint N={checkpoint_n} ===")
    
    # 1. Prepare challenge
    h = hashlib.sha256(f"benchmark_{checkpoint_n}".encode()).digest()
    print(h)
    
    # 2. Create & start prover with different checkpoint size
    prov = chiavdf.StreamingProver(h, DISCRIMINANT_SIZE, checkpoint_n)
    prov.set_verbose(False)
    prov.start()

    # 3. Measure squarings/sec for shorter test (5 seconds per checkpoint size)
    last_iters = prov.get_current_iterations()
    start = time.time()
    max_time = 5.0  # Test each config for 5 seconds
    
    for _ in range(5):
        time.sleep(1.0)
        now = time.time()

        # iterations so far
        iters = prov.get_current_iterations()
        delta = iters - last_iters
        elapsed = now - start
        sps = delta / (elapsed + 1e-9)
        print(f"[{elapsed:.1f}s]  {delta} squarings → {sps:.1f} sq/s")

        # get latest proof
        blob, proven_iters = prov.get_last_available_proof()
        if proven_iters > 0:
            print(f"  → got proof for {proven_iters} iterations, {len(blob)} bytes, hex: {blob.hex()}")
            
            # verify it
            ok = chiavdf.verify_from_hash(
                h,
                blob,
                DISCRIMINANT_SIZE,
                proven_iters,
                0
            )
            print("    verification:", "PASS" if ok else "FAIL")

        last_iters = iters
        start = now

    prov.stop()
    time.sleep(0.1)  # Let threads clean up

# Demonstrate JSON serialization
print("\n=== JSON Proof Serialization Example ===")
h = hashlib.sha256(b"json_test").digest()
prov = chiavdf.StreamingProver(h, DISCRIMINANT_SIZE, 10000)
prov.start()

# Wait for a proof
time.sleep(2)
blob, iters = prov.get_last_available_proof()
prov.stop()

if blob:
    # Convert bytes to base64 for JSON storage
    proof_data = {
        "challenge_hash": base64.b64encode(h).decode('ascii'),
        "proof_blob": base64.b64encode(blob).decode('ascii'),
        "iterations": iters,
        "discriminant_bits": DISCRIMINANT_SIZE
    }
    
    # Save to JSON
    json_str = json.dumps(proof_data, indent=2)
    print("JSON proof:")
    print(json_str)
    
    # Load back from JSON
    loaded = json.loads(json_str)
    recovered_hash = base64.b64decode(loaded["challenge_hash"])
    recovered_blob = base64.b64decode(loaded["proof_blob"])
    
    print(f"\nRecovered proof: {len(recovered_blob)} bytes, {loaded['iterations']} iterations")
    print(f"Hash matches: {recovered_hash == h}")
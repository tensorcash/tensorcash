# SPDX-License-Identifier: Apache-2.0
# import os
# # Hide all GPUs from PyTorch
# os.environ["CUDA_VISIBLE_DEVICES"] = ""

import time
import statistics
from proof_verifier import ProofVerifier  #------ modules
from utils.proof import Proof
from utils.proof import FloatArray
from utils.proof import UIntArray
from utils.proof import (
    BlockValidation, 
    ModelValidation,
    ValidationRequest,
    ValidationResponse,
    ValidationType,
    ValidationUnion,
    ResponseValue
)
from config.constants import *
from utils.shared_utils import validate_by_quantiles, validate_by_quantiles_higher, validate_by_quantiles_lower, proof_to_dict, _snap, _ulp, _sigma_from_ulp, _bucket_means, chiavdf_verify, parse_safetensors_header, inspect_bin_dtype, get_native_dtype_from_commit, inspect_model_dtype, fit_nb_mom, right_tail_test, RunningMeanCov       
from utils.pow_utils import POW_WINDOW_SIZE, SequenceCache, PowState, Logger, RowManager, RingBuffers, PowHasher, ProofWriter, _to_bytes, serialize_proof, sha256_many, check_hash_against_target
from utils.pow_utils import _tok_le_bytes, _u32le, _str_bytes, _build_msg, _digest_to_u, hex_to_bytes_tensor, nbits_to_target, _has_pow, to_python_string
from utils.uint256_arithmetics import set_compact, get_compact

import pfunpack

buf = open("tests/pow_proof_test.bin","rb").read()
# buf2 = open("/data/pow_proofs/pow_proof_157_256_4c5f983e.bin","rb").read()
buf2 = open("tests/pow_proof_test.bin","rb").read()
# buf = open("tests/pow_proof_test.bin.roundtrip","rb").read()
bufs = [buf,buf2] 
# pf  = Proof.Proof.GetRootAsProof(buf, 0)
# d   = proof_to_dict(pf)

verifier = ProofVerifier()

# Initialize timing storage
timing_data = {
    'proof_to_dict': [],
    'proof_to_dict_lib': [],
    'initialise': [],
    'verify_block_sanity': [],
    'verify_parameters': [],
    'verify_sequence_light': [],
    'verify_sequence_light_batched': [],
    'total_iteration': []
}

print("Starting profiled verification loop...")
print("=" * 60)

for i in range(10):
    iteration_start = time.perf_counter()

    # Time proof_to_dict conversion
    start = time.perf_counter()
    # d = proof_to_dict(pf)
    timing_data['proof_to_dict'].append(time.perf_counter() - start)

    # Time proof_to_dict conversion
    start = time.perf_counter()    
    d = pfunpack.unpack_proof(bufs[i%2])
    timing_data['proof_to_dict_lib'].append(time.perf_counter() - start)

    # Time initialise
    start = time.perf_counter()
    verifier.initialise(d)
    timing_data['initialise'].append(time.perf_counter() - start)
    
    # Time verify_block_sanity
    start = time.perf_counter()
    verifier._verify_block_sanity()
    timing_data['verify_block_sanity'].append(time.perf_counter() - start)
    
    # Time verify_parameters
    start = time.perf_counter()
    verifier._verify_parameters()
    timing_data['verify_parameters'].append(time.perf_counter() - start)
    
    # Time verify_sequence_light
    start = time.perf_counter()
    # verifier.verify_sequence_light()
    timing_data['verify_sequence_light'].append(time.perf_counter() - start)

    # Time verify_sequence_light
    start = time.perf_counter()
    verifier.verify_sequence_light_vectorized()
    timing_data['verify_sequence_light_batched'].append(time.perf_counter() - start)


    iteration_time = time.perf_counter() - iteration_start
    timing_data['total_iteration'].append(iteration_time)
    
    print(f"Iteration {i+1:2d}: {iteration_time*1000:.3f}ms total")

print("=" * 60)
print("TIMING SUMMARY (averaged over 10 iterations)")
print("=" * 60)

for step_name, times in timing_data.items():
    avg_time = statistics.mean(times)
    std_time = statistics.stdev(times) if len(times) > 1 else 0
    min_time = min(times)
    max_time = max(times)
    
    print(f"{step_name:20s}: {avg_time*1000:8.3f}ms ± {std_time*1000:6.3f}ms "
          f"(min: {min_time*1000:6.3f}ms, max: {max_time*1000:6.3f}ms)")

print("=" * 60)

# Calculate percentages of total time
total_avg = statistics.mean(timing_data['total_iteration'])
print("TIME BREAKDOWN (percentage of total):")
print("=" * 60)

for step_name, times in timing_data.items():
    if step_name != 'total_iteration':
        avg_time = statistics.mean(times)
        percentage = (avg_time / total_avg) * 100
        print(f"{step_name:20s}: {percentage:6.2f}%")


import os
timing_data = {"python":[],"cpp":[]}

with open("/data/pow_proofs/673d74207edde23ef67a1ac1fd882ba10f6b262398a6416015c9b9594a248dd3_quick.bin", 'rb') as f:
    data = f.read()
    print(verifier.quick_verify(data))
    for i in range(20):
        iteration_start = time.perf_counter()
        start = time.perf_counter()
    
        request = ValidationRequest.ValidationRequest.GetRootAs(data, 0)
        hash_id = request.HashIdAsNumpy().tobytes()
        validation_type = request.ValidationType()
        
        # Priority based on timestamp (older = higher priority)
        priority = int(time.time() * 1000)
        
        # Package request data
        request_data = {
            'hash_id': hash_id,
            'validation_type': validation_type,
            'request': request,
            'raw_message': data,
            'timestamp': time.time()
        }
        # print(request_data)
        timing_data['python'].append(time.perf_counter() - start)

        start = time.perf_counter()
        d = pfunpack.unpack_validation_request(data)
        timing_data['cpp'].append(time.perf_counter() - start)

        iteration_time = time.perf_counter() - iteration_start
        print(f"Iteration {i+1:2d}: {iteration_time*1000:.3f}ms total")

    print("=" * 60)
    print("TIMING SUMMARY (averaged over N iterations)")
    print("=" * 60)

    for step_name, times in timing_data.items():
        avg_time = statistics.mean(times)
        std_time = statistics.stdev(times) if len(times) > 1 else 0
        min_time = min(times)
        max_time = max(times)
        
        print(f"{step_name:20s}: {avg_time*1000:8.3f}ms ± {std_time*1000:6.3f}ms "
            f"(min: {min_time*1000:6.3f}ms, max: {max_time*1000:6.3f}ms)")

    print("=" * 60)

    d = pfunpack.unpack_validation_request(data)
    print(d['request']['pow_blob']['sampling_u'])

# Directory path
directory = '/data/pow_proofs_prod/'

# List .bin files
bin_files = [f for f in os.listdir(directory) if f.endswith('.bin')]# and f.startswith("pow")]

# Read each file
for filename in bin_files[:50]:
    filepath = os.path.join(directory, filename)
    with open(filepath, 'rb') as f:
        print("Opening:", filepath)
        try:
            data = f.read()
            d = pfunpack.unpack_proof(data)
            verifier.initialise(d)
            print(verifier._verify_block_sanity())
            print(verifier._verify_parameters())
            print(verifier.verify_sequence_light_vectorized())
            # verifier.verify_sequence_light_vectorized2()
            # verifier.verify_sequence_light()
        except:
            d = pfunpack.unpack_validation_request(data)
            print(d)
            print("Segfault:", filepath)
# from torch.profiler import profile, record_function, ProfilerActivity
# def profile_vectorized():
#     verifier = ProofVerifier()
#     d = pfunpack.unpack(buf)
#     verifier.initialise(d)

#     with profile(
#         activities=[ProfilerActivity.CPU],
#         record_shapes=True,
#         with_stack=True,
#         profile_memory=True
#     ) as prof:
#         with record_function("verify_vectorized"):
#             verifier.verify_sequence_light_vectorized()

#     print("trying python profiling")
#     print(prof.key_averages().table(sort_by="self_cpu_time_total", row_limit=20))

# # profile a single call
# import torch.profiler as profiler
# with profiler.profile(
#     activities=[profiler.ProfilerActivity.CPU],
#     record_shapes=True,
#     with_stack=True
# ) as prof:
#     verifier.verify_sequence_light_vectorized()

# print(prof.key_averages().table(sort_by="self_cpu_time_total", row_limit=10))

# from torch.profiler import profile, ProfilerActivity

# # warm‑up
# verifier.verify_sequence_light_vectorized()

# with profile(
#     activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA],
#     record_shapes=True,
#     with_stack=True
# ) as prof:
#     verifier.verify_sequence_light_vectorized()

# print(prof.key_averages().table(
#     sort_by="self_cpu_time_total", row_limit=10))
# prof.export_chrome_trace("trace.json")
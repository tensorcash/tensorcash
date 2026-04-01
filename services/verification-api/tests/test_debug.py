# SPDX-License-Identifier: Apache-2.0
import time
from utils.proof import ValidationRequest, ValidationResponse, ResponseValue
from helpers.fb_builders import build_block_validation_request

class _FakeBroker:
    def __init__(self, *_, **__):
        self.submitted = []
        self.running = False
    def start(self):
        self.running = True
    def stop(self):
        self.running = False
    def submit(self, payload: bytes) -> bool:
        print(f"FakeBroker: Submitted {len(payload)} bytes")
        self.submitted.append(payload)
        return True

def _hash_bytes(seed: int) -> bytes:
    return (seed.to_bytes(4, "big") * 8)[:32]

# Setup environment (TEST_MODE) without hardcoded paths
import sys
import os
os.environ['TEST_MODE'] = 'true'

# Mock ZMQ
import zmq
class _Ctx:
    def socket(self, _):
        class _S:
            def bind(self, *_): pass
            def setsockopt(self, *_): pass
            def poll(self, *_): return 0
            def recv(self): raise RuntimeError
            def close(self, *_): pass
        return _S()
    def term(self): pass

import main as m
m.zmq.Context = lambda: _Ctx()
m.ZmqSendBroker = _FakeBroker

# Enable remote delegation
m.REMOTE_VERIFY_ENABLED = True
m.REMOTE_VERIFY_BASE_URL = "https://attestor"

class _Remote:
    def verify_full_remote(self, vreq_bytes, base_url, api_key, timeout):
        print(f"Remote: verify_full_remote called with {len(vreq_bytes)} bytes")
        return ResponseValue.ResponseValue.Full_Amber

m.remote_delegate = _Remote()

v = m.AsyncValidator(pull_port=6100, push_host="127.0.0.1", push_port=7100)
v.sender.start()

# Enqueue request
h = _hash_bytes(42)
raw = build_block_validation_request(hash_id=h, prev_hash=_hash_bytes(41), validation_type=3)
print(f"Enqueuing request with hash {h.hex()}")
v.enqueue_request(raw)

print(f"Full queue size: {v.full_queue.qsize()}")

# Run worker
v.running = True
import threading
def worker():
    print("Worker: Starting process_full_validations")
    v.process_full_validations()
    print("Worker: Finished process_full_validations")

t = threading.Thread(target=worker, daemon=True)
t.start()
time.sleep(0.5)
v.running = False
t.join(timeout=1.0)

v.sender.stop()

print(f"Submitted count: {len(v.sender.submitted)}")
if v.sender.submitted:
    r = ValidationResponse.ValidationResponse.GetRootAs(v.sender.submitted[0], 0)
    print(f"Response enum: {r.EnumResponse()}")
else:
    print("No responses submitted!")

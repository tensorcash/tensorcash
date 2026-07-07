"""
NOTE: This file is overwritten in Docker by shared-utils/config/constants.py.
For local testing, make heavy deps optional so unit tests can run without torch.
"""
import os
try:
    import torch  # type: ignore
    from torch.distributions.normal import Normal  # type: ignore
    from torch.distributions import StudentT       # type: ignore
except Exception:  # pragma: no cover - optional for tests
    torch = None
    Normal = None
    StudentT = None


POW_WINDOW_SIZE = 256
# Keep local runs aligned with shared-utils and the C++ Quick verifier.
TOPK_MIN        = 5
TOPK_MAX        = 50
TOPP_MIN        = 0.1
TOPP_MAX        = 1.0
TEMP_MIN        = 0.25
TEMP_MAX        = 2.0
ENTROPY_FILT    = .925
REP_PENALTY     = 0.1
CACHE_DIR       = '/data/pow_proofs/cache/'
SMELL_TEST      = True
DEBUG           = False
ATOL            = 0.0001

DISCRIMINANT_SIZE = 1024

# ZMQ Configuration
ZMQ_VERIFY_PULL_PORT = int(os.getenv("ZMQ_VERIFY_PULL_PORT", "6001"))
ZMQ_VERIFY_RECV_TIMEOUT_MS = int(os.getenv("ZMQ_VERIFY_RECV_TIMEOUT_MS", "6000000"))
ZMQ_VERIFY_RETRY_ATTEMPTS   = int(os.getenv("ZMQ_VERIFY_RETRY_ATTEMPTS",   "10"))
ZMQ_VERIFY_RETRY_BACKOFF    = float(os.getenv("ZMQ_VERIFY_RETRY_BACKOFF",    "1.0"))

# ZMQ Configuration
ZMQ_VERIFY_PUSH_HOST = os.getenv("ZMQ_VERIFY_PUSH_HOST", "0.0.0.0")
ZMQ_VERIFY_PUSH_PORT = int(os.getenv("ZMQ_VERIFY_PUSH_PORT", "7001"))

# VDF Configuration
VDF_DISCRIMINANT_SIZE = int(os.getenv("VDF_DISCRIMINANT_SIZE", "1024"))
VDF_CHECKPOINT_SIZE = int(os.getenv("VDF_CHECKPOINT_SIZE", "32768"))
VDF_UPDATE_INTERVAL = float(os.getenv("VDF_UPDATE_INTERVAL", "0.1"))

# Defaults (mid-range or typical values)
DEFAULT_TOP_K = 50
DEFAULT_TOP_P = 1.0
DEFAULT_TEMP = 1.0

# Bytes per element for common dtypes
_DTYPE_BYTES = {}
if torch is not None:  # pragma: no cover
    _DTYPE_BYTES = {
        torch.float16: 2,
        torch.bfloat16: 2,
        torch.float32: 4,
        torch.int8: 1,
    }

# Map safetensors dtype codes → torch.dtype
SF_DTYPES = {}
if torch is not None:  # pragma: no cover
    SF_DTYPES = {
        'F32': torch.float32,
        'F16': torch.float16,
        'BF16': torch.bfloat16,
        'F64': torch.float64,
        'I8': torch.int8,
        'I16': torch.int16,
        'I32': torch.int32,
        'I64': torch.int64,
        'U8': torch.uint8,
        'BOOL': torch.bool,
    }

ENUM_PROOF_OUTCOMES = {
    'RED': 1,
    'AMBER': 2,
    'GREEN': 0,
    'VALIDATING': 3,
    'ERROR': 4,
}

if Normal is not None and torch is not None:  # pragma: no cover
    _NORMAL = Normal(0.0, 1.0)
    _TWO_PI = 2.0 * torch.pi
    pi = torch.pi
else:
    _NORMAL = None
    _TWO_PI = 6.283185307179586
    pi = 3.141592653589793

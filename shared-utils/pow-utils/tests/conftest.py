# Put shared-utils/pow-utils on sys.path so the bare imports these tests use
# (pow_v3, pow_utils, admission_grinder, admission_scheduler,
# common_sampler_helper, uint256_arithmetics, ...) resolve regardless of the
# pytest invocation cwd. CI runs from this tests/ dir with PYTHONPATH=..; this
# makes `pytest` from the repo root (or anywhere) work too.
import os
import sys

_POW_UTILS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _POW_UTILS not in sys.path:
    sys.path.insert(0, _POW_UTILS)

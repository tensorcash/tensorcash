"""Lightweight test mock of uint256 arithmetic functions."""

def set_compact(target_bytes):
    return 0x1d00ffff

def get_compact(nbits):
    return b"\xff" * 32

def adjust_nbits_by_multiplier(nbits, multiplier, default_difficulty):
    # Provide a sane int nbits for tests regardless of input type
    try:
        nbits_int = int(nbits)
    except Exception:
        nbits_int = 0x1d00ffff
    return {"target_bytes": b"\xff" * 32, "nbits": nbits_int}

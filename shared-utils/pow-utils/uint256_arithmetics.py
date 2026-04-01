# SPDX-License-Identifier: Apache-2.0
# Uint256 arithmetic 
def set_compact(nCompact: int):
    """
    Convert a compact nBits value (uint32) into a full 256-bit target integer,
    along with negative and overflow flags, exactly as in C++ arith_uint256::SetCompact.
    """
    # Extract exponent (size) and mantissa (nWord)
    exponent = nCompact >> 24
    mantissa = nCompact & 0x007fffff
    negative = bool(nCompact & 0x00800000)
    
    # Reconstruct the full target
    if exponent <= 3:
        target = mantissa >> (8 * (3 - exponent))
    else:
        target = mantissa << (8 * (exponent - 3))
    
    # Detect overflow (mantissa != 0 and target wouldn't fit in 256 bits)
    overflow = False
    if mantissa != 0:
        # exponent > 34 means shifting by > 248 bits → overflow 256-bit range
        if exponent > 34:
            overflow = True
        else:
            # C++ also checks if mantissa is too large to fit after shift
            # i.e., nWord > 0xff << (8*(exponent-3))
            if exponent > 3 and mantissa > (0xff << (8 * (exponent - 3))):
                overflow = True

    return target, negative, overflow

def get_compact(target: int, negative: bool = False):
    """
    Convert a full 256-bit target integer into compact nBits (uint32),
    optionally preserving a negative flag, exactly as in C++ arith_uint256::GetCompact.
    """
    if isinstance(target, (bytes, bytearray)):
        target = int.from_bytes(target, byteorder="big")
            
    # Determine byte size of target
    size = (target.bit_length() + 7) // 8
    
    # Shift target to fit into 3-byte mantissa
    if size <= 3:
        mantissa = target << (8 * (3 - size))
    else:
        mantissa = target >> (8 * (size - 3))
    
    # If the sign bit (0x00800000) is set, shift mantissa down and bump exponent
    if mantissa & 0x00800000:
        mantissa >>= 8
        size += 1
    
    # Compose compact: 1-byte exponent, 3-byte mantissa
    compact = (size << 24) | (mantissa & 0x007fffff)
    if negative and mantissa != 0:
        compact |= 0x00800000
    
    return compact

def adjust_nbits_by_multiplier(nbits: int, multiplier: int, divider: int):
    # 1. Unpack correctly
    target, negative, overflow = set_compact(nbits)

    # 2. Scale target
    new_target = (target * multiplier) // divider

    # 3. (Optionally) detect if new_target overflows 256 bits
    new_overflow = new_target.bit_length() > 256

    # 4. Re-pack into "compact"
    new_nbits = get_compact(new_target, negative)

    # 5. Make sure that the loss of resolution in compact represenatation does not fuck up target accuracy downstream
    new_target, negative, overflow = set_compact(new_nbits)

    # 6. Return everything
    return {
      "nbits": new_nbits,
      "target_bytes": new_target.to_bytes(32, byteorder="big"),
      "overflow": new_overflow,
      "negative": negative
    }
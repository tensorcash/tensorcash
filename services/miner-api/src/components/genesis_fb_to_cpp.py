#!/usr/bin/env python3
"""
Flatbuffer Binary to C++ Code Generator
Converts a flatbuffer .bin file to C++ code that instantiates a CProofBlob object
"""

import sys
import os
import argparse
from datetime import datetime
import hashlib
# You'll need to import the generated Python flatbuffer classes
# Assuming they're in a module called 'proof' based on the C++ namespace
try:
    from proof import Proof
    from proof import FloatArray
    from proof import UIntArray
    from proof import MiningResponse
    from components import genesis

except ImportError:
    print("Error: Could not import flatbuffer Python modules.")
    print("Please ensure you've generated Python classes from your .fbs schema file using:")
    print("  flatc --python your_schema.fbs")
    sys.exit(1)

#!/usr/bin/env python3
"""
Flatbuffer Binary to C++ Code Generator
Converts a flatbuffer .bin file to C++ code that instantiates a CProofBlob object

Expected flatbuffer schema structure (example):
    namespace proof;
    
    table FloatArray {
        values:[float];
    }
    
    table UIntArray {
        values:[uint];
    }
    
    table Proof {
        version:uint;
        tick:uint;
        timestamp:uint;
        target:[ubyte];
        vdf:[ubyte];
        hash:[ubyte];
        block_hash:[ubyte];
        header_prefix:[ubyte];
        is_solution:bool;
        model_identifier:string;
        compute_precision:string;
        ipfs_cid:string;
        extra_flags:string;
        temperature:float;
        top_p:float;
        top_k:uint;
        repetition_penalty:float;
        chosen_tokens:[uint];
        chosen_probs:[float];
        sampling_u:[float];
        softmax_normalizers:[float];
        prompt_tokens:[uint];
        pad_mask:[uint];
        topk_logits:[FloatArray];
        topk_indices:[UIntArray];
        logsumexp_stats:[FloatArray];
    }
    
    root_type Proof;
"""

import sys
import os
import argparse
from datetime import datetime
import flatbuffers

# Optional numpy import for better performance if available
try:
    import numpy as np
    # IEEE 754 float32 has ~7.22 decimal digits of precision
    # Using 8 provides margin for rounding while avoiding false precision
    FLOAT32_DECIMAL_DIGITS = 8
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False
    FLOAT32_DECIMAL_DIGITS = 8
    
# You'll need to import the generated Python flatbuffer classes
# Assuming they're in a module called 'proof' based on the C++ namespace
try:
    import proof.Proof
except ImportError:
    print("Error: Could not import flatbuffer Python modules.")
    print("Please ensure you've generated Python classes from your .fbs schema file using:")
    print("  flatc --python your_schema.fbs")
    print("\nIf your schema is in a different namespace, update the import statement.")
    sys.exit(1)

def format_byte_vector(data, indent=4, items_per_line=16):
    """Format a byte vector as C++ initializer list"""
    if not data:
        return "{}"
    
    lines = []
    for i in range(0, len(data), items_per_line):
        chunk = data[i:i+items_per_line]
        hex_values = [f"0x{b:02X}" for b in chunk]
        lines.append(" " * indent + ", ".join(hex_values) + ("," if i + items_per_line < len(data) else ""))
    
    return "{\n" + "\n".join(lines) + "\n" + " " * (indent - 4) + "}"

def _cpp_float(v, precision=FLOAT32_DECIMAL_DIGITS):
    """Return v as a VALID C++ float literal (always has '.' or an exponent, + 'f').

    Plain '%g' yields e.g. '1' for 1.0 -> the invalid literal '1f'
    ("unable to find numeric literal operator 'operator\"\"f'"). Ensure a
    decimal point or exponent is always present.
    """
    if v != v or v in (float('inf'), float('-inf')):
        return "0.0f"
    av = abs(v)
    if av != 0.0 and (av < 1e-6 or av > 1e7):
        s = f"{v:.{precision-1}e}"
    else:
        s = f"{v:.{precision}g}"
        if 'e' not in s and 'E' not in s and '.' not in s:
            s += ".0"
    return s + "f"


def format_numeric_vector(data, indent=4, items_per_line=8, float_precision=FLOAT32_DECIMAL_DIGITS):
    """Format a numeric vector as C++ initializer list with adaptive decimal formatting.

    Uses fixed-point notation to avoid scientific notation in C++ code,
    while maintaining float32 precision (~7-8 significant figures).
    """
    if not data:
        return "{}"

    lines = []
    for i in range(0, len(data), items_per_line):
        chunk = data[i:i+items_per_line]
        if isinstance(data[0], float):
            values = [_cpp_float(v, float_precision) for v in chunk]
        else:
            # bools (e.g. pad_mask) -> 0/1; ints unchanged. Python's "True"/"False"
            # are not valid C++ for a uint field.
            values = [str(int(v)) if isinstance(v, bool) else str(v) for v in chunk]
        lines.append(" " * indent + ", ".join(values) + ("," if i + items_per_line < len(data) else ""))

    return "{\n" + "\n".join(lines) + "\n" + " " * (indent - 4) + "}"

def format_2d_vector(data, indent=4, float_precision=8):
    """Format a 2D vector as C++ initializer list with adaptive decimal formatting.

    Uses float_precision=8 (up from 6) to properly represent float32 precision.
    Avoids scientific notation to ensure valid C++ aggregate initialization.
    """
    if not data:
        return "{}"

    lines = ["    {"]
    for i, row in enumerate(data):
        if not row:
            lines.append(" " * (indent + 4) + "{}" + ("," if i < len(data) - 1 else ""))
        else:
            lines.append(" " * (indent + 4) + "{")
            # Format each row
            for j in range(0, len(row), 8):
                chunk = row[j:j+8]
                if isinstance(row[0], float):
                    values = [_cpp_float(v, float_precision) for v in chunk]
                else:
                    values = [str(int(v)) if isinstance(v, bool) else str(v) for v in chunk]
                comma = "," if j + 8 < len(row) else ""
                lines.append(" " * (indent + 8) + ", ".join(values) + comma)
            lines.append(" " * (indent + 4) + "}" + ("," if i < len(data) - 1 else ""))
    lines.append(" " * indent + "}")

    return "\n".join(lines)

def escape_string(s):
    """Escape a string for C++ string literal"""
    if not s:
        return '""'
    # Escape backslashes first, then other special characters
    s = s.replace('\\', '\\\\')
    s = s.replace('"', '\\"')
    s = s.replace('\n', '\\n')
    s = s.replace('\r', '\\r')
    s = s.replace('\t', '\\t')
    return f'"{s}"'

def extract_byte_vector(proof_data, field_name):
    """Extract byte vector from flatbuffer trying different accessor patterns"""
    # Try AsNumpy method first (if numpy is available)
    if HAS_NUMPY:
        try:
            numpy_method = getattr(proof_data, f"{field_name}AsNumpy")
            result = numpy_method()
            if result is not None:
                return result.tolist()
        except (AttributeError, TypeError):
            pass
    
    # Try Length + indexed access
    try:
        length_method = getattr(proof_data, f"{field_name}Length")
        length = length_method()
        if length > 0:
            accessor = getattr(proof_data, field_name)
            return [accessor(i) for i in range(length)]
    except (AttributeError, TypeError):
        pass
    
    return []

def extract_numeric_vector(proof_data, field_name):
    """Extract numeric vector from flatbuffer trying different accessor patterns"""
    return extract_byte_vector(proof_data, field_name)  # Same logic applies

def generate_cpp_code(proof_data, class_name="g_genesisBlob"):
    """Generate C++ code that instantiates a CProofBlob with the given data"""
    
    code = []
    code.append("// Auto-generated genesis blob data")
    code.append(f"// Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    code.append("")
    code.append('#include "proofblob.h"')
    code.append("")
    code.append("// Genesis blob instance with all values explicitly defined")
    code.append(f"static CProofBlob {class_name} = {{")
    
    # Basic fields
    code.append(f"    .version = {proof_data.Version()},")
    code.append(f"    .tick = {proof_data.Tick()},")
    code.append(f"    .timestamp = {proof_data.Timestamp()},")
    
    # Byte vectors
    code.append("    .target = " + format_byte_vector(extract_byte_vector(proof_data, 'Target'), 8) + ",")
    code.append("    .vdf = " + format_byte_vector(extract_byte_vector(proof_data, 'Vdf'), 8) + ",")
    code.append("    .hash = " + format_byte_vector(extract_byte_vector(proof_data, 'Hash'), 8) + ",")
    code.append("    .block_hash = " + format_byte_vector(extract_byte_vector(proof_data, 'BlockHash'), 8) + ",")
    code.append("    .header_prefix = " + format_byte_vector(extract_byte_vector(proof_data, 'HeaderPrefix'), 8) + ",")
    
    # Boolean
    code.append(f"    .is_solution = {str(proof_data.IsSolution()).lower()},")
    
    # Strings - check if they exist first
    model_id = proof_data.ModelIdentifier()
    compute_prec = proof_data.ComputePrecision()
    ipfs_cid = proof_data.IpfsCid()
    extra_flags = proof_data.ExtraFlags()
    
    # Helper to decode strings safely
    def safe_decode(s):
        if s is None:
            return ''
        if isinstance(s, bytes):
            return s.decode('utf-8')
        return str(s)
    
    code.append(f"    .model_identifier = {escape_string(safe_decode(model_id))},")
    code.append(f"    .compute_precision = {escape_string(safe_decode(compute_prec))},")
    code.append(f"    .ipfs_cid = {escape_string(safe_decode(ipfs_cid))},")
    code.append(f"    .extra_flags = {escape_string(safe_decode(extra_flags))},")
    
    # Float parameters
    code.append(f"    .temperature = {_cpp_float(proof_data.Temperature())},")
    code.append(f"    .top_p       = {_cpp_float(proof_data.TopP())},")
    code.append(f"    .top_k = {proof_data.TopK()},")
    code.append(f"    .repetition_penalty = {_cpp_float(proof_data.RepetitionPenalty())},")
    
    # Numeric vectors
    code.append("    .chosen_tokens = " + format_numeric_vector(extract_numeric_vector(proof_data, 'ChosenTokens'), 8) + ",")
    code.append("    .chosen_probs = " + format_numeric_vector(extract_numeric_vector(proof_data, 'ChosenProbs'), 8) + ",")
    code.append("    .sampling_u = " + format_numeric_vector(extract_numeric_vector(proof_data, 'SamplingU'), 8) + ",")
    code.append("    .softmax_normalizers = " + format_numeric_vector(extract_numeric_vector(proof_data, 'SoftmaxNormalizers'), 8) + ",")
    code.append("    .prompt_tokens = " + format_numeric_vector(extract_numeric_vector(proof_data, 'PromptTokens'), 8) + ",")
    code.append("    .pad_mask = " + format_numeric_vector(extract_numeric_vector(proof_data, 'PadMask'), 8) + ",")
    
    # 2D arrays - extract data from flatbuffer format
    topk_logits = []
    try:
        for i in range(proof_data.TopkLogitsLength()):
            fa = proof_data.TopkLogits(i)
            if fa:
                # Try AsNumpy first if available
                if HAS_NUMPY:
                    try:
                        row = fa.ValuesAsNumpy().tolist()
                    except (AttributeError, TypeError):
                        # Fall back to indexed access
                        row = []
                        for j in range(fa.ValuesLength()):
                            row.append(fa.Values(j))
                else:
                    # Use indexed access
                    row = []
                    for j in range(fa.ValuesLength()):
                        row.append(fa.Values(j))
                topk_logits.append(row)
            else:
                topk_logits.append([])
    except (AttributeError, TypeError):
        pass
    
    topk_indices = []
    try:
        for i in range(proof_data.TopkIndicesLength()):
            ua = proof_data.TopkIndices(i)
            if ua:
                # Try AsNumpy first if available
                if HAS_NUMPY:
                    try:
                        row = ua.ValuesAsNumpy().tolist()
                    except (AttributeError, TypeError):
                        # Fall back to indexed access
                        row = []
                        for j in range(ua.ValuesLength()):
                            row.append(ua.Values(j))
                else:
                    # Use indexed access
                    row = []
                    for j in range(ua.ValuesLength()):
                        row.append(ua.Values(j))
                topk_indices.append(row)
            else:
                topk_indices.append([])
    except (AttributeError, TypeError):
        pass
    
    logsumexp_stats = []
    try:
        for i in range(proof_data.LogsumexpStatsLength()):
            fa = proof_data.LogsumexpStats(i)
            if fa:
                # Try AsNumpy first if available
                if HAS_NUMPY:
                    try:
                        row = fa.ValuesAsNumpy().tolist()
                    except (AttributeError, TypeError):
                        # Fall back to indexed access
                        row = []
                        for j in range(fa.ValuesLength()):
                            row.append(fa.Values(j))
                else:
                    # Use indexed access
                    row = []
                    for j in range(fa.ValuesLength()):
                        row.append(fa.Values(j))
                logsumexp_stats.append(row)
            else:
                logsumexp_stats.append([])
    except (AttributeError, TypeError):
        pass
    
    code.append("    .topk_logits = " + format_2d_vector(topk_logits, 8) + ",")
    code.append("    .topk_indices = " + format_2d_vector(topk_indices, 8) + ",")
    code.append("    .logsumexp_stats = " + format_2d_vector(logsumexp_stats, 8))
    
    code.append("};")
    code.append("")

    code.append(generate_genesis_comments(proof_data))    
    return "\n".join(code)

def generate_genesis_comments(proof_data):
    """Generate comments with genesis block parameters computed from proof data"""
    
    # Extract header prefix first to get the actual header data
    header_prefix = extract_byte_vector(proof_data, 'HeaderPrefix')
    
    # Extract version and timestamp from header prefix (Bitcoin block header format)
    # Block header structure: version(4) + prev_hash(32) + merkle_root(32) + timestamp(4) + bits(4) + nonce(4)
    version = 0
    timestamp = 0
    
    if len(header_prefix) >= 4:
        # Version is first 4 bytes (little endian)
        version = (header_prefix[0] | 
                  (header_prefix[1] << 8) | 
                  (header_prefix[2] << 16) | 
                  (header_prefix[3] << 24))
    
    if len(header_prefix) >= 72:
        # Timestamp is at bytes 68-71 (little endian)
        timestamp = (header_prefix[68] | 
                    (header_prefix[69] << 8) | 
                    (header_prefix[70] << 16) | 
                    (header_prefix[71] << 24))
    
    # Extract nonce from first 4 bytes of hash
    hash_data = extract_byte_vector(proof_data, 'Hash')
    nonce = 0
    nonce_bytes = []
    if len(hash_data) >= 4:
        # Convert first 4 bytes to uint32 (little endian)
        nonce = (hash_data[0] | 
                (hash_data[1] << 8) | 
                (hash_data[2] << 16) | 
                (hash_data[3] << 24))
        nonce_bytes = hash_data[:4]
    
    # Extract header prefix to get more genesis info
    header_prefix = extract_byte_vector(proof_data, 'HeaderPrefix')
    
    # Compute short hash as header_prefix | nonce (concatenated)
    short_hash_bytes = header_prefix + nonce_bytes
    short_hash_hex = ''.join(f'{b:02x}' for b in short_hash_bytes)
    
    # Compute double SHA256 of short hash
    double_sha256_hash = ""
    if short_hash_bytes:
        first_hash = hashlib.sha256(bytes(short_hash_bytes)).digest()
        second_hash = hashlib.sha256(first_hash).digest()
        double_sha256_hash = ''.join(f'{b:02x}' for b in second_hash)

    # Try to extract difficulty from header prefix if available
    # Typically bits/difficulty is at bytes 72-75 in block header
    bits = 0
    if len(header_prefix) >= 76:
        bits = (header_prefix[72] | 
               (header_prefix[73] << 8) | 
               (header_prefix[74] << 16) | 
               (header_prefix[75] << 24))
    
    # Extract block hash and merkle root if available
    block_hash_data = extract_byte_vector(proof_data, 'BlockHash')
    block_hash_hex = ''.join(f'{b:02x}' for b in block_hash_data) if block_hash_data else "unknown"
    
    # Try to extract merkle root from header prefix (bytes 36-67 typically)
    merkle_root_hex = "unknown"
    if len(header_prefix) >= 68:
        merkle_root_bytes = header_prefix[36:68]
        merkle_root_hex = ''.join(f'{b:02x}' for b in merkle_root_bytes)
    
    # Genesis constants validation and info
    validation_notes = []
    genesis_seed = "unknown"
    genesis_pubkey = "unknown"
    genesis_difficulty = "unknown"
    genesis_reward = getattr(genesis, 'GENESIS_REWARD_COINS', 50)

    try:
        genesis_seed = getattr(genesis, 'SEED_PHRASE', 'not found')
        genesis_pubkey = getattr(genesis, 'GENESIS_PUBKEY', 'not found')
        genesis_difficulty = getattr(genesis, 'GENESIS_DIFFICULTY', 'not found')
        
        # Validation checks
        if hasattr(genesis, 'GENESIS_DIFFICULTY'):
            if bits != 0 and bits != genesis.GENESIS_DIFFICULTY:
                validation_notes.append(f"WARNING: Computed difficulty 0x{bits:08X} != expected 0x{genesis.GENESIS_DIFFICULTY:08X}")
            else:
                validation_notes.append(f"✓ Difficulty matches: 0x{bits:08X}")
        
    except Exception as e:
        validation_notes.append(f"Error accessing genesis constants: {e}")

    # Format the genesis parameters
    comments = []
    comments.append("")
    comments.append("/*")
    comments.append(" * Genesis Block Parameters (computed from proof data)")
    comments.append(" * Copy these values to your genesis block configuration:")
    comments.append(" */")
    comments.append("")
    
    # Add validation notes if any
    if validation_notes:
        comments.append("// Validation against genesis constants:")
        for note in validation_notes:
            comments.append(f"// {note}")
        comments.append("")
    
    comments.append("// Genesis block creation parameters:")
    comments.append(f"// Timestamp: {timestamp}")
    comments.append(f"// Nonce: {nonce}")
    comments.append(f"// Bits/Difficulty: 0x{bits:08X}")
    comments.append(f"// Version: {version}")
    comments.append(f"// Reward: {genesis_reward} * COIN  // Tensor genesis reward (GENESIS_REWARD_COINS)")
    comments.append("")
    
    # Add genesis constants info
    comments.append("// Genesis constants from components.genesis:")
    comments.append(f'// SEED_PHRASE: "{genesis_seed}"')
    comments.append(f'// GENESIS_PUBKEY: "{genesis_pubkey}"')
    comments.append(f'// GENESIS_DIFFICULTY: 0x{genesis_difficulty:08X}' if isinstance(genesis_difficulty, int) else f'// GENESIS_DIFFICULTY: {genesis_difficulty}')
    comments.append("")
    
    comments.append("// For CreateGenesisBlockNew function (kernel/chainparams.cpp):")
    comments.append(f'// genesis = CreateGenesisBlockNew({timestamp}, {nonce}, 0x{bits:08X}, {version}, {genesis_reward} * COIN);')
    comments.append("")
    comments.append("// Expected hash values for assertions:")
    comments.append(f'// assert(consensus.hashGenesisBlockShort == uint256{{"{double_sha256_hash}"}});')
    comments.append(f'// assert(genesis.hashMerkleRoot == uint256{{"{merkle_root_hex}"}});')
    comments.append("")
    comments.append("// Raw data for reference:")
    comments.append(f"// Block hash: {block_hash_hex}")
    comments.append(f"// Short hash (header_prefix | nonce): {short_hash_hex}")
    comments.append(f"// Header prefix length: {len(header_prefix)} bytes")
    comments.append(f"// Nonce bytes: {' '.join(f'{b:02x}' for b in nonce_bytes)}")
    if header_prefix:
        comments.append("// Header prefix (first 32 bytes): " + 
                       ''.join(f'{b:02x}' for b in header_prefix[:32]))
    comments.append("")
    comments.append("// C++ Genesis Block Function Template:")
    comments.append("/*")
    comments.append("static CBlock CreateGenesisBlock(uint32_t nTime, uint32_t nNonce, uint32_t nBits, int32_t nVersion, const CAmount& genesisReward)")
    comments.append("{")
    comments.append(f'    const char* pszTimestamp = "{genesis_seed}";')
    comments.append(f'    const CScript genesisOutputScript = CScript() << "{genesis_pubkey}"_hex << OP_CHECKSIG;')
    comments.append("    return CreateGenesisBlock(pszTimestamp, genesisOutputScript, nTime, nNonce, nBits, nVersion, genesisReward);")
    comments.append("}")
    comments.append("*/")
    
    return "\n".join(comments)


def main():
    parser = argparse.ArgumentParser(description="Convert flatbuffer binary to C++ code")
    parser.add_argument("input", help="Input .bin file containing flatbuffer data")
    parser.add_argument("-o", "--output", help="Output C++ file (default: genesis_blob.cpp)")
    parser.add_argument("-n", "--name", default="g_genesisBlob", help="Variable name for the CProofBlob instance")
    parser.add_argument("--header", action="store_true", help="Generate as header file (.h) instead of source file")
    parser.add_argument("--debug", action="store_true", help="Show debug information about the flatbuffer structure")
    
    args = parser.parse_args()
    
    # Read the binary file
    try:
        with open(args.input, 'rb') as f:
            buf = f.read()
    except IOError as e:
        print(f"Error reading file: {e}")
        sys.exit(1)
    
    # Parse the flatbuffer - try MiningResponse first (wrapper format), then raw Proof
    proof_data = None
    try:
        # First try parsing as MiningResponse (the wrapper format saved by C++ writer)
        mining_resp = proof.MiningResponse.MiningResponse.GetRootAsMiningResponse(buf, 0)
        # Extract the Proof from inside the MiningResponse
        proof_data = mining_resp.PowBlob()
        if proof_data:
            print(f"Successfully unwrapped Proof from MiningResponse (req_id={mining_resp.ReqId()}, nonce={mining_resp.Nonce()})")
        else:
            raise ValueError("MiningResponse has no PowBlob")
    except Exception as e:
        # If that fails, try parsing as raw Proof
        try:
            proof_data = proof.Proof.Proof.GetRootAsProof(buf, 0)
            print("Parsed as raw Proof format")
        except Exception as e2:
            print(f"Error parsing flatbuffer as MiningResponse: {e}")
            print(f"Error parsing flatbuffer as Proof: {e2}")
            print("\nMake sure:")
            print("1. The file contains valid flatbuffer data")
            print("2. The Python classes match the schema used to create the data")
            sys.exit(1)
    
    # Debug mode - show available methods
    if args.debug:
        print("Available methods on proof_data:")
        for method in dir(proof_data):
            if not method.startswith('_'):
                print(f"  {method}")
        print("\nTrying to read basic fields:")
        try:
            print(f"  Version: {proof_data.Version()}")
            print(f"  Tick: {proof_data.Tick()}")
            print(f"  Timestamp: {proof_data.Timestamp()}")
        except Exception as e:
            print(f"  Error reading basic fields: {e}")
        return
    
    # Generate C++ code
    try:
        cpp_code = generate_cpp_code(proof_data, args.name)
    except Exception as e:
        print(f"Error generating C++ code: {e}")
        print("\nTry running with --debug to see available methods")
        sys.exit(1)
    
    # Determine output filename
    if args.output:
        output_file = args.output
    else:
        base_name = os.path.splitext(os.path.basename(args.input))[0]
        ext = ".h" if args.header else ".cpp"
        output_file = f"{base_name}_genesis{ext}"
    
    # If generating header file, wrap in header guards
    if args.header or output_file.endswith('.h'):
        guard_name = os.path.basename(output_file).upper().replace('.', '_')
        header_code = f"#ifndef {guard_name}\n#define {guard_name}\n\n"
        header_code += cpp_code
        header_code += f"\n#endif // {guard_name}\n"
        cpp_code = header_code
    
    # Write the output
    try:
        with open(output_file, 'w') as f:
            f.write(cpp_code)
        print(f"Generated C++ code written to: {output_file}")
    except IOError as e:
        print(f"Error writing output file: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
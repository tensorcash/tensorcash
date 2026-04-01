import hashlib
import struct
from utils.uint256_arithmetics import set_compact, get_compact, adjust_nbits_by_multiplier


def dsha256(b: bytes) -> bytes:
    return hashlib.sha256(hashlib.sha256(b).digest()).digest()

def compact_size(n: int) -> bytes:
    """Bitcoin CompactSize (varint) encoder."""
    if n < 0xfd:
        return struct.pack('<B', n)
    elif n <= 0xffff:
        return b'\xfd' + struct.pack('<H', n)
    elif n <= 0xffffffff:
        return b'\xfe' + struct.pack('<I', n)
    else:
        return b'\xff' + struct.pack('<Q', n)

def push_data(data: bytes, *, warn=True, strict=False) -> bytes:
    """
    Canonical Script push (matches what CScript does).
    - warn: print a warning if we need OP_PUSHDATA1/2/4
    - strict: raise if >75 (so you can force-short headlines)
    """
    l = len(data)
    if l > 75:
        if strict:
            raise ValueError(f"push_data: {l} bytes > 75; will require OP_PUSHDATA1/2/4")
        if warn:
            print(f"[warn] push_data: {l} bytes -> OP_PUSHDATA1/2/4 will be used")

    if l <= 75:
        return struct.pack('<B', l) + data
    elif l <= 0xff:
        return b'\x4c' + struct.pack('<B', l) + data          # OP_PUSHDATA1
    elif l <= 0xffff:
        return b'\x4d' + struct.pack('<H', l) + data          # OP_PUSHDATA2
    else:
        return b'\x4e' + struct.pack('<I', l) + data          # OP_PUSHDATA4

def generate_genesis_header_prefix(seed_phrase, timestamp, difficulty, version,
                                   nonce=2083236893, pubkey=None, *,
                                   strict_short_headline=False, warn_push=False,
                                   reward_coins: int = None):
    """
    Generate genesis block header prefix (76 bytes - everything except nonce),
    with **canonical** script pushes, so it matches Core.
    """
    # Version
    v_bytes = struct.pack('<I', version)

    # Previous block hash (all zeros for genesis)
    prev_hash = b'\x00' * 32

    # Default to the Tensor genesis pubkey (GENESIS_PUBKEY, module constant below).
    # Resolved at call time, so it is available even though declared after this fn.
    if pubkey is None:
        pubkey = GENESIS_PUBKEY
    if reward_coins is None:
        reward_coins = GENESIS_REWARD_COINS

    # --- Build coinbase script (Bitcoin-genesis style, but canonical) ---
    coinbase_msg = seed_phrase.encode('utf-8')

    # 1) push 4 raw bytes of difficulty (little-endian)
    push_difficulty = b'\x04' + struct.pack('<I', difficulty)

    # 2) push single byte 0x04 (as data, not OP_4): 01 04
    push_literal_04 = b'\x01\x04'

    # 3) canonical push of the headline
    push_headline = push_data(coinbase_msg, warn=warn_push, strict=strict_short_headline)

    coinbase_script = push_difficulty + push_literal_04 + push_headline

    # --- Build coinbase transaction (canonical CompactSize lengths) ---
    version_le = struct.pack('<I', 1)
    input_count = b'\x01'
    prev_txid = b'\x00' * 32
    prev_index = b'\xff\xff\xff\xff'
    script_len = compact_size(len(coinbase_script))
    sequence = b'\xff\xff\xff\xff'
    output_count = b'\x01'
    # Coinbase output value (in satoshis). For Tensor genesis we use 715 TSC.
    value = struct.pack('<Q', reward_coins * 10**8)

    # scriptPubKey: P2PK (67 bytes: 0x41 <pubkey 65B> 0xac)
    spk = b'\x41' + bytes.fromhex(pubkey) + b'\xac'
    spk_len = compact_size(len(spk))

    locktime = struct.pack('<I', 0)

    coinbase_tx = (
        version_le +
        input_count +
        prev_txid +
        prev_index +
        script_len +
        coinbase_script +
        sequence +
        output_count +
        value +
        spk_len +
        spk +
        locktime
    )

    # Calculate merkle root: double-sha256(tx) for the only transaction
    tx_hash = dsha256(coinbase_tx)
    merkle_root = tx_hash  # big-endian bytes

    # Header fields
    header_prefix = (
        v_bytes +
        prev_hash +
        merkle_root +
        struct.pack('<I', timestamp) +
        struct.pack('<I', difficulty)
    )
    header = header_prefix + struct.pack('<I', nonce)

    # # Prints
    # print("scriptSig len   :", len(coinbase_script))
    # print("scriptSig (hex) :", coinbase_script.hex())
    # print("txid (BE)       :", tx_hash.hex())
    # print("txid (LE)       :", tx_hash[::-1].hex())
    # print("merkle (BE)     :", merkle_root.hex())
    # print("merkle (LE)     :", merkle_root[::-1].hex())

    # hdr_hash = dsha256(header)
    # print("header hash (BE):", hdr_hash.hex())
    # print("header hash (LE/display):", hdr_hash[::-1].hex())

    return prev_hash, merkle_root, header_prefix.hex()
    # return header_prefix.hex()


# secp256k1 curve order (used for deterministic key derivation)
SECP256K1_N = int(
    "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141", 16
)


def derive_tensor_keypair_from_sentence(sentence: str, *, extra_salt: str = ""):
    """
    Deterministically derive a Tensor-specific secp256k1 keypair from a long sentence.

    - Uses SHA256 over a domain-separated string:
        "TensorCash/GenesisKey" || "|" || sentence || "|" || extra_salt
    - Maps to a private key in [1, n-1] where n is the secp256k1 order.
    - Returns (privkey_hex, pubkey_hex) with pubkey in uncompressed 0x04 || X || Y form.

    This is intentionally different from Bitcoin's historical genesis key derivation.
    """
    domain = "TensorCash/GenesisKey"
    material = f"{domain}|{sentence}|{extra_salt}".encode("utf-8")
    seed = hashlib.sha256(material).digest()

    # Map to valid private key range [1, n-1]
    priv_int = int.from_bytes(seed, "big") % (SECP256K1_N - 1) + 1
    priv_bytes = priv_int.to_bytes(32, "big")

    try:
        from ecdsa import SECP256k1, SigningKey  # type: ignore[import]
    except Exception as exc:  # pragma: no cover - dependency hint
        raise RuntimeError(
            "ecdsa package is required for derive_tensor_keypair_from_sentence "
            "(pip install ecdsa)"
        ) from exc

    sk = SigningKey.from_string(priv_bytes, curve=SECP256k1)
    vk = sk.get_verifying_key()

    # Uncompressed 65-byte secp256k1 pubkey (0x04 || X || Y)
    pub_bytes = b"\x04" + vk.to_string()

    return priv_bytes.hex(), pub_bytes.hex()

class SeedPhrasePromptGenerator:
    def __init__(self, seed_phrase):
        self.seed_phrase = seed_phrase

        self.templates = [
            "Comment on {}",
            "Explain the significance of {}",
            "What does {} tell us about society?",
            "Analyze the phrase '{}'",
            "Write a haiku about {}",
            "Summarize {} in one sentence",
            "What happened before {}?",
            "What happened after {}?",
            "Rewrite '{}' for today",
            "Create a meme about {}",
            "Tweet about {}",
            "Write a headline inspired by {}",
            "What if {} never happened?",
            "Make {} funny",
            "Make {} dramatic",
            "How did {} change history?",
            "Write a conspiracy theory about {}",
            "Fact-check '{}'",
            "What would {} look like in 2050?",
            "Create a movie title from {}",
            "Write a song about {}",
            "Make {} into a children's story",
            "Turn {} into a sci-fi plot",
            "Create a podcast episode about {}",
            "Generate clickbait from {}",
            "Make {} into a startup idea",
            "Write a fortune cookie about {}",
            "Make {} into a video game",
            "Write a LinkedIn post about {}",
            "Create a documentary title about {}",
            "Create a warning label for {}",
            "Write a time capsule note about {}",
            "Write a manifesto about {}",
            "Create a university course about {}",
            "Write a Wikipedia entry for {}",
            "Write a news ticker about {}",
            "Create a political campaign from {}",
            "Write a prophecy about {}",
            "Make {} into a museum exhibit",
            "Create a monument to {}",
            "Write a prayer about {}",
            "Make {} into a law",
            "Create a holiday celebrating {}",
            "Write a constitution based on {}",
            "Make {} into a philosophical paradox",
            "Create a mathematical theorem from {}",
            "Write an alien's perspective on {}",
        ]
        
    def generate(self, count=64):
        prompts = []
        for i in range(count):
            template = self.templates[i % len(self.templates)]
            prompts.append(template.format(self.seed_phrase))
        return prompts

# Public coinbase headline embedded in the genesis scriptSig (must match the
# C++ pszTimestamp in CreateGenesisBlockNew, kernel/chainparams.cpp).
SEED_PHRASE = "The New York Times 02/Apr/2025 Trump Announces Sweeping Tariffs on All Imports"

# Tensor genesis coinbase reward, in whole TSC. Must match the genesisReward
# passed at the CreateGenesisBlockNew call site in kernel/chainparams.cpp
# (i.e. GENESIS_REWARD_COINS * COIN). Changing it changes the coinbase tx and
# therefore the genesis merkle root.
GENESIS_REWARD_COINS = 715

# Genesis output pubkey (uncompressed secp256k1, P2PK form). Derived locally and
# privately from a passphrase via derive_genesis_key.py — the private key is held
# offline and never lives in this repo. Must match the genesisOutputScript pubkey
# in CreateGenesisBlockNew (kernel/chainparams.cpp).
GENESIS_PUBKEY = (
    "047acd8421eb4bd5f3a9cf822e393a8ac9ab773ef4d90a92345ce1598cbf62eb4b5ad9d4ce1fa06cd0f3f1edfbe9b0352fac08a2646a244ecd03acbfeb000cdf13"
)

hashes_per_sec = 2 
int_difficulty = int( (2**256) * (1/(60*hashes_per_sec)) * (1/6) )
nBits = get_compact(int_difficulty)

GENESIS_DIFFICULTY = nBits

print( "#################### GENESIS FUNCTION IMPORTED ###################################")
print( f"####### Difficulty nBits: {nBits}, hex: {hex(nBits)}")
print( f"####### Seed Phrase: {SEED_PHRASE}")
print( f"####### PubKey: {GENESIS_PUBKEY}")

"""
ZMQ listener for receiving mining job updates
"""
import threading
import logging
import struct
import zmq
import flatbuffers
import time 
import os 
import json 

from components.context import LockFreeContext
from components import constants
from proof import BlockHeader

if constants.GENESIS_GENERATOR:
    from components import genesis 
    from components.genesis import generate_genesis_header_prefix

logger = logging.getLogger(__name__)

class ZMQListener:
    """Listens for mining jobs via ZMQ and updates context"""
    
    def __init__(self, context: LockFreeContext, vdf_service=None, test_mode=False):
        self.context = context
        self.vdf_service = vdf_service  # Reference to VDF service for immediate reset
        self.pull_port = constants.ZMQ_PULL_PORT
        self.recv_timeout = constants.ZMQ_RECV_TIMEOUT_MS
        self.difficulty = constants.BASE_NBITS
        
        self.running = False
        self.thread = None
        self._zmq_context = None
        self._socket = None
        self.test_mode = test_mode

    def set_vdf_service(self, vdf_service):
        """Set VDF service reference (for circular dependency resolution)"""
        self.vdf_service = vdf_service
        
    def start(self):
        """Start ZMQ listener in background thread"""
        if self.running:
            logger.warning("ZMQ listener already running")
            return
            
        self.running = True
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        logger.info(f"ZMQ listener started on port {self.pull_port}")
    
    def stop(self):
        """Stop ZMQ listener gracefully"""
        logger.info("Stopping ZMQ listener...")
        self.running = False
        
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)
            if self.thread.is_alive():
                logger.error("ZMQ thread did not stop cleanly")
        
        logger.info("ZMQ listener stopped")
    
    def _run(self):
        """Main ZMQ loop - runs in background thread"""
        logger.info("ZMQ thread started")
        
        try:
            # Create ZMQ context and socket
            self._zmq_context = zmq.Context()
            self._socket = self._zmq_context.socket(zmq.PULL)
            self._socket.bind(f"tcp://*:{self.pull_port}")
            self._socket.setsockopt(zmq.RCVTIMEO, self.recv_timeout)
            
            logger.info(f"ZMQ listening on tcp://*:{self.pull_port}")
            
            while self.running:
                try:
                    if constants.GENESIS_GENERATOR:
                        header_fb = self._generate_genesis_job()
                        if not self.running:
                            break
                        self._process_mining_job(header_fb)
                        time.sleep(5)
                    else:                        
                        # Receive mining job
                        header_fb = self._socket.recv()
                        
                        # Check if we should still process
                        if not self.running:
                            break
                        
                        self._process_mining_job(header_fb)
                    
                except zmq.error.Again:
                    if self.test_mode:
                        for attempt in range(1, constants.ZMQ_RETRY_ATTEMPTS + 1):
                            header_fb = self._generate_test_job()
                            try:
                                self._process_mining_job(header_fb)
                                break
                            except Exception as e:
                                if attempt == constants.ZMQ_RETRY_ATTEMPTS:
                                    logger.error(f"Test job retry {attempt} failed: {e}")
                                else:
                                    delay = constants.ZMQ_RETRY_BACKOFF * (2 ** (attempt - 1))
                                    logger.warning(
                                        f"Test job retry {attempt} failed, retrying in {delay}s: {e}"
                                    )
                                    time.sleep(delay)
                    continue

                except Exception as e:
                    logger.exception(f"Error processing mining job: {e}")
                    
        except Exception as e:
            logger.exception(f"Fatal ZMQ error: {e}")
            
        finally:
            # Clean up resources
            if self._socket:
                self._socket.close()
            if self._zmq_context:
                self._zmq_context.term()
                
        logger.info("ZMQ thread stopped")
    
    def _process_mining_job(self, header_fb: bytes, base_share_target=None):
        """Process received mining job and update context.

        ``base_share_target`` (hex str) rides along from the broker
        MINE_REQUEST template; the legacy zmq core-node path doesn't
        carry one and leaves it None (no sub-block share emission).
        """
        try:
            # First, dump the raw bytes
            logger.debug(f"DEBUG: Raw FlatBuffer bytes (first 100): {header_fb[:100].hex()}")
            logger.debug(f"DEBUG: FlatBuffer total length: {len(header_fb)}")
            
            # Parse FlatBuffer
            request = BlockHeader.BlockHeader.GetRootAs(header_fb, 0)
            
            # Extract raw byte arrays
            prev_hash_bytes = bytes(request.PrevBlockHashAsNumpy())
            merkle_root_bytes = bytes(request.MerkleRootAsNumpy())
            
            logger.debug(f"DEBUG: prev_hash raw bytes: {prev_hash_bytes.hex()}")
            logger.debug(f"DEBUG: merkle_root raw bytes: {merkle_root_bytes.hex()}")
            logger.debug(f"DEBUG: bits: {request.Bits()} (0x{request.Bits():08x})")
            logger.debug(f"DEBUG: version: {request.Version()}")
            logger.debug(f"DEBUG: timestamp: {request.Timestamp()}")
            logger.debug(f"DEBUG: req_id: {request.ReqId()}")            
            # Parse FlatBuffer
            request = BlockHeader.BlockHeader.GetRootAs(header_fb, 0)
            
            # Extract fields
            block_hash = bytes(request.PrevBlockHashAsNumpy()).hex()
            header_prefix = self._build_header_prefix(request)
            base_difficulty = request.Bits()
            request_id = request.ReqId()
            
            logger.info(f"Received mining job: request_id={request_id}, "
                       f"block={block_hash[:16]}..., header_prefix={header_prefix[:16]}..., bits={request.Bits()}")
            
            # Update context (returns True if block changed)
            block_changed = self.context.update_mining(
                block_hash, header_prefix, base_difficulty, request_id,
                base_share_target=base_share_target,
            )
            
            if block_changed:
                logger.info(f"New block detected, triggering immediate VDF reset")
                
                # Trigger immediate VDF reset if service is available
                if self.vdf_service:
                    try:
                        # Force VDF service to check for updates immediately
                        # This ensures VDF starts working on new block right away
                        self.vdf_service.trigger_reset_check()
                    except Exception as e:
                        logger.error(f"Failed to trigger VDF reset: {e}")
                else:
                    logger.warning("VDF service not available for immediate reset")
                
        except Exception as e:
            logger.exception(f"Error parsing mining job: {e}")
    
    def _build_header_prefix(self, request) -> str:
        """Build header prefix from BlockHeader"""
        # Bitcoin header structure (80 bytes total):
        # - version: 4 bytes
        # - previous block hash: 32 bytes  
        # - merkle root: 32 bytes
        # - timestamp: 4 bytes
        # - bits: 4 bytes
        # - nonce: 4 bytes (added later)
        
        header = bytearray()
        header += struct.pack('<I', request.Version())
        header += bytes(request.PrevBlockHashAsNumpy())
        header += bytes(request.MerkleRootAsNumpy())
        header += struct.pack('<I', request.Timestamp())
        #### consider changing this adj nbits for legacy system compatibility so that nonce/target are consistent 
        header += struct.pack('<I', request.Bits())
        
        return header.hex()
    
    def get_status(self) -> dict:
        """Get ZMQ listener status"""
        return {
            "running": self.running,
            "port": self.pull_port,
            "has_socket": self._socket is not None,
            "timeout_ms": self.recv_timeout,
            "has_vdf_service": self.vdf_service is not None
        }
    
    def _generate_test_job(self) -> bytes:
        """Builds a synthetic FlatBuffer BlockHeader for test mode."""
        import time
        builder = flatbuffers.Builder(0)

        # 32-byte zero arrays for prev_hash and merkle_root
        prev_hash = builder.CreateByteVector(b'\x00' * 32)
        merkle_root = builder.CreateByteVector(b'\x00' * 32)

        # Start the FlatBuffer table
        BlockHeader.BlockHeaderStart(builder)
        BlockHeader.BlockHeaderAddVersion(builder, constants.DEFAULT_VERSION)
        BlockHeader.BlockHeaderAddPrevBlockHash(builder, prev_hash)
        BlockHeader.BlockHeaderAddMerkleRoot(builder, merkle_root)
        BlockHeader.BlockHeaderAddTimestamp(builder, int(time.time()))
        BlockHeader.BlockHeaderAddBits(builder, self.difficulty)
        BlockHeader.BlockHeaderAddReqId(builder, 0)  # zero for test
        header = BlockHeader.BlockHeaderEnd(builder)
        builder.Finish(header)

        logger.debug("Generated synthetic test mining job FlatBuffer")
        return bytes(builder.Output())    
    
    def _generate_genesis_job(self) -> bytes:
        """Builds a synthetic FlatBuffer BlockHeader for genesis mode."""
        timestamp = int(time.time())
        prev_hash, merkle_root, header_prefix = generate_genesis_header_prefix(genesis.SEED_PHRASE,
                                                                               timestamp,
                                                                               genesis.GENESIS_DIFFICULTY,
                                                                               constants.DEFAULT_VERSION,
                                                                               nonce=2083236893,
                                                                               pubkey=genesis.GENESIS_PUBKEY
                                                                            )

        log_dir = "/data/pow_proofs/genesis_gen"
        os.makedirs(log_dir, exist_ok=True)

        # Create a record with all relevant info
        genesis_record = {
            "timestamp": timestamp,
            "header_prefix": header_prefix,
            "merkle_root": merkle_root.hex(),
            "difficulty": genesis.GENESIS_DIFFICULTY,
            "difficulty_hex": f"{genesis.GENESIS_DIFFICULTY:08x}",
            "version": constants.DEFAULT_VERSION,
        }

        # Append to log file
        log_file = os.path.join(log_dir, "genesis_jobs.jsonl")
        with open(log_file, "a") as f:
            f.write(json.dumps(genesis_record) + "\n")

        logger.info(f"Generated genesis job - timestamp: {timestamp}, block_hash: {genesis_record['header_prefix'][:16]}...")
        

        builder = flatbuffers.Builder(0)

        prev_hash_vector = builder.CreateByteVector(prev_hash)
        merkle_root_vector = builder.CreateByteVector(merkle_root)

        # Start the FlatBuffer table
        BlockHeader.BlockHeaderStart(builder)
        BlockHeader.BlockHeaderAddVersion(builder, constants.DEFAULT_VERSION)
        BlockHeader.BlockHeaderAddPrevBlockHash(builder, prev_hash_vector)
        BlockHeader.BlockHeaderAddMerkleRoot(builder, merkle_root_vector)
        BlockHeader.BlockHeaderAddTimestamp(builder, timestamp)
        BlockHeader.BlockHeaderAddBits(builder, genesis.GENESIS_DIFFICULTY)
        BlockHeader.BlockHeaderAddReqId(builder, 0) 
        header = BlockHeader.BlockHeaderEnd(builder)
        builder.Finish(header)

        logger.debug("Generated synthetic genesis mining job FlatBuffer")
        return bytes(builder.Output())        
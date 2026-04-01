# SPDX-License-Identifier: Apache-2.0
"""
ZMQ listener service for receiving and logging mining response proofs
"""
import zmq
import logging
import signal
import sys
import os
import flatbuffers
from typing import Optional
import time

# Assuming these imports based on your code structure
from proof import MiningResponse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('mining_responses.log')
    ]
)

logger = logging.getLogger(__name__)

class MiningResponseListener:
    """Listens for mining response proofs via ZMQ and logs them"""
    
    def __init__(self):
        """Initialize the mining response listener"""
        self.pull_port = os.environ.get('ZMQ_PULL_PORT', 7000)
        self.pull_host = os.environ.get('ZMQ_PULL_HOST', '*')
        
        # ZMQ components
        self._zmq_context = None
        self._socket = None
        
        # Control
        self.running = False
        
        # Stats
        self.messages_received = 0
        self.messages_failed = 0
        self._saved_bins = 0
        
    def start(self):
        """Start the listener"""
        logger.info(f"Starting mining response listener on {self.pull_host}:{self.pull_port}")
        
        try:
            # Create ZMQ context and socket
            self._zmq_context = zmq.Context()
            self._socket = self._zmq_context.socket(zmq.PULL)
            self._socket.bind(f"tcp://{self.pull_host}:{self.pull_port}")
            
            # Set socket options
            self._socket.setsockopt(zmq.RCVHWM, 1000)  # High water mark
            self._socket.setsockopt(zmq.LINGER, 1000)  # Linger on close (ms)
            
            logger.info(f"ZMQ listener bound to tcp://{self.pull_host}:{self.pull_port}")
            
            self.running = True
            self._listen_loop()
            
        except Exception as e:
            logger.exception(f"Failed to start listener: {e}")
            raise
        finally:
            self._cleanup()
    
    def stop(self):
        """Stop the listener gracefully"""
        logger.info("Stopping mining response listener...")
        self.running = False
    
    def _listen_loop(self):
        """Main listening loop"""
        logger.info("Mining response listener started, waiting for messages...")
        
        while self.running:
            try:
                # Check for messages with timeout
                if self._socket.poll(timeout=1000):  # 1 second timeout
                    # Receive message
                    message = self._socket.recv()
                    self._process_message(message)
                    self.messages_received += 1
                    
            except zmq.error.ZMQError as e:
                if e.errno == zmq.ETERM:
                    logger.info("ZMQ context terminated, stopping listener")
                    break
                else:
                    logger.error(f"ZMQ receive error: {e}")
                    self.messages_failed += 1
                    
            except KeyboardInterrupt:
                logger.info("Received interrupt signal, stopping...")
                break
                
            except Exception as e:
                logger.exception(f"Error processing message: {e}")
                self.messages_failed += 1
    
    def _process_message(self, message: bytes):
        """
        Process received mining response message
        
        Args:
            message: Raw FlatBuffer message bytes
        """
        # save the first 10 raw messages to disk
        if self._saved_bins < 10:
            filename = f"/data/miner_logs/raw_msg_{self._saved_bins}.bin"
            with open(filename, "wb") as f:
                f.write(message)
            self._saved_bins += 1        
        try:
            # Parse FlatBuffer using the correct API
            mining_response = MiningResponse.MiningResponse.GetRootAs(message, 0)
            
            # Extract basic fields
            req_id = mining_response.ReqId()
            nonce = mining_response.Nonce()
            adjusted_bits = mining_response.AdjustedBits()
            difficulty = mining_response.Difficulty()
            
            # Extract PowBlobHash (byte array)
            pow_blob_hash = b''
            if not mining_response.PowBlobHashIsNone():
                pow_blob_hash = mining_response.PowBlobHashAsNumpy().tobytes()
            
            # Extract PowBlob (nested Proof object)
            pow_blob_info = "None"
            pow_blob_size = 0
            if mining_response.PowBlob() is not None:
                proof_obj = mining_response.PowBlob()
                pow_blob_info = f"Proof object: {type(proof_obj)}"
                # If the Proof object has serialization methods, we could get its size
                try:
                    # Try to get some info about the proof object
                    proof_methods = [m for m in dir(proof_obj) if not m.startswith('_')]
                    pow_blob_info = f"Proof object with methods: {proof_methods[:5]}..."  # Show first 5 methods
                except:
                    pass
            
            # Log the mining response
            logger.info(
                f"MINING RESPONSE - "
                f"req_id={req_id}, "
                f"nonce={nonce}, "
                f"adjusted_bits={adjusted_bits}, "
                f"difficulty={difficulty}, "
                f"pow_blob_hash_len={len(pow_blob_hash)}, "
                f"pow_blob={pow_blob_info}"
            )
            
            # Log hash in hex format if present
            if pow_blob_hash:
                logger.info(f"  pow_blob_hash: {pow_blob_hash.hex()}")
            
            # Log detailed stats periodically
            if self.messages_received % 100 == 0:
                self._log_stats()
                
        except Exception as e:
            logger.exception(f"Failed to parse mining response: {e}")
            logger.error(f"Message length: {len(message)}")
            logger.error(f"Message preview: {message[:64].hex()}...")
    
    def _log_stats(self):
        """Log current statistics"""
        logger.info(
            f"STATS - "
            f"received={self.messages_received}, "
            f"failed={self.messages_failed}, "
            f"success_rate={self.messages_received/(self.messages_received + self.messages_failed)*100:.1f}%"
        )
    
    def _cleanup(self):
        """Clean up ZMQ resources"""
        if self._socket:
            self._socket.close()
            logger.info("ZMQ socket closed")
        if self._zmq_context:
            self._zmq_context.term()
            logger.info("ZMQ context terminated")
    
    def get_stats(self) -> dict:
        """Get listener statistics"""
        return {
            "running": self.running,
            "port": self.pull_port,
            "messages_received": self.messages_received,
            "messages_failed": self.messages_failed,
            "success_rate": self.messages_received / (self.messages_received + self.messages_failed) * 100 if (self.messages_received + self.messages_failed) > 0 else 0
        }


def signal_handler(signum, frame):
    """Handle shutdown signals"""
    logger.info(f"Received signal {signum}, shutting down...")
    sys.exit(0)


def main():
    """Main entry point"""
    # Set up signal handlers
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Create and start listener
    listener = MiningResponseListener()
    
    try:
        listener.start()
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.exception(f"Listener failed: {e}")
        sys.exit(1)
    finally:
        listener.stop()
        logger.info("Mining response listener stopped")


if __name__ == "__main__":
    main()

# sudo API_KEY=super-secret-token      MODEL_API_KEY=super-secret-token      RPC_USER=user1      RPC_PASS=pass1  CU_VERSION=cu123 ZMQ_PUSH_HOST=localhost   docker compose -f deployments/docker-compose/core-miner-api/docker-compose-nodeminer.yaml up --build
# sudo docker exec -it vllm-miner-api /bin/bash
# cd /app/vllm/vllm/sampling && ZMQ_PULL_HOST=localhost python zmq_test_listener.py
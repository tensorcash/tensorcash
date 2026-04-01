# SPDX-License-Identifier: Apache-2.0
import zmq
import time
import os

def send_binary_files():
    # Setup ZMQ
    context = zmq.Context()
    socket = context.socket(zmq.PUSH)  # or zmq.PUSH depending on your needs
    socket.connect("tcp://localhost:7000")
    
    # Give ZMQ time to establish connection
    time.sleep(0.1)
    
    try:
        # Loop through files 0-9
        while True:
            for i in range(10):
                filename = f"/data/miner_logs/raw_msg_{i}.bin"
                
                # Check if file exists
                if os.path.exists(filename):
                    with open(filename, "rb") as f:
                        message = f.read()
                    
                    # Send the binary data
                    socket.send(message)
                    print(f"Sent file {filename} ({len(message)} bytes)")
                else:
                    print(f"File {filename} not found, skipping")
                
                # Wait 2 seconds before next send
                time.sleep(10)
            
    except KeyboardInterrupt:
        print("Stopping sender...")
    finally:
        socket.close()
        context.term()

if __name__ == "__main__":
    send_binary_files()
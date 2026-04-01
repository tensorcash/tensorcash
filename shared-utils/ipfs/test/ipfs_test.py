#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""
Test script for IPFS model loading functionality.
Run this to verify the IPFS setup works correctly.
"""

import os
import subprocess
import tempfile
import shutil
import time
from pathlib import Path

def test_ipfs_setup():
    """Test basic IPFS functionality."""
    print("🔧 Testing IPFS setup...")
    
    # Test IPFS installation
    try:
        result = subprocess.run(['ipfs', 'version'], capture_output=True, text=True)
        if result.returncode == 0:
            print(f"✅ IPFS installed: {result.stdout.strip()}")
        else:
            print("❌ IPFS not properly installed")
            return False
    except FileNotFoundError:
        print("❌ IPFS binary not found in PATH")
        return False
    
    # Test initialization
    with tempfile.TemporaryDirectory() as temp_dir:
        os.environ['IPFS_PATH'] = temp_dir
        
        try:
            # Initialize
            subprocess.run(['ipfs', 'init', '--profile=lowpower'], 
                          check=True, capture_output=True)
            print("✅ IPFS initialization successful")
            
            # Start daemon in background
            daemon = subprocess.Popen(['ipfs', 'daemon', '--routing=dhtclient'], 
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Wait for daemon to be ready
            import time
            time.sleep(5)
            
            try:
                # Test simple fetch (hello world file)
                result = subprocess.run([
                    'ipfs', 'cat', 'QmT78zSuBmuS4z925WZfrqQ1qHaJ56DQaTfyMUF7F8ff5o',
                    '--timeout=30s'
                ], capture_output=True, text=True, timeout=35)
            finally:
                # Stop daemon
                daemon.terminate()
                daemon.wait()
            
            if result.returncode == 0 and 'hello world' in result.stdout:
                print("✅ IPFS network connectivity working")
                return True
            else:
                print("⚠️  IPFS network connectivity issues")
                print(f"Output: {result.stdout}")
                print(f"Error: {result.stderr}")
                return False
                
        except Exception as e:
            print(f"❌ IPFS test failed: {e}")
            return False

def test_model_download(cid: str = "bafybeidb4ab3vp6wivpusgoxh2iudb3nvf7qpyx4gp5fmdmkbvol6w7mnm"):
    """Test downloading a model structure from IPFS using production pattern."""
    print(f"\n📥 Testing model download with CID: {cid}")
    
    with tempfile.TemporaryDirectory() as temp_dir:
        # Set IPFS_PATH for this test
        ipfs_path = os.path.join(temp_dir, "ipfs_repo")
        os.environ['IPFS_PATH'] = ipfs_path
        
        # Initialize IPFS repo (same as production)
        subprocess.run(['ipfs', 'init', '--profile=lowpower'], 
                      check=True, capture_output=True)
        
        # Minimal working config
        configs = [
            ('Routing.Type', '"dhtclient"'),
            # ('Discovery.MDNS.Enabled', 'false'),
            # ('Reprovider.Strategy', '"manual"'),
        ]
        
        for key, value in configs:
            subprocess.run(['ipfs', 'config', '--json', key, value], 
                          check=True, capture_output=True)
        
        # Start daemon (same as production)
        daemon = subprocess.Popen(['ipfs', 'daemon', '--routing=dhtclient'], 
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        # Wait for daemon to be ready and connect to peers
        for _ in range(30):
            try:
                result = subprocess.run(['ipfs', 'version'], capture_output=True, timeout=2)
                if result.returncode == 0:
                    print("✅ IPFS daemon ready")
                    break
            except:
                pass
            time.sleep(1)
        else:
            daemon.terminate()
            return False
        
        # Wait for peer connections (crucial for downloads)
        print("🔗 Waiting for peer connections...")
        for attempt in range(30):
            try:
                peers = subprocess.run(['ipfs', 'swarm', 'peers'], 
                                     capture_output=True, text=True, timeout=5)
                peer_count = len(peers.stdout.strip().splitlines()) if peers.stdout.strip() else 0
                if peer_count > 0:
                    print(f"✅ Connected to {peer_count} peers")
                    break
                print(f"⏳ Attempt {attempt+1}/30: {peer_count} peers")
            except:
                pass
            time.sleep(2)
        else:
            print("⚠️ No peer connections established")
            # Continue anyway - might still work
        
        try:
            output_path = os.path.join(temp_dir, "test_model")
            
            # Download (same as production - NO timeout, show progress)
            cmd = ['ipfs', 'get', cid, '--output', temp_dir, '--progress']
            
            print(f"Running: {' '.join(cmd)}")
            result = subprocess.run(cmd, text=True)  # Let progress show in real-time
            
            if result.returncode == 0:
                # Check what was actually downloaded
                temp_contents = os.listdir(temp_dir)
                print(f"Downloaded contents: {temp_contents}")
                
                # Files are downloaded directly to temp_dir
                model_files = [f for f in temp_contents if f in [
                    'config.json', 'pytorch_model.bin', 'model.safetensors', 
                    'tokenizer.json', 'tokenizer_config.json'
                ] and f != 'ipfs_repo']
                
                if model_files:
                    print(f"✅ Found model files: {model_files}")
                    return True
                else:
                    print("⚠️  No recognized model files found")
                    print(f"All files: {temp_contents}")
            else:
                print(f"❌ Download failed: {result.stderr}")
                
        except Exception as e:
            print(f"❌ Download test failed: {e}")
        finally:
            # Cleanup daemon (same as production)
            daemon.terminate()
            try:
                daemon.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.kill()
            
        return False

if __name__ == "__main__":
    print("🧪 IPFS Model Loading Test Suite")
    print("=" * 50)
    
    # Basic setup test
    if not test_ipfs_setup():
        print("\n❌ Basic IPFS setup failed - check installation")
        exit(1)
    
    # Model download test (using a small test model CID if available)
    print("\n" + "=" * 50)
    test_cid = os.environ.get('TEST_MODEL_CID')
    if test_cid and test_cid.lower() != 'false':
        if test_model_download(test_cid):
            print("\n✅ All tests passed!")
        else:
            print("\n⚠️  Model download test failed")
    else:
        print("⏭️  Skipping model download test (set TEST_MODEL_CID to test)")
        print("✅ Basic IPFS functionality verified")
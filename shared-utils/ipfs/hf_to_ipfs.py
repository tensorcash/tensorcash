#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
import os
import shutil
import subprocess
import tempfile
import logging
import argparse
import time
import re
from pathlib import Path
from huggingface_hub import snapshot_download

# Use mounted models directory for HF cache
if os.path.exists("/models"):
    os.environ["HF_HOME"] = "/models"

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def check_filestore_support():
    """Check if IPFS filestore is supported and enabled"""
    try:
        # Check IPFS version first
        version_out = run_ipfs(["version"], timeout=10)
        logger.info(f"IPFS version info: {version_out.strip()}")
        
        # Check if filestore is available
        config_out = run_ipfs(["config", "Experimental.FilestoreEnabled"], timeout=10)
        enabled = config_out.strip().lower() == "true"
        
        logger.info(f"Filestore enabled: {enabled}")
        return enabled
    except Exception as e:
        logger.warning(f"Could not check filestore support: {e}")
        return False

def enable_filestore():
    """Enable IPFS filestore for no-copy adds"""
    try:
        logger.info("Enabling IPFS filestore...")
        run_ipfs(["config", "--json", "Experimental.FilestoreEnabled", "true"])
        logger.info("✅ Filestore enabled")
        return True
    except Exception as e:
        logger.error(f"Failed to enable filestore: {e}")
        return False

def validate_hf_file(file_path: Path, snapshot_dir: Path) -> bool:
    """
    Validate that a file belongs to the HF repo and should be included
    """
    # Only include files that are symlinks to blobs (HF's deduplication)
    if not file_path.is_symlink():
        logger.warning(f"Skipping non-symlink file: {file_path.name} (not part of HF repo)")
        return False
    
    # Check if symlink points to blobs directory (HF structure)
    try:
        target = file_path.readlink()
        if not str(target).startswith('../../blobs/'):
            logger.warning(f"Skipping file with unexpected symlink target: {file_path.name} -> {target}")
            return False
    except (OSError, ValueError):
        logger.warning(f"Skipping file with invalid symlink: {file_path.name}")
        return False
    
    # Whitelist of expected HF file patterns
    expected_patterns = [
        r'config\.json$',
        r'tokenizer.*\.json$', 
        r'generation_config\.json$',
        r'model.*\.safetensors$',
        r'model\.safetensors\.index\.json$',
        r'vocab\.(json|txt)$',
        r'merges\.txt$',
        r'special_tokens_map\.json$',
        r'added_tokens\.json$',
        r'pytorch_model.*\.bin$',
        r'model\.bin$',
        r'README\.md$'
    ]
    
    filename = file_path.name
    if not any(re.search(pattern, filename) for pattern in expected_patterns):
        logger.warning(f"Skipping unexpected file: {filename} (not in whitelist)")
        return False
    
    return True

def normalize_directory_hf_aware(src: Path, dst: Path):
    """
    Create normalized directory respecting HF's symlink structure
    """
    fixed_mtime = 1600000000
    logger.info(f"Creating HF-aware normalized structure: {src} -> {dst}")
    
    if dst.exists():
        shutil.rmtree(dst)
    
    dst.mkdir(parents=True, exist_ok=True)
    
    files_linked = 0
    files_skipped = 0
    blobs_seen = set()
    
    # Process all files in the snapshot directory
    for item in sorted(src.iterdir()):
        if not item.is_file() and not item.is_symlink():
            continue
            
        # Validate the file belongs to HF repo
        if not validate_hf_file(item, src):
            files_skipped += 1
            continue
            
        # Get the target blob file
        try:
            resolved_target = item.resolve()
            blob_hash = resolved_target.name
            
            # Track which blobs we're using
            blobs_seen.add(blob_hash)
            
            # Create the normalized file
            dst_file = dst / item.name
            
            # Hardlink to the actual blob (maintaining HF's deduplication)
            os.link(resolved_target, dst_file)
            
            # Normalize timestamp and permissions
            os.utime(dst_file, (fixed_mtime, fixed_mtime), follow_symlinks=False)
            os.chmod(dst_file, 0o644)
            
            files_linked += 1
            logger.debug(f"Linked: {item.name} -> blob {blob_hash[:8]}...")
            
        except (OSError, PermissionError) as e:
            logger.warning(f"Failed to link {item.name}: {e}")
            files_skipped += 1
    
    logger.info(f"HF-aware normalization: {files_linked} files linked, {files_skipped} skipped")
    logger.info(f"Using {len(blobs_seen)} unique blobs from HF cache")
    
    # Verify we have essential files
    essential_files = ['config.json']
    missing = [f for f in essential_files if not (dst / f).exists()]
    if missing:
        raise ValueError(f"Missing essential files after normalization: {missing}")
    
    return dst

def run_ipfs(cmd_args, cwd=None, timeout=300):
    """Run `ipfs <…>` and return stdout, or raise on error."""
    cmd = ["ipfs"] + cmd_args
    logger.debug(f"Running: {' '.join(cmd)}")
    
    try:
        p = subprocess.run(
            cmd,
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        return p.stdout
    except subprocess.TimeoutExpired:
        logger.error(f"IPFS command timed out after {timeout}s: {' '.join(cmd)}")
        raise
    except subprocess.CalledProcessError as e:
        logger.error(f"IPFS command failed: {e.stderr}")
        raise

def wait_for_ipfs_daemon(max_wait=60):
    """Wait for IPFS daemon to be ready"""
    logger.info("Waiting for IPFS daemon...")
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        try:
            run_ipfs(["version"], timeout=5)
            logger.info("IPFS daemon is ready")
            return True
        except Exception:
            time.sleep(2)
    
    raise RuntimeError(f"IPFS daemon not ready after {max_wait}s")

def pin_hf_commit(repo_id, revision, pin=True, hf_cache_home=None):
    """Download HF model, normalize, add to IPFS with filestore if available"""
    logger.info(f"Processing {repo_id}@{revision}")
    
    # Use mounted models directory if available
    if hf_cache_home is None and os.path.exists("/models/hub"):
        hf_cache_home = "/models/hub"
        logger.info(f"Using mounted cache directory: {hf_cache_home}")
    
    # 1. Download to HF cache (no redundant downloads)
    logger.info("Downloading to HF cache...")
    cache_path = Path(snapshot_download(
        repo_id=repo_id,
        revision=revision,
        cache_dir=hf_cache_home,
        ignore_patterns=["*.gguf"]
    ))
    logger.info(f"Found in cache: {cache_path}")
    
    # 2. Check filestore support
    filestore_enabled = check_filestore_support()
    if not filestore_enabled:
        logger.info("Enabling filestore for no-copy adds...")
        if not enable_filestore():
            logger.warning("Filestore not available, will copy data")
    
    # 3. Create normalized directory with validated HF files only
    normalized_name = f"{repo_id.replace('/', '--')}_{revision[:8]}"
    normalized_dir = Path(hf_cache_home) / "ipfs_normalized" / normalized_name
    
    logger.info(f"Creating HF-validated normalized directory: {normalized_dir}")
    normalize_directory_hf_aware(cache_path, normalized_dir)
    
    # 4. Add to IPFS (with filestore if available)
    logger.info("Adding to IPFS...")
    
    add_args = [
        "add", "-r", 
        "--cid-version=1",
        "--hash=sha2-256",
        "--chunker=size-262144"
    ]
    
    # Use filestore if available (no-copy mode)
    if filestore_enabled or check_filestore_support():
        add_args.append("--nocopy")
        logger.info("Using filestore mode (no data duplication)")
    else:
        logger.warning("Filestore not available, copying data")
    
    add_args.append(str(normalized_dir))
    
    out = run_ipfs(add_args)
    
    # Parse output: "added <hash> <path>"
    lines = out.strip().splitlines()
    if not lines:
        raise RuntimeError("No output from ipfs add")
    
    last_line = lines[-1]
    parts = last_line.split()
    if len(parts) < 2 or parts[0] != "added":
        raise RuntimeError(f"Unexpected ipfs add output: {last_line}")
    
    cid = parts[1]  # The hash is the second part
    logger.info(f"Generated CID: {cid}")
    
    # 5. Pin if requested
    if pin:
        logger.info("Pinning CID...")
        run_ipfs(["pin", "add", cid])
        logger.info("CID pinned successfully")
    
    # 6. Publish to DHT
    logger.info("Publishing to DHT...")
    try:
        run_ipfs(["dht", "provide", cid], timeout=30)
    except Exception as e:
        logger.warning(f"DHT provide failed (not critical): {e}")
    
    return {
        'cid': cid,
        'normalized_path': str(normalized_dir),
        'cache_path': str(cache_path),
        'filestore_used': filestore_enabled or check_filestore_support()
    }

def load_from_ipfs(cid, cache_root="~/.cache/ipfs_models"):
    """Load model from IPFS CID with proper transformers support"""
    cache_dir = Path(cache_root).expanduser()
    model_dir = cache_dir / cid
    
    if not model_dir.exists():
        logger.info(f"Downloading {cid} from IPFS...")
        cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Download from IPFS
        run_ipfs(["get", cid, "-o", str(cache_dir)])
        
        # IPFS get creates a directory named with the CID
        downloaded_dir = cache_dir / cid
        if not downloaded_dir.exists():
            # Check if it was downloaded with a different structure
            possible_dirs = list(cache_dir.glob(f"{cid}*"))
            if possible_dirs:
                downloaded_dir = possible_dirs[0]
                if downloaded_dir != model_dir:
                    downloaded_dir.rename(model_dir)
        
        logger.info(f"Downloaded to {model_dir}")
    else:
        logger.info(f"Using cached model at {model_dir}")
    
    # Verify model files exist
    config_file = model_dir / "config.json"
    if not config_file.exists():
        raise FileNotFoundError(f"config.json not found in {model_dir}. Contents: {list(model_dir.iterdir())}")
    
    # Load with transformers (use local directory path)
    logger.info(f"Loading model from {model_dir}")
    from transformers import AutoModelForCausalLM, AutoTokenizer
    
    # Load tokenizer first (faster to test)
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
    
    # Load model
    model = AutoModelForCausalLM.from_pretrained(str(model_dir), local_files_only=True)
    
    return model, tokenizer

def load_from_normalized_path(normalized_path):
    """Load model directly from normalized path (no IPFS download needed)"""
    model_dir = Path(normalized_path)
    
    if not model_dir.exists():
        raise FileNotFoundError(f"Normalized path not found: {model_dir}")
    
    logger.info(f"Loading model from normalized path: {model_dir}")
    from transformers import AutoModelForCausalLM, AutoTokenizer
    
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir), local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(str(model_dir), local_files_only=True)
    
    return model, tokenizer

def load_from_hf_cache(repo_id, revision=None):
    """Load model directly from HF cache (original)"""
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tokenizer = AutoTokenizer.from_pretrained(repo_id, revision=revision)
    model = AutoModelForCausalLM.from_pretrained(repo_id, revision=revision)
    return model, tokenizer

def get_ipfs_id():
    """Get IPFS node ID and addresses"""
    try:
        id_info = run_ipfs(["id"])
        logger.info(f"IPFS Node Info:\n{id_info}")
        return id_info
    except Exception as e:
        logger.error(f"Failed to get IPFS ID: {e}")
        return None

def check_other_services_downloading():
    """Check if other services are actively downloading by monitoring file activity"""
    try:
        hf_cache = Path(os.environ.get("HF_HOME", "~/.cache/huggingface"))
        if not hf_cache.exists():
            return False
        
        current_time = time.time()
        recent_threshold = 30  # Files modified in last 30 seconds
        
        # Check for recently modified files (active downloads)
        for file_path in hf_cache.rglob("*"):
            if file_path.is_file():
                try:
                    mod_time = file_path.stat().st_mtime
                    if current_time - mod_time < recent_threshold:
                        logger.info(f"Recent file activity detected: {file_path.name}")
                        return True
                except (OSError, PermissionError):
                    continue
        
        # Check for HF-specific download indicators
        download_indicators = [
            "**/*.lock",           # HF lock files
            "**/tmp*",             # Temporary files
            "**/.tmp*",            # Hidden temp files
            "**/downloading*",     # Download state files
        ]
        
        for pattern in download_indicators:
            if list(hf_cache.glob(pattern)):
                logger.info(f"Download indicator found: {pattern}")
                return True
                
    except Exception as e:
        logger.debug(f"Error checking file activity: {e}")
    
    return False

def serve_all_cached_models():
    """Pin and serve all models found in HF cache"""
    hf_cache = Path(os.environ.get("HF_HOME", "~/.cache/huggingface"))
    hub_cache = hf_cache / "hub"
    
    if not hub_cache.exists():
        logger.warning("No HF hub cache found")
        return {}
    
    model_cids = {}
    
    # Find all model snapshots
    for model_dir in hub_cache.glob("models--*"):
        try:
            repo_name = model_dir.name.replace("models--", "").replace("--", "/")
            snapshots_dir = model_dir / "snapshots"
            
            if not snapshots_dir.exists():
                continue
                
            # Get the latest snapshot (most recent folder)
            snapshots = [d for d in snapshots_dir.iterdir() if d.is_dir()]
            if not snapshots:
                continue
                
            latest_snapshot = max(snapshots, key=lambda x: x.stat().st_mtime)
            revision = latest_snapshot.name[:8]
            
            logger.info(f"Pinning cached model: {repo_name}@{revision}")
            result = pin_hf_commit(repo_name, revision, pin=True)
            model_cids[f"{repo_name}@{revision}"] = result['cid']
            
        except Exception as e:
            logger.warning(f"Failed to pin {model_dir.name}: {e}")
            continue
    
    return model_cids

def main():
    parser = argparse.ArgumentParser(description='Convert HF models to IPFS CIDs with filestore')
    parser.add_argument('--repo-id', default='Qwen/Qwen2.5-0.5B', help='HuggingFace repo ID')
    parser.add_argument('--revision', default='main', help='Git revision/commit hash')
    parser.add_argument('--no-pin', action='store_true', help='Skip pinning')
    parser.add_argument('--test-load', action='store_true', help='Test loading after pinning')
    parser.add_argument('--cache-dir', help='HF cache directory')
    parser.add_argument('--load-from', choices=['ipfs', 'local', 'original'], 
                       default='local', help='Where to load model from for testing')
    parser.add_argument('--serve-all', action='store_true', help='Serve all cached models')
    parser.add_argument('--wait-if-busy', action='store_true', help='Wait if other services downloading')
        
    args = parser.parse_args()
    
    # Handle serve-all mode
    if args.serve_all:
        try:
            wait_for_ipfs_daemon()
            model_cids = serve_all_cached_models()
            
            print("🎉 All cached models served!")
            for model, cid in model_cids.items():
                print(f"📦 {model}: {cid}")
            
            return 0
        except Exception as e:
            logger.error(f"Failed to serve all models: {e}")
            return 1
    
    # Existing main logic with wait-if-busy check
    try:
        wait_for_ipfs_daemon()
        
        # Check if model exists in cache first
        hf_cache = Path(os.environ.get("HF_HOME", "~/.cache/huggingface"))
        model_cache = hf_cache / "hub" / f"models--{args.repo_id.replace('/', '--')}"
        
        if not model_cache.exists() and args.wait_if_busy:
            logger.info("Model not cached, checking for other services...")
            if check_other_services_downloading():
                logger.info("Waiting 60 seconds for other services...")
                time.sleep(60)
                if check_other_services_downloading():
                    logger.info("Other services still busy, skipping download")
                    return 0
        
        # Show IPFS node info (but handle the lock error gracefully)
        try:
            get_ipfs_id()
        except:
            logger.warning("Could not get IPFS ID (daemon starting up)")
        
        # Pin the model
        result = pin_hf_commit(
            args.repo_id, 
            args.revision, 
            pin=not args.no_pin,
            hf_cache_home=args.cache_dir
        )
        
        cid = result['cid']
        print(f"🎉 SUCCESS! CID: {cid}")
        print(f"🌐 Check on IPFS: https://ipfs.io/ipfs/{cid}")
        print(f"📋 Gateway URLs:")
        print(f"   https://gateway.pinata.cloud/ipfs/{cid}")
        print(f"   https://cloudflare-ipfs.com/ipfs/{cid}")
        
        # Show storage info
        if result['filestore_used']:
            print("💾 Storage: Filestore mode (references only, no duplication)")
        else:
            print("⚠️  Storage: Copy mode (data duplicated in IPFS)")
        
        print(f"📁 Normalized path: {result['normalized_path']}")
        print(f"📦 Original cache: {result['cache_path']}")
        
        # Test loading if requested
        if args.test_load:
            logger.info(f"Testing model loading from {args.load_from}...")
            
            try:
                if args.load_from == 'ipfs':
                    model, tokenizer = load_from_ipfs(cid)
                    logger.info("✅ Model loaded from IPFS successfully!")
                elif args.load_from == 'local':
                    model, tokenizer = load_from_normalized_path(result['normalized_path'])
                    logger.info("✅ Model loaded from normalized path successfully!")
                else:  # original
                    model, tokenizer = load_from_hf_cache(args.repo_id, args.revision)
                    logger.info("✅ Model loaded from original cache successfully!")
                
                # Quick test
                inputs = tokenizer("Hello", return_tensors="pt")
                try:
                    import torch
                    with torch.no_grad():
                        outputs = model.generate(**inputs, max_length=10, do_sample=False)
                except ImportError:
                    # If torch not available, just test tokenizer
                    outputs = model.generate(**inputs, max_length=10, do_sample=False)
                
                response = tokenizer.decode(outputs[0], skip_special_tokens=True)
                print(f"🤖 Test output: {response}")
                
            except Exception as e:
                logger.error(f"Model loading failed: {e}")
                print("⚠️  Model pinned successfully but loading test failed")
        
    except Exception as e:
        logger.error(f"Failed: {e}")
        return 1
    
    return 0

if __name__ == "__main__":
    exit(main())
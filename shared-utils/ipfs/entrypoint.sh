#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
set -e

init_ipfs() {
    if [ ! -f "$IPFS_PATH/config" ]; then
        # Debug IPFS binary
        echo "🔍 IPFS Binary Debug:"
        echo "   Architecture: $(uname -m)"
        echo "   IPFS binary: $(file /usr/local/bin/ipfs)"
        echo "   IPFS version check:"
        /usr/local/bin/ipfs version || echo "   ❌ IPFS version failed"

        echo "📁 Initializing IPFS repository..."
        ipfs init --profile=lowpower
        
        # Configure for external access and low resource usage
        ipfs config Addresses.API /ip4/0.0.0.0/tcp/5001
        ipfs config Addresses.Gateway /ip4/0.0.0.0/tcp/8080
        ipfs config --json Addresses.Swarm '["/ip4/0.0.0.0/tcp/4001", "/ip6/::/tcp/4001"]'
        
        # Low resource optimizations
        ipfs config --json Datastore.StorageMax '"100GB"'
        ipfs config --json Reprovider.Interval '"12h"'
        ipfs config --json Swarm.ConnMgr.LowWater 50
        ipfs config --json Swarm.ConnMgr.HighWater 100
        ipfs config --json Routing.Type '"dhtclient"'
        ipfs config --json Routing.AcceleratedDHTClient true
        ipfs config --json Reprovider.Strategy '"roots"'
        ipfs config --json Reprovider.Interval '"24h"' 
        ipfs config --json Swarm.Transports.Network.Relay false
        
        # Reduce DHT query concurrency to prevent overwhelming
        ipfs config --json Routing.Type '"dht"'  # Use full DHT, not just client        
        echo "✅ IPFS initialized with low-resource profile"
    fi
}

check_models_mount() {
    if [ -d "/models" ] && [ -w "/models" ]; then
        echo "📦 Using mounted models directory: /models"
        echo "   HF_HOME: $HF_HOME"
        echo "   Available space: $(df -h /models | awk 'NR==2 {print $4}')"
        
        # Create subdirectories if needed
        mkdir -p /models/transformers /models/datasets /models/hub
        
        # Check if there's existing cache
        if [ -d "/models/hub" ] && [ "$(ls -A /models/hub)" ]; then
            echo "   Existing cache found: $(ls /models/hub | wc -l) repositories"
        fi
    else
        echo "⚠️  Models directory not mounted or not writable, using default cache"
    fi
}

start_daemon_background() {
    echo "🚀 Starting IPFS daemon in background..."
    ipfs daemon --enable-gc --routing=dhtclient &
    DAEMON_PID=$!
    
    # Wait for daemon to be ready
    echo "⏳ Waiting for IPFS daemon to be ready..."
    timeout 60 bash -c 'until ipfs id >/dev/null 2>&1; do sleep 2; done'
    echo "✅ IPFS daemon is ready (PID: $DAEMON_PID)"
}

cleanup() {
    echo "🛑 Shutting down..."
    if [ ! -z "$DAEMON_PID" ]; then
        kill $DAEMON_PID 2>/dev/null || true
        wait $DAEMON_PID 2>/dev/null || true
    fi
    exit 0
}

configure_for_tor() {
    echo "🔒 Configuring IPFS for Tor usage..."
    
    # Disable local discovery to prevent IP leakage
    ipfs config --json Discovery.MDNS.Enabled false
    
    # Only use websocket transports (work better with proxies)
    ipfs config --json Addresses.Swarm '[
        "/ip4/0.0.0.0/tcp/4001/ws",
        "/ip6/::/tcp/4001/ws"
    ]'
    
    # Disable DHT client mode to reduce fingerprinting
    ipfs config --json Routing.Type '"dhtclient"'
    
    # Reduce connection counts for Tor performance
    ipfs config --json Swarm.ConnMgr.LowWater 10
    ipfs config --json Swarm.ConnMgr.HighWater 20
    
    # Disable relay discovery to prevent IP leakage
    ipfs config --json Swarm.RelayClient.Enabled false
    
    # Use websocket-only bootstrap nodes
    ipfs config --json Bootstrap '[
        "/dns4/ams-1.bootstrap.libp2p.io/tcp/80/ws/p2p/QmSoLer265NRgSp2LA3dPaeykiS1J6DifTC88f5uVQKNAd"
    ]'
    
    # Disable announcing to reduce DHT presence
    ipfs config --json Reprovider.Strategy '"manual"'
    
    echo "✅ Tor-friendly configuration applied"
}

trap cleanup SIGTERM SIGINT

echo "🐳 Starting IPFS Model Server Container..."

# Always check models mount status
check_models_mount

case "$1" in
    serve-all-cached)
        echo "📚 Serve all cached models mode..."
        
        # Initialize and start daemon
        init_ipfs
        start_daemon_background
        
        # Serve all existing models first
        echo "🔍 Discovering and serving cached models..."
        python3 /app/hf_to_ipfs.py --serve-all
        
        # Then handle the specified model if set
        if [ ! -z "$REPO_ID" ]; then
            echo "📦 Processing specified model: ${REPO_ID}@${REVISION:-main}..."
            python3 /app/hf_to_ipfs.py \
                --repo-id "$REPO_ID" \
                --revision "${REVISION:-main}" \
                --wait-if-busy \
                --load-from "${LOAD_FROM:-local}"
        fi
        
        # Show summary
        echo "📊 Service summary:"
        if [ -d "/models/hub" ]; then
            echo "   Available models: $(find /models/hub -name "models--*" -type d | wc -l)"
            echo "   Pinned models: $(find /models/ipfs_normalized -type d -maxdepth 1 2>/dev/null | wc -l)"
        fi
        
        # Keep daemon running
        echo "🔄 All models served, keeping daemon running..."
        wait $DAEMON_PID
        ;;
        

    pin-and-serve)
        echo "📌 Pin and serve mode..."
        
        # Initialize and start daemon
        init_ipfs
        start_daemon_background
        
        # Pin the model
        REPO_ID="${REPO_ID:-Qwen/Qwen2.5-0.5B}"
        REVISION="${REVISION:-main}"
        LOAD_FROM="${LOAD_FROM:-local}"  # local, ipfs, original
        
        echo "📦 Pinning ${REPO_ID}@${REVISION}..."
        python3 /app/hf_to_ipfs.py \
            --repo-id "$REPO_ID" \
            --revision "$REVISION" \
            --load-from "$LOAD_FROM" 
        
        # Show cache info
        if [ -d "/models/hub" ]; then
            echo "📊 Cache statistics:"
            echo "   Total repositories: $(find /models/hub -name "snapshots" -type d | wc -l)"
            echo "   Cache size: $(du -sh /models 2>/dev/null | cut -f1)"
            echo "   Normalized models: $(find /models/ipfs_normalized -type d -maxdepth 1 2>/dev/null | wc -l)"
        fi
        
        # Keep daemon running
        echo "🔄 Keeping daemon running... (Ctrl+C to stop)"
        wait $DAEMON_PID
        ;;
        
    daemon-only)
        echo "📡 Daemon only mode..."
        init_ipfs
        echo "🌐 Starting IPFS daemon..."
        exec ipfs daemon --enable-gc --routing=dhtclient
        ;;
        
    pin-only)
        echo "📌 Pin only mode (requires external daemon)..."
        timeout 60 bash -c 'until ipfs version >/dev/null 2>&1; do sleep 2; done'
        
        REPO_ID="${REPO_ID:-Qwen/Qwen2.5-0.5B}"
        REVISION="${REVISION:-main}"
        LOAD_FROM="${LOAD_FROM:-local}"
        
        echo "📦 Pinning ${REPO_ID}@${REVISION}..."
        python3 /app/hf_to_ipfs.py \
            --repo-id "$REPO_ID" \
            --revision "$REVISION" \
            --load-from "$LOAD_FROM" \
            --test-load
        ;;

    tor-mode)
        echo "🔒 Tor privacy mode..."
        
        init_ipfs
        configure_for_tor
        start_daemon_background
        
        # Only pin specific models (don't auto-discover)
        if [ ! -z "$REPO_ID" ]; then
            python3 /app/hf_to_ipfs.py \
                --repo-id "$REPO_ID" \
                --revision "${REVISION:-main}" \
                --wait-if-busy \
                --load-from "${LOAD_FROM:-local}" \
                --no-pin  # Skip DHT announcements
        fi
        
        echo "🔒 Running in Tor mode (reduced DHT participation)"
        wait $DAEMON_PID
        ;;

    test)
        echo "🧪 Test mode (no daemon persistence)..."
        init_ipfs
        start_daemon_background
        
        REPO_ID="${REPO_ID:-Qwen/Qwen2.5-0.5B}"
        REVISION="${REVISION:-main}"
        LOAD_FROM="${LOAD_FROM:-local}"  # local, ipfs, original
        
        python3 /app/hf_to_ipfs.py \
            --repo-id "$REPO_ID" \
            --revision "$REVISION" \
            --load-from "$LOAD_FROM" \
            --test-load
        
        cleanup
        ;;
        
    shell)
        echo "🐚 Shell mode..."
        init_ipfs
        exec /bin/bash
        ;;
        
    *)
        echo "Usage: $0 {pin-and-serve|daemon-only|pin-only|test|shell}"
        echo ""
        echo "Modes:"
        echo "  pin-and-serve  - Pin model and keep daemon running (DEFAULT)"
        echo "  daemon-only    - Start IPFS daemon only"  
        echo "  pin-only       - Pin model to existing daemon"
        echo "  test           - Pin model then exit"
        echo "  shell          - Interactive shell"
        echo "  serve-all-cached - Serve all cached models + optional new model"        
        echo ""
        echo "Environment variables:"
        echo "  REPO_ID      - HuggingFace repo (default: Qwen/Qwen2.5-0.5B)"
        echo "  REVISION     - Git revision (default: main)"
        echo "  LOAD_FROM    - Test loading from: local|ipfs|original (default: local)"
        echo ""
        echo "Volume mounts:"
        echo "  /models   - HuggingFace cache directory (recommended)"
        echo "  /data/ipfs - IPFS data directory"
        echo ""
        echo "Features:"
        echo "  - Uses IPFS filestore (references only, no data duplication)"
        echo "  - Creates hardlinked normalized directories"
        echo "  - Preserves original HF cache for compatibility"
        echo "  - Generates deterministic CIDs across hosts"
        exit 1
        ;;
esac
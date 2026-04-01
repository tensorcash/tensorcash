# Production Deployment Guide - Cosign Bridge

## Overview

This guide describes how to deploy and operate the TensorCash Cosign Bridge in production. It covers building, installation, service supervision (systemd/launchd/Docker), configuration, monitoring, security hardening, and routine operational procedures.

The bridge is a stdio-driven helper process: TensorCash bcore (the node/Qt wallet) launches it as a child and exchanges newline-delimited JSON requests and responses over the bridge's stdin/stdout. It performs the SPAKE2 password-authenticated key exchange, the Noise (`NNpsk0`) session handshake, and the Short Authentication String (SAS) confirmation that underpin the cosigning transport. It does not hold consensus state and stores no persistent secrets.

The crate ships with a full test suite (221 unit tests plus 76 integration tests, 297 in total) run by `cargo test`.

---

## Build Instructions

### Production Build

**Environment:**
- Rust: 1.85 (stable)
- OS: Linux (Ubuntu 22.04 LTS recommended)
- Architecture: x86_64 or aarch64

**Build Command:**
```bash
# Clean previous builds
cargo clean

# Production build with optimizations
cargo build --release

# Verify binary
./target/release/cosign-bridge --version

# Strip debug symbols (optional, reduces binary size)
strip target/release/cosign-bridge

# Verify binary size
ls -lh target/release/cosign-bridge
```

**Expected Output:**
- Binary size: ~5-10 MB (without debug symbols)
- No warnings during compilation
- All optimizations enabled

### Cross-Platform Builds

**Linux (Ubuntu 22.04):**
```bash
cargo build --release --target x86_64-unknown-linux-gnu
```

**macOS:**
```bash
cargo build --release --target x86_64-apple-darwin
cargo build --release --target aarch64-apple-darwin  # M1/M2 Macs
```

**Windows (if supported):**
```bash
cargo build --release --target x86_64-pc-windows-msvc
```

### Docker Build (Reproducible)

```bash
# Build in Docker for reproducibility
docker run --rm -v $(pwd):/workspace \
  -w /workspace/services/core-node/cosign-bridge \
  rust:1.85 \
  cargo build --release

# Extract binary
cp target/release/cosign-bridge ./cosign-bridge-linux-x86_64
```

### Build Verification

```bash
# Run tests on production build
cargo test --release

# Verify no test mode enabled
strings target/release/cosign-bridge | grep "TEST MODE" && \
  echo "ERROR: Test mode found in production build!" || \
  echo "OK: No test mode in production build"

# Check binary dependencies
ldd target/release/cosign-bridge  # Linux
otool -L target/release/cosign-bridge  # macOS

# Verify static linking (if required)
file target/release/cosign-bridge
```

---

## Installation

### System Requirements

**Minimum:**
- CPU: 1 core, 1 GHz
- RAM: 512 MB
- Disk: 100 MB free space
- OS: Linux 4.4+, macOS 10.14+, Windows 10+ (if supported)

**Recommended:**
- CPU: 2+ cores, 2+ GHz
- RAM: 1 GB+
- Disk: 500 MB free space (for logs)
- Network: Stable internet connection (if using WebSocket relay)

### Installation Steps

**Linux (Ubuntu/Debian):**
```bash
# Install binary
sudo cp target/release/cosign-bridge /usr/local/bin/
sudo chmod +x /usr/local/bin/cosign-bridge

# Verify installation
cosign-bridge --version

# (Optional) Install systemd service
sudo cp deployment/cosign-bridge.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable cosign-bridge
```

**macOS:**
```bash
# Install binary
sudo cp target/release/cosign-bridge /usr/local/bin/
sudo chmod +x /usr/local/bin/cosign-bridge

# Verify installation
cosign-bridge --version

# (Optional) Install launchd service
sudo cp deployment/com.tensorcash.cosign-bridge.plist /Library/LaunchDaemons/
sudo launchctl load /Library/LaunchDaemons/com.tensorcash.cosign-bridge.plist
```

**Windows:**
```powershell
# Copy binary to Program Files
Copy-Item target\release\cosign-bridge.exe "C:\Program Files\TensorCash\"

# Verify installation
& "C:\Program Files\TensorCash\cosign-bridge.exe" --version

# (Optional) Install Windows service
# Use NSSM or similar service wrapper
```

---

## Configuration

### Environment Variables

**Logging:**
```bash
# Production logging (info level)
export RUST_LOG=info

# Debug logging (for troubleshooting)
export RUST_LOG=debug

# Quiet logging (errors only)
export RUST_LOG=error
```

**Runtime Settings:**
```bash
# (Optional) WebSocket relay URL
export COSIGN_RELAY_URL="wss://relay.tensorcash.org"

# (Optional) Custom session storage path
export COSIGN_SESSION_PATH="/var/lib/cosign-bridge/sessions"
```

### Command-Line Flags

**Production Mode (Default):**
```bash
# No flags = production mode (secure)
./cosign-bridge
```

**Test Mode (NEVER USE IN PRODUCTION):**
```bash
# Test mode with default seed
./cosign-bridge --test-mode

# Test mode with custom seed
./cosign-bridge --test-mode \
  --test-seed=4242424242424242424242424242424242424242424242424242424242424242

# Test mode with fixed timestamp
./cosign-bridge --test-mode \
  --test-seed=4242424242424242424242424242424242424242424242424242424242424242 \
  --test-time=1609459200000
```

**CRITICAL WARNING:**
- **NEVER** use `--test-mode` in production
- Test mode compromises all security (deterministic randomness)
- Only use test mode in development/testing environments
- The binary accepts `--test-mode` at runtime regardless of build profile, so it must be excluded by deployment policy (never present in the service `ExecStart`)

### Configuration Files

**Session Storage:**
- Location: `~/.cosign-bridge/sessions/` (default) or `$COSIGN_SESSION_PATH`
- Format: JSON files (one per session)
- Permissions: 600 (read/write owner only)
- Backup: Not required (sessions are ephemeral)

**Logs:**
- Location: stdout/stderr (captured by systemd/launchd)
- Format: Human-readable text with timestamps
- Rotation: Handled by systemd/launchd/syslog
- Retention: 7-30 days recommended

---

## Running the Bridge

### Standalone Mode

```bash
# Run in foreground (for testing)
RUST_LOG=info ./cosign-bridge

# Run in background (using nohup)
nohup RUST_LOG=info ./cosign-bridge > /var/log/cosign-bridge.log 2>&1 &

# Check process
ps aux | grep cosign-bridge
```

### Systemd Service (Linux)

**Service File:** `/etc/systemd/system/cosign-bridge.service`
```ini
[Unit]
Description=TensorCash Cosign Bridge
After=network.target

[Service]
Type=simple
User=tensorcash
Group=tensorcash
Environment="RUST_LOG=info"
ExecStart=/usr/local/bin/cosign-bridge
Restart=on-failure
RestartSec=10s

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/cosign-bridge

[Install]
WantedBy=multi-user.target
```

**Service Management:**
```bash
# Start service
sudo systemctl start cosign-bridge

# Enable auto-start on boot
sudo systemctl enable cosign-bridge

# Check status
sudo systemctl status cosign-bridge

# View logs
sudo journalctl -u cosign-bridge -f

# Restart service
sudo systemctl restart cosign-bridge

# Stop service
sudo systemctl stop cosign-bridge
```

### Launchd Service (macOS)

**Plist File:** `/Library/LaunchDaemons/com.tensorcash.cosign-bridge.plist`
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.tensorcash.cosign-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/local/bin/cosign-bridge</string>
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>RUST_LOG</key>
        <string>info</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/var/log/cosign-bridge.log</string>
    <key>StandardErrorPath</key>
    <string>/var/log/cosign-bridge.error.log</string>
</dict>
</plist>
```

**Service Management:**
```bash
# Load service
sudo launchctl load /Library/LaunchDaemons/com.tensorcash.cosign-bridge.plist

# Unload service
sudo launchctl unload /Library/LaunchDaemons/com.tensorcash.cosign-bridge.plist

# Start service
sudo launchctl start com.tensorcash.cosign-bridge

# Stop service
sudo launchctl stop com.tensorcash.cosign-bridge

# View logs
tail -f /var/log/cosign-bridge.log
```

### Docker Container (Optional)

**Dockerfile:**
```dockerfile
FROM rust:1.85 as builder
WORKDIR /workspace
COPY . .
RUN cargo build --release

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /workspace/target/release/cosign-bridge /usr/local/bin/
ENV RUST_LOG=info
ENTRYPOINT ["/usr/local/bin/cosign-bridge"]
```

**Docker Commands:**
```bash
# Build image
docker build -t tensorcash/cosign-bridge:1.0.0 .

# Run container
docker run -d \
  --name cosign-bridge \
  --restart unless-stopped \
  -v /var/lib/cosign-bridge:/root/.cosign-bridge \
  -e RUST_LOG=info \
  tensorcash/cosign-bridge:1.0.0

# View logs
docker logs -f cosign-bridge

# Stop container
docker stop cosign-bridge

# Remove container
docker rm cosign-bridge
```

---

## Monitoring

### Health Checks

The bridge speaks newline-delimited JSON over stdio. A request is `{"command":"<name>","params":{...}}` with a **bare** command name (no `cosign.` prefix). Each response is a flat JSON object: the handler's result fields appear at the top level, alongside an optional top-level `error` string when the command fails. There is no JSON-RPC `result`/`error.code` envelope, and no wrapping `data` key.

**Ping Check:**
```bash
# Check if bridge is responsive
echo '{"command":"ping","params":{}}' | ./cosign-bridge
```

**Expected Response:**
```json
{"bridge_alive":true,"version":"0.1.0","transports":["ws"],"uptime_sec":86400,"capabilities":["resume","send_multi","bip322"]}
```

**Automated Health Check Script:**
```bash
#!/bin/bash
# health-check.sh

RESPONSE=$(echo '{"command":"ping","params":{}}' | timeout 5s ./cosign-bridge 2>/dev/null)

if echo "$RESPONSE" | grep -q '"bridge_alive":true'; then
    echo "OK: Bridge is healthy"
    exit 0
else
    echo "ERROR: Bridge is not responding"
    exit 1
fi
```

### Metrics Collection

**Available Metrics:**
```bash
echo '{"command":"metrics","params":{}}' | ./cosign-bridge
```

**Response:**
```json
{
  "active_sessions": 5,
  "total_messages": 0,
  "bridge_restarts": 0,
  "transport_failures": { "ws": 0 },
  "avg_latency_ms": 42,
  "p95_latency_ms": 85,
  "p99_latency_ms": 150
}
```

### Log Monitoring

**Important Log Patterns:**

**Normal Operation:**
```
[INFO] Session initiated: session_123
[INFO] Handshake completed for session_123
[INFO] Message sent on session_123 (1234 bytes)
[INFO] Session closed: session_123
```

**Warnings:**
```
[WARN] Rate limit hit for session_123
[WARN] Bandwidth limit hit for session_123
[WARN] Session session_123 idle for 5 minutes
```

**Errors:**
```
[ERROR] Handshake failed for session_123: invalid message
[ERROR] Decryption failed for session_123
[ERROR] WebSocket connection failed: connection refused
```

**Security Alerts:**
```
[WARN] ⚠️  TEST MODE ENABLED - NOT FOR PRODUCTION
[ERROR] SPAKE2 verification failed for session_123
[ERROR] Invalid invite code attempted: alpha-bravo-***
```

**Log Monitoring Script:**
```bash
#!/bin/bash
# monitor-logs.sh

tail -f /var/log/cosign-bridge.log | while read line; do
    if echo "$line" | grep -q "ERROR"; then
        echo "[ALERT] Error detected: $line"
        # Send alert (email, Slack, PagerDuty, etc.)
    fi

    if echo "$line" | grep -q "TEST MODE"; then
        echo "[CRITICAL] Test mode enabled in production!"
        # Send critical alert
    fi
done
```

---

## Security Hardening

### System-Level Security

**File Permissions:**
```bash
# Binary permissions (executable by all, writable only by root)
sudo chown root:root /usr/local/bin/cosign-bridge
sudo chmod 755 /usr/local/bin/cosign-bridge

# Session storage permissions (read/write by bridge user only)
sudo chown -R tensorcash:tensorcash /var/lib/cosign-bridge
sudo chmod 700 /var/lib/cosign-bridge
sudo chmod 600 /var/lib/cosign-bridge/sessions/*
```

**SELinux/AppArmor:**
```bash
# SELinux policy (example)
sudo semanage fcontext -a -t bin_t /usr/local/bin/cosign-bridge
sudo restorecon -v /usr/local/bin/cosign-bridge

# AppArmor profile (example)
# /etc/apparmor.d/usr.local.bin.cosign-bridge
# (To be created based on specific deployment requirements)
```

**Firewall Rules:**
```bash
# If using WebSocket relay, allow outbound HTTPS
sudo ufw allow out 443/tcp comment "Cosign Bridge WebSocket"

# Block all other outbound connections (if not needed)
sudo ufw default deny outgoing
sudo ufw default deny incoming
```

### Application-Level Security

**Runtime Checks:**
- [ ] `--test-mode` flag absent from all service definitions and launch scripts
- [ ] All inputs validated and sanitized
- [ ] No plaintext secrets in logs
- [ ] No sensitive data in error messages
- [ ] Rate limiting enabled and enforced
- [ ] Bandwidth limiting enabled and enforced

**Secret Management:**
- Passwords: Never logged, never stored
- Session keys: Zeroized on drop
- Handshake messages: Can be logged (not secret)
- Encrypted payloads: Opaque to bridge
- Invite codes: Ephemeral (not persisted)

**Network Security:**
- WebSocket connections use TLS (wss://)
- Certificate validation enabled
- No self-signed certificates accepted
- No downgrade to unencrypted connections

---

## Operational Procedures

### Startup Procedure

1. **Verify Environment:**
```bash
# Check Rust version
rustc --version  # Should be 1.85+

# Check system resources
free -h  # Check available memory
df -h    # Check available disk space

# Check network connectivity (if using WebSocket relay)
ping -c 1 relay.tensorcash.org
```

2. **Start Bridge:**
```bash
# Production mode (no test flags)
sudo systemctl start cosign-bridge

# Verify startup
sudo systemctl status cosign-bridge

# Check initial logs
sudo journalctl -u cosign-bridge -n 50
```

3. **Verify Health:**
```bash
# Ping check
./health-check.sh

# Metrics check
echo '{"command":"metrics","params":{}}' | cosign-bridge
```

4. **Test Integration:**
```bash
# Test version check
echo '{"command":"version","params":{}}' | cosign-bridge

# Test session init
echo '{"command":"init","params":{}}' | cosign-bridge
```

### Shutdown Procedure

1. **Graceful Shutdown:**
```bash
# Wait for active sessions to complete before stopping
watch -n 1 'echo "{\"command\":\"metrics\",\"params\":{}}" | cosign-bridge | jq .active_sessions'

# Stop service
sudo systemctl stop cosign-bridge
```

2. **Verify Shutdown:**
```bash
# Check process is stopped
ps aux | grep cosign-bridge

# Check logs for clean shutdown
sudo journalctl -u cosign-bridge -n 20
```

### Restart Procedure

1. **Planned Restart:**
```bash
# Restart systemd service (automatically handles graceful shutdown)
sudo systemctl restart cosign-bridge

# Verify restart
sudo systemctl status cosign-bridge
```

2. **Emergency Restart:**
```bash
# Force kill process
sudo systemctl kill -s SIGKILL cosign-bridge

# Start service
sudo systemctl start cosign-bridge

# Check for errors
sudo journalctl -u cosign-bridge -n 50
```

### Backup Procedure

**What to Backup:**
- Bridge binary: `/usr/local/bin/cosign-bridge`
- Configuration: Environment variables, systemd service file
- Logs: `/var/log/cosign-bridge.log` (for troubleshooting)

**What NOT to Backup:**
- Session storage (ephemeral, not needed)
- Temporary files

**Backup Script:**
```bash
#!/bin/bash
# backup-cosign-bridge.sh

BACKUP_DIR="/backup/cosign-bridge/$(date +%Y%m%d)"
mkdir -p "$BACKUP_DIR"

# Backup binary
cp /usr/local/bin/cosign-bridge "$BACKUP_DIR/"

# Backup service file
cp /etc/systemd/system/cosign-bridge.service "$BACKUP_DIR/"

# Backup logs (last 7 days)
journalctl -u cosign-bridge --since="7 days ago" > "$BACKUP_DIR/logs.txt"

echo "Backup completed: $BACKUP_DIR"
```

### Update Procedure

1. **Pre-Update Checks:**
```bash
# Backup current version
./backup-cosign-bridge.sh

# Run current tests
cargo test --quiet

# Review changelog
git log v1.0.0..v1.1.0
```

2. **Update:**
```bash
# Stop service
sudo systemctl stop cosign-bridge

# Build new version
git checkout v1.1.0
cargo build --release

# Run tests on new version
cargo test --release --quiet

# Install new binary
sudo cp target/release/cosign-bridge /usr/local/bin/
sudo chmod 755 /usr/local/bin/cosign-bridge

# Verify new version
/usr/local/bin/cosign-bridge --version
```

3. **Post-Update Checks:**
```bash
# Start service
sudo systemctl start cosign-bridge

# Verify health
./health-check.sh

# Monitor logs
sudo journalctl -u cosign-bridge -f

# Test basic operations
echo '{"command":"ping","params":{}}' | cosign-bridge
```

4. **Rollback (if needed):**
```bash
# Stop service
sudo systemctl stop cosign-bridge

# Restore previous binary from backup
sudo cp /backup/cosign-bridge/<YYYYMMDD>/cosign-bridge /usr/local/bin/

# Start service
sudo systemctl start cosign-bridge

# Verify health
./health-check.sh
```

---

## Troubleshooting

### Common Issues

**1. Bridge Not Starting**

**Symptoms:**
- Systemd service fails to start
- Process crashes immediately

**Diagnosis:**
```bash
# Check service status
sudo systemctl status cosign-bridge

# Check logs
sudo journalctl -u cosign-bridge -n 50

# Try manual start for detailed errors
RUST_LOG=debug /usr/local/bin/cosign-bridge
```

**Solutions:**
- Check binary permissions (should be executable)
- Check binary path in service file
- Check for missing dependencies: `ldd /usr/local/bin/cosign-bridge`
- Check for port conflicts (if applicable)
- Check disk space: `df -h`

**2. Session Handshake Failures**

**Symptoms:**
- Handshake step returns error
- "Invalid message" errors in logs

**Diagnosis:**
```bash
# Enable debug logging
RUST_LOG=debug /usr/local/bin/cosign-bridge

# Check for handshake errors
sudo journalctl -u cosign-bridge | grep "handshake"

# Check for SPAKE2 errors
sudo journalctl -u cosign-bridge | grep "SPAKE2"
```

**Solutions:**
- Verify both parties using same invite code
- Verify both parties using compatible versions
- Verify handshake messages not corrupted during exchange
- Verify no man-in-the-middle attack (SAS verification)

**3. High Memory Usage**

**Symptoms:**
- Process using excessive memory
- OOM killer terminates process

**Diagnosis:**
```bash
# Check memory usage
ps aux | grep cosign-bridge

# Check active sessions
echo '{"command":"metrics","params":{}}' | cosign-bridge

# Monitor memory over time
watch -n 1 'ps aux | grep cosign-bridge'
```

**Solutions:**
- Close unused sessions: send the `close` command
- Check for memory leaks (run tests with valgrind)
- Increase system memory if needed
- Restart service to clear leaked memory

**4. Rate Limit or Bandwidth Limit Errors**

**Symptoms:**
- "Rate limit exceeded" errors
- "Bandwidth limit exceeded" errors

**Diagnosis:**
```bash
# Check metrics for limit hits
echo '{"command":"metrics","params":{}}' | cosign-bridge

# Check logs for limit errors
sudo journalctl -u cosign-bridge | grep "limit"
```

**Solutions:**
- Wait and retry (limits reset over time)
- Reduce request rate in client code
- Split large messages into smaller chunks
- Adjust limits in configuration (if appropriate)

**5. WebSocket Connection Failures**

**Symptoms:**
- "WebSocket connection failed" errors
- Messages not being delivered

**Diagnosis:**
```bash
# Check network connectivity
ping relay.tensorcash.org

# Check TLS certificate
openssl s_client -connect relay.tensorcash.org:443

# Check logs for WebSocket errors
sudo journalctl -u cosign-bridge | grep -i websocket
```

**Solutions:**
- Verify relay URL is correct
- Verify network connectivity
- Verify firewall allows outbound HTTPS (port 443)
- Check relay server status
- Fall back to manual message exchange (QR codes)

### Debug Mode

**Enable Debug Logging:**
```bash
# Temporary (current session)
RUST_LOG=debug /usr/local/bin/cosign-bridge

# Permanent (systemd service)
sudo systemctl edit cosign-bridge
# Add: Environment="RUST_LOG=debug"
sudo systemctl restart cosign-bridge
```

**Trace-Level Logging (Very Verbose):**
```bash
RUST_LOG=trace /usr/local/bin/cosign-bridge
```

**Module-Specific Logging:**
```bash
# Only crypto module
RUST_LOG=cosign_bridge::crypto=debug /usr/local/bin/cosign-bridge

# Only session module
RUST_LOG=cosign_bridge::session=debug /usr/local/bin/cosign-bridge

# Only transport module
RUST_LOG=cosign_bridge::transport=debug /usr/local/bin/cosign-bridge
```

### Support Escalation

**When to Escalate:**
- Security vulnerabilities discovered
- Critical bugs affecting production
- Data corruption or loss
- Repeated crashes or instability

**Escalation Procedure:**
1. Gather diagnostic information (logs, metrics, reproduction steps)
2. Create detailed bug report with version info
3. Contact: support@tensorcash.org
4. For security issues: security@tensorcash.org (private)

---

## Production Safety Rules

### NEVER Do This in Production

1. **NEVER use `--test-mode` flag**
   - Test mode compromises all security
   - Only use in development/testing environments

2. **NEVER disable rate limiting or bandwidth limiting**
   - Protects against DoS attacks
   - Essential for resource management

3. **NEVER skip SAS verification**
   - SAS is critical for MITM detection
   - User MUST verify SAS matches

4. **NEVER log sensitive data**
   - No passwords in logs
   - No plaintext messages in logs
   - No private keys in logs

5. **NEVER expose bridge to untrusted input**
   - Only TensorCash bcore should communicate with the bridge over stdio
   - No external network access to stdio interface

6. **NEVER run as root (unless necessary)**
   - Use dedicated user account (e.g., `tensorcash`)
   - Drop privileges after startup if elevated needed

7. **NEVER reuse session IDs**
   - Each session must have unique ID
   - No session ID reuse across restarts

8. **NEVER share invite codes insecurely**
   - Use encrypted channels (Signal, WhatsApp, etc.)
   - Or in-person exchange

9. **NEVER ignore security warnings**
   - "TEST MODE ENABLED" → Terminate immediately
   - "SAS mismatch" → Terminate session, investigate

10. **NEVER deploy without testing**
    - Run the full test suite before deployment
    - Test on production-like environment first

### ALWAYS Do This in Production

1. **ALWAYS verify all tests pass**
   - Run `cargo test` before deployment
   - All 297 tests should pass (221 unit + 76 integration)

2. **ALWAYS use production build**
   - `cargo build --release` (optimized, no debug)
   - Strip debug symbols if needed

3. **ALWAYS enable logging**
   - Set `RUST_LOG=info` minimum
   - Monitor logs for errors and warnings

4. **ALWAYS monitor health**
   - Regular ping checks
   - Monitor metrics (active sessions, uptime, etc.)

5. **ALWAYS verify SAS with users**
   - Display SAS prominently in UI
   - Require explicit user confirmation

6. **ALWAYS close sessions when done**
   - Send the `close` command when finished
   - Frees resources, prevents leaks

7. **ALWAYS use TLS for WebSocket connections**
   - Use `wss://` URLs, not `ws://`
   - Verify TLS certificates

8. **ALWAYS backup before updates**
   - Backup binary and configuration
   - Test updates in staging first

9. **ALWAYS have rollback plan**
   - Keep previous version available
   - Document rollback procedure

10. **ALWAYS follow principle of least privilege**
    - Run as non-root user
    - Restrict file permissions
    - Use AppArmor/SELinux if available

---

## Performance Tuning

### Optimization Settings

**Rust Compiler Optimizations:**
```toml
# Cargo.toml
[profile.release]
opt-level = 3           # Maximum optimization
lto = "fat"             # Link-time optimization
codegen-units = 1       # Better optimization (slower build)
strip = true            # Strip debug symbols
```

**System Settings:**
```bash
# Increase file descriptor limit (if many sessions)
ulimit -n 4096

# Increase process priority (if needed)
nice -n -10 /usr/local/bin/cosign-bridge

# Use performance CPU governor (if needed)
sudo cpupower frequency-set -g performance
```

### Resource Limits

**Per-Session Limits:**
- Rate limit: 10 requests/second (adjustable)
- Bandwidth limit: 1 MB/second (adjustable)
- Session recovery window: 20 minutes idle (sessions outside it are unrecoverable)

**Global Limits:**
- Max concurrent sessions: Unlimited (resource-limited)
- Max memory per session: ~10-20 KB
- Max message size: 1 MB (Noise protocol limit)

### Load Testing

**Load Test Script:**
```bash
#!/bin/bash
# load-test.sh - Create multiple concurrent sessions

for i in {1..100}; do
    (
        echo '{"command":"init","params":{}}' | cosign-bridge
        sleep 1
    ) &
done

wait
echo "Load test completed"
```

**Expected Performance:**
- Handshake: ~10-15ms per session (local)
- Message send/recv: <10ms (excluding network latency)
- 100 concurrent sessions: <200 MB memory usage
- CPU usage: <5% under normal load

---

## Disaster Recovery

### Data Loss

**Impact:** Session data lost (power failure, disk corruption, etc.)

**Recovery:**
1. Restart bridge service
2. Users must re-initialize sessions (new invite codes)
3. No permanent data loss (sessions are ephemeral)

**Prevention:**
- Regular backups of configuration
- Redundant power supply (UPS)
- RAID storage (if applicable)

### Service Outage

**Impact:** Bridge service down (crash, hardware failure, etc.)

**Recovery:**
1. Check logs for crash cause: `sudo journalctl -u cosign-bridge -n 100`
2. Restart service: `sudo systemctl restart cosign-bridge`
3. Verify health: `./health-check.sh`
4. Monitor logs for repeated issues

**Prevention:**
- Automatic restart on failure (systemd `Restart=on-failure`)
- Health monitoring and alerting
- Regular updates and patches

### Security Breach

**Impact:** Potential compromise of bridge or system

**Response:**
1. **Isolate:** Disconnect from network immediately
2. **Assess:** Determine scope of breach
3. **Contain:** Stop bridge service, preserve logs
4. **Investigate:** Analyze logs, check for unauthorized access
5. **Remediate:** Patch vulnerabilities, rotate secrets
6. **Restore:** Rebuild from clean backup if needed
7. **Report:** Notify users if data compromised

**Prevention:**
- Regular security audits
- Penetration testing
- Intrusion detection (fail2ban, OSSEC, etc.)
- Keep software updated

---

## Compliance & Auditing

### Audit Trail

**What to Log:**
- Session lifecycle (init, handshake, close)
- Handshake completions and failures
- Rate limit and bandwidth limit hits
- Errors and exceptions
- Service start/stop events

**What NOT to Log:**
- Passwords
- Shared secrets
- Plaintext message contents
- Private keys
- Invite codes (except truncated for debugging)

**Log Retention:**
- Operational logs: 30 days
- Security logs: 90 days
- Compliance logs: 1+ year (as required)

### Compliance Requirements

**Data Privacy:**
- No user data stored permanently
- Sessions are ephemeral (no persistent state)
- All data encrypted in transit
- No telemetry or analytics

**Security Standards:**
- Follow NIST guidelines for cryptographic implementations
- Regular security audits (annual minimum)
- Vulnerability disclosure program
- Incident response plan

---

## Conclusion

This guide covers building, installing, supervising, hardening, monitoring, and operating the TensorCash Cosign Bridge in production.

**Key practices:**
- Run the full test suite before deployment.
- Never use test mode in production.
- Monitor health and logs continuously.
- Follow the security hardening and production-safety rules above.
- Keep a tested rollback plan.

**Contact:** ops@tensorcash.org · security issues (private): security@tensorcash.org

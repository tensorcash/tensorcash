// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Minimal Tor control protocol implementation for ADD_ONION
//!
//! This module provides just enough functionality to:
//! - Authenticate with Tor control port using cookie authentication
//! - Create ephemeral hidden services via ADD_ONION command
//! - Delete hidden services via DEL_ONION command
//!
//! References:
//! - Tor Control Protocol: https://spec.torproject.org/control-spec/index.html

use anyhow::{anyhow, Result};
use std::path::Path;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::TcpStream;

/// Tor control port client
pub struct TorControl {
    stream: BufReader<TcpStream>,
}

impl TorControl {
    /// Connect to Tor control port
    pub async fn connect(control_addr: &str) -> Result<Self> {
        log::info!("Connecting to Tor control port: {}", control_addr);
        let stream = TcpStream::connect(control_addr).await.map_err(|e| {
            anyhow!(
                "Failed to connect to Tor control port {}: {}",
                control_addr,
                e
            )
        })?;

        Ok(Self {
            stream: BufReader::new(stream),
        })
    }

    /// Authenticate using cookie file
    pub async fn authenticate_cookie(&mut self, cookie_path: &Path) -> Result<()> {
        log::info!("Authenticating with cookie file: {:?}", cookie_path);

        // Read cookie file (32 bytes)
        let cookie = std::fs::read(cookie_path)
            .map_err(|e| anyhow!("Failed to read Tor cookie file {:?}: {}", cookie_path, e))?;

        if cookie.len() != 32 {
            return Err(anyhow!(
                "Invalid cookie file length: {} (expected 32 bytes)",
                cookie.len()
            ));
        }

        // Send AUTHENTICATE command with hex-encoded cookie
        let cookie_hex = hex::encode(&cookie);
        let command = format!("AUTHENTICATE {}\r\n", cookie_hex);

        self.send_command(&command).await?;
        let response = self.read_response().await?;

        if response.starts_with("250 ") {
            log::info!("✓ Tor authentication successful");
            Ok(())
        } else {
            Err(anyhow!("Tor authentication failed: {}", response))
        }
    }

    /// Create ephemeral hidden service via ADD_ONION
    ///
    /// Returns: (onion_address, service_id)
    ///
    /// **IMPORTANT**: This method waits 30 seconds after creation to allow descriptor
    /// upload and propagation. This ensures the onion address is resolvable when returned.
    ///
    /// Example:
    /// ```text
    /// ADD_ONION NEW:BEST Port=9735,127.0.0.1:9735
    /// 250-ServiceID=abcdef1234567890abcdef1234567890
    /// 250 OK
    /// [sleep 30s for intro circuits + descriptor upload + propagation]
    /// ```
    pub async fn add_onion(&mut self, port: u16) -> Result<(String, String)> {
        log::info!("Creating ephemeral hidden service on port {}", port);

        // Send ADD_ONION command
        // NEW:BEST = generate new keypair with best available algorithm
        // Port=virtual_port,target_addr:target_port
        let command = format!("ADD_ONION NEW:BEST Port={},127.0.0.1:{}\r\n", port, port);

        self.send_command(&command).await?;

        // Parse response (multiple lines)
        // Expected:
        // 250-ServiceID=<56-char-onion-id>
        // 250 OK
        let mut service_id = String::new();
        loop {
            let mut line = String::new();
            self.stream
                .read_line(&mut line)
                .await
                .map_err(|e| anyhow!("Failed to read ADD_ONION response: {}", e))?;

            let line = line.trim();
            log::debug!("ADD_ONION response line: {}", line);

            if line.starts_with("250-ServiceID=") {
                service_id = line.strip_prefix("250-ServiceID=").unwrap().to_string();
            } else if line.starts_with("250 ") {
                // Final line
                break;
            } else if line.starts_with("551 ") || line.starts_with("552 ") {
                return Err(anyhow!("ADD_ONION failed: {}", line));
            }
        }

        if service_id.is_empty() {
            return Err(anyhow!("ADD_ONION response missing ServiceID"));
        }

        let onion_address = format!("{}.onion", service_id);
        let creation_time = chrono::Utc::now();
        log::info!(
            "✓ Created ephemeral hidden service: {} (time: {})",
            onion_address,
            creation_time.format("%H:%M:%S%.3f")
        );

        // Wait for intro circuits to build and descriptor to propagate
        self.wait_for_descriptor_upload(&service_id).await;

        let ready_time = chrono::Utc::now();
        log::info!(
            "✓ Service ready after {}s wait (ready time: {})",
            (ready_time - creation_time).num_seconds(),
            ready_time.format("%H:%M:%S%.3f")
        );

        Ok((onion_address, service_id))
    }

    /// Wait 30 seconds for intro circuits and descriptor propagation
    ///
    /// HS_DESC UPLOADED events are unreliable indicators of service reachability.
    /// The event means "descriptor uploaded to one HSDir" but doesn't guarantee:
    /// - Introduction circuits are fully built
    /// - Descriptor has propagated to enough HSDirs
    /// - Service is actually reachable by clients
    ///
    /// Therefore, we ALWAYS wait the full 30 seconds regardless of events.
    async fn wait_for_descriptor_upload(&mut self, _service_id: &str) {
        use tokio::time::{sleep, Duration};

        let wait_start = chrono::Utc::now();
        log::info!(
            "⏳ Waiting 30s for intro circuits + descriptor propagation (start: {})...",
            wait_start.format("%H:%M:%S%.3f")
        );
        sleep(Duration::from_secs(30)).await;
        let wait_end = chrono::Utc::now();
        log::info!(
            "✓ 30s wait completed - intro circuits should be ready (end: {})",
            wait_end.format("%H:%M:%S%.3f")
        );
    }

    /// Delete ephemeral hidden service via DEL_ONION
    pub async fn del_onion(&mut self, service_id: &str) -> Result<()> {
        log::info!("Deleting hidden service: {}", service_id);

        let command = format!("DEL_ONION {}\r\n", service_id);
        self.send_command(&command).await?;
        let response = self.read_response().await?;

        if response.starts_with("250 ") {
            log::info!("✓ Deleted hidden service: {}", service_id);
            Ok(())
        } else {
            Err(anyhow!("DEL_ONION failed: {}", response))
        }
    }

    /// Send raw command to Tor control port
    async fn send_command(&mut self, command: &str) -> Result<()> {
        self.stream
            .get_mut()
            .write_all(command.as_bytes())
            .await
            .map_err(|e| anyhow!("Failed to send Tor command: {}", e))
    }

    /// Read single-line response (for simple commands)
    async fn read_response(&mut self) -> Result<String> {
        let mut line = String::new();
        self.stream
            .read_line(&mut line)
            .await
            .map_err(|e| anyhow!("Failed to read Tor response: {}", e))?;

        Ok(line.trim().to_string())
    }

    /// Get info about Tor connection
    #[allow(dead_code)]
    pub async fn get_info(&mut self, keyword: &str) -> Result<String> {
        let command = format!("GETINFO {}\r\n", keyword);
        self.send_command(&command).await?;

        let mut result = String::new();
        loop {
            let mut line = String::new();
            self.stream
                .read_line(&mut line)
                .await
                .map_err(|e| anyhow!("Failed to read GETINFO response: {}", e))?;

            let line = line.trim();

            if let Some(value) = line.strip_prefix(&format!("250-{}=", keyword)) {
                result = value.to_string();
            } else if line.starts_with("250 ") {
                break;
            } else if line.starts_with("551 ") {
                return Err(anyhow!("GETINFO failed: {}", line));
            }
        }

        Ok(result)
    }

    /// Subscribe to Tor events for debugging hidden service operations
    ///
    /// Events include:
    /// - HS_DESC: Hidden service descriptor upload/fetch/store events
    /// - HS_SERVICE: Hidden service circuit build events (server side)
    /// - HS_CLIENT: Hidden service circuit build events (client side)
    /// - WARN, ERR: Important diagnostic messages
    ///
    /// NOTE: The control connection must remain open to receive events!
    /// This method returns the TorControl instance; caller must keep it alive.
    pub async fn subscribe_events(&mut self) -> Result<()> {
        log::info!("Subscribing to Tor HS events for debugging...");

        // HS_DESC is the main event for hidden service descriptor operations
        // HS_SERVICE and HS_CLIENT are not available in Tor 0.4.8.x
        let command = "SETEVENTS HS_DESC WARN ERR\r\n";
        self.send_command(command).await?;
        let response = self.read_response().await?;

        if response.starts_with("250 ") {
            log::info!("✓ Subscribed to HS events (HS_DESC, WARN, ERR)");
            Ok(())
        } else {
            Err(anyhow!("SETEVENTS failed: {}", response))
        }
    }

    /// Read and log Tor events in a loop (blocking; meant for background task)
    ///
    /// This method continuously reads asynchronous events from the control port
    /// and logs them at INFO level. It runs until the connection is closed.
    ///
    /// Usage:
    /// ```ignore
    /// tokio::spawn(async move {
    ///     control.read_events_loop().await;
    /// });
    /// ```
    pub async fn read_events_loop(mut self) {
        log::info!("Starting Tor event monitor loop...");

        loop {
            let mut line = String::new();
            match self.stream.read_line(&mut line).await {
                Ok(0) => {
                    // EOF - control connection closed
                    log::info!("Tor event monitor: control connection closed");
                    break;
                }
                Ok(_) => {
                    let line = line.trim();
                    if line.starts_with("650 ")
                        || line.starts_with("650+")
                        || line.starts_with("650-")
                    {
                        // Asynchronous event
                        log::info!("🔔 TOR EVENT: {}", line);
                    } else if !line.is_empty() {
                        log::debug!("Tor event monitor: unexpected line: {}", line);
                    }
                }
                Err(e) => {
                    log::warn!("Tor event monitor error: {}", e);
                    break;
                }
            }
        }

        log::info!("Tor event monitor loop exited");
    }
}

/// Helper: Derive control port address from SOCKS proxy address
///
/// Assumes control port is SOCKS port + 1 (e.g., 9150 -> 9151)
pub fn derive_control_addr_from_socks(socks_proxy: &str) -> Result<String> {
    let parts: Vec<&str> = socks_proxy.split(':').collect();
    if parts.len() != 2 {
        return Err(anyhow!("Invalid SOCKS proxy format: {}", socks_proxy));
    }

    let host = parts[0];
    let socks_port: u16 = parts[1]
        .parse()
        .map_err(|_| anyhow!("Invalid SOCKS port: {}", parts[1]))?;

    let control_port = socks_port + 1;
    Ok(format!("{}:{}", host, control_port))
}

/// Helper: Find cookie file path based on Tor data directory
pub fn find_cookie_file(tor_data_dir: &Path) -> Result<std::path::PathBuf> {
    let cookie_path = tor_data_dir.join("control_auth_cookie");

    if cookie_path.exists() {
        Ok(cookie_path)
    } else {
        Err(anyhow!(
            "Tor cookie file not found: {:?} (is Tor running?)",
            cookie_path
        ))
    }
}

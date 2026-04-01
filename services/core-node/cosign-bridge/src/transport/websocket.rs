// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! WebSocket transport implementation

use anyhow::{Context, Result};
use futures_util::{SinkExt, StreamExt};
use tokio::net::TcpStream;
use tokio_tungstenite::{
    connect_async,
    tungstenite::{self, Message},
    MaybeTlsStream, WebSocketStream,
};

type WsStream = WebSocketStream<MaybeTlsStream<TcpStream>>;

pub struct WebSocketTransport {
    relay_urls: Vec<String>,
    /// The relay URL that was actually connected
    pub(crate) connected_relay: Option<String>,
    room_id: String,
    pub(crate) stream: Option<WsStream>,
}

impl WebSocketTransport {
    /// Default cosign relay URLs, tried in order during connection
    pub const DEFAULT_RELAY_URLS: &[&str] =
        &["wss://relay.tensorcash.org", "wss://relay.tensorcash.org"];

    #[allow(dead_code)]
    pub fn new(relay_url: String, room_id: String) -> Self {
        Self {
            relay_urls: vec![relay_url],
            connected_relay: None,
            room_id,
            stream: None,
        }
    }

    /// Create transport with multiple relay URLs (tried in order as fallbacks)
    #[allow(dead_code)]
    pub fn with_fallbacks(relay_urls: Vec<String>, room_id: String) -> Self {
        Self {
            relay_urls,
            connected_relay: None,
            room_id,
            stream: None,
        }
    }

    /// Build a WebSocket upgrade request with proper headers (User-Agent for WAF compatibility)
    fn build_request(url: &str) -> Result<tungstenite::http::Request<()>> {
        let host = url::Url::parse(url)
            .map(|u| u.host_str().unwrap_or("").to_string())
            .unwrap_or_default();

        tungstenite::http::Request::builder()
            .uri(url)
            .header("Host", host)
            .header("Connection", "Upgrade")
            .header("Upgrade", "websocket")
            .header("Sec-WebSocket-Version", "13")
            .header(
                "Sec-WebSocket-Key",
                tungstenite::handshake::client::generate_key(),
            )
            .header(
                "User-Agent",
                format!("cosign-bridge/{}", env!("CARGO_PKG_VERSION")),
            )
            .body(())
            .context("Failed to build WebSocket request")
    }

    /// Connect to the WebSocket relay, trying each URL in order
    #[allow(dead_code)]
    pub async fn connect(&mut self) -> Result<()> {
        log::info!(
            "WebSocket::connect() relay_urls={:?} room={}",
            self.relay_urls,
            self.room_id
        );

        let mut last_error = None;

        for relay_url in &self.relay_urls {
            let url = format!("{}/room/{}", relay_url, self.room_id);
            log::info!("Trying relay: {}", relay_url);

            let request = match Self::build_request(&url) {
                Ok(r) => r,
                Err(e) => {
                    log::warn!("Failed to build request for {}: {}", relay_url, e);
                    last_error = Some(e);
                    continue;
                }
            };

            match connect_async(request).await {
                Ok((ws_stream, _)) => {
                    log::info!("Connected to relay {} for room {}", relay_url, self.room_id);
                    self.connected_relay = Some(relay_url.clone());
                    self.stream = Some(ws_stream);
                    return Ok(());
                }
                Err(e) => {
                    log::warn!("Failed to connect to {}: {}", relay_url, e);
                    last_error = Some(anyhow::anyhow!("{}: {}", relay_url, e));
                }
            }
        }

        Err(last_error.unwrap_or_else(|| anyhow::anyhow!("No relay URLs configured")))
            .context("Failed to connect to any WebSocket relay")
    }

    /// Send a message through the WebSocket
    /// TODO: Integrate with SessionManager::send()
    #[allow(dead_code)]
    pub async fn send(&mut self, data: Vec<u8>) -> Result<()> {
        let stream = self
            .stream
            .as_mut()
            .ok_or_else(|| anyhow::anyhow!("Not connected"))?;

        stream
            .send(Message::Binary(data))
            .await
            .context("Failed to send message")?;

        Ok(())
    }

    /// Receive a message from the WebSocket
    /// TODO: Integrate with SessionManager::recv()
    #[allow(dead_code)]
    pub async fn recv(&mut self) -> Result<Vec<u8>> {
        let stream = self
            .stream
            .as_mut()
            .ok_or_else(|| anyhow::anyhow!("Not connected"))?;

        match stream.next().await {
            Some(Ok(Message::Binary(data))) => Ok(data),
            Some(Ok(Message::Text(text))) => Ok(text.into_bytes()),
            Some(Ok(Message::Close(_))) => {
                anyhow::bail!("WebSocket closed by peer")
            }
            Some(Err(e)) => {
                anyhow::bail!("WebSocket error: {}", e)
            }
            None => {
                anyhow::bail!("WebSocket stream ended")
            }
            _ => {
                anyhow::bail!("Unexpected message type")
            }
        }
    }

    /// Close the WebSocket connection
    /// TODO: Call during session close
    #[allow(dead_code)]
    pub async fn close(&mut self) -> Result<()> {
        if let Some(mut stream) = self.stream.take() {
            stream
                .close(None)
                .await
                .context("Failed to close WebSocket")?;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn test_transport_creation() {
        let transport = WebSocketTransport::new(
            "wss://relay.example.com".to_string(),
            "test_room".to_string(),
        );
        assert_eq!(transport.relay_urls, vec!["wss://relay.example.com"]);
        assert_eq!(transport.room_id, "test_room");
        assert!(transport.connected_relay.is_none());
        assert!(transport.stream.is_none());
    }

    #[tokio::test]
    async fn test_transport_with_fallbacks() {
        let transport = WebSocketTransport::with_fallbacks(
            vec![
                "wss://relay.example.com".to_string(),
                "wss://fallback.example.com".to_string(),
            ],
            "test_room".to_string(),
        );
        assert_eq!(transport.relay_urls.len(), 2);
        assert_eq!(transport.relay_urls[0], "wss://relay.example.com");
        assert_eq!(transport.relay_urls[1], "wss://fallback.example.com");
        assert!(transport.connected_relay.is_none());
    }

    #[test]
    fn test_default_relay_urls() {
        assert!(WebSocketTransport::DEFAULT_RELAY_URLS.len() >= 2);
        assert!(WebSocketTransport::DEFAULT_RELAY_URLS[0].contains("tensorcash.org"));
        assert!(WebSocketTransport::DEFAULT_RELAY_URLS[1].contains("tensorcash.org"));
    }

    #[tokio::test]
    async fn test_transport_connect_failure() {
        let mut transport = WebSocketTransport::new(
            "wss://invalid-relay-that-does-not-exist.example.com".to_string(),
            "test_room".to_string(),
        );

        // Should fail to connect to non-existent server
        let result = transport.connect().await;
        assert!(result.is_err());
    }

    #[tokio::test]
    async fn test_transport_send_without_connection() {
        let mut transport = WebSocketTransport::new(
            "wss://relay.example.com".to_string(),
            "test_room".to_string(),
        );

        // Should fail when not connected
        let result = transport.send(vec![1, 2, 3]).await;
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("Not connected"));
    }

    #[tokio::test]
    async fn test_transport_recv_without_connection() {
        let mut transport = WebSocketTransport::new(
            "wss://relay.example.com".to_string(),
            "test_room".to_string(),
        );

        // Should fail when not connected
        let result = transport.recv().await;
        assert!(result.is_err());
        assert!(result.unwrap_err().to_string().contains("Not connected"));
    }

    #[tokio::test]
    async fn test_transport_close_without_connection() {
        let mut transport = WebSocketTransport::new(
            "wss://relay.example.com".to_string(),
            "test_room".to_string(),
        );

        // Should succeed even if not connected
        let result = transport.close().await;
        assert!(result.is_ok());
    }

    #[test]
    fn test_transport_url_formatting() {
        let transport = WebSocketTransport::new(
            "wss://relay.example.com".to_string(),
            "my_room_123".to_string(),
        );

        // The URL is constructed in connect() as "{relay_url}/room/{room_id}"
        let expected_url = format!("{}/room/{}", transport.relay_urls[0], transport.room_id);
        assert_eq!(expected_url, "wss://relay.example.com/room/my_room_123");
    }

    #[test]
    fn test_transport_field_access() {
        let transport = WebSocketTransport::new(
            "wss://relay.example.com".to_string(),
            "test_room".to_string(),
        );

        assert_eq!(transport.relay_urls, vec!["wss://relay.example.com"]);
        assert_eq!(transport.room_id, "test_room");
        assert!(transport.connected_relay.is_none());
        assert!(transport.stream.is_none());
    }
}

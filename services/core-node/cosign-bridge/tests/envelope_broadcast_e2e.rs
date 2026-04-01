// Copyright (c) 2026 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! End-to-end wire test for the relay envelope filter + poisoned-session
//! teardown against a relay that BROADCASTS-TO-SENDER (echoes every binary
//! frame to all connected clients in the room, including the original
//! sender).
//!
//! This is the failure mode that the production bug exhibited:
//!
//! - Maker initiates a cosign session, sends SPAKE2/Noise handshake frames.
//! - Public relay broadcasts those frames to every client in the room.
//! - One of the destinations is the maker themselves; their bridge sees
//!   its own SPAKE2/Noise bytes coming back on the recv side.
//! - Without the envelope filter, those own-echoed bytes are fed into
//!   Noise transport-mode decrypt and the AEAD fails with
//!   "Noise decryption failed".
//! - With the envelope filter, those own-echoed frames carry our own
//!   instance_id in `sender`, so try_unwrap_envelope drops them before
//!   they ever reach the Noise cipher.
//!
//! The existing `tests/websocket_integration.rs::test_automated_handshake`
//! is `#[ignore]` because it predates the room-from-URL convention the
//! bridge uses (`{relay}/room/{room_id}`). This test ships its own minimal
//! relay that:
//!
//!   * parses the room_id directly from the request path,
//!   * broadcasts every binary frame to ALL clients in the room INCLUDING
//!     the sender — explicitly NOT filtering own messages, which is the
//!     condition needed to exercise the envelope's `sender` check.

use cosign_bridge::session::SessionManager;
use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use std::collections::HashMap;
use std::sync::Arc;
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{mpsc, Mutex};
use tokio_tungstenite::{
    accept_hdr_async,
    tungstenite::{
        handshake::server::{Request, Response},
        Message,
    },
};

type Rooms = Arc<Mutex<HashMap<String, Vec<mpsc::UnboundedSender<Vec<u8>>>>>>;

/// Start a relay that broadcasts every binary frame to every connected
/// client in the room — including the sender. Returns the bound port.
async fn start_broadcast_to_sender_relay() -> u16 {
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let port = listener.local_addr().unwrap().port();
    let rooms: Rooms = Arc::new(Mutex::new(HashMap::new()));

    tokio::spawn(async move {
        loop {
            let (stream, _) = match listener.accept().await {
                Ok(v) => v,
                Err(_) => continue,
            };
            let rooms = rooms.clone();
            tokio::spawn(async move {
                let _ = handle_conn(stream, rooms).await;
            });
        }
    });

    port
}

async fn handle_conn(stream: TcpStream, rooms: Rooms) -> anyhow::Result<()> {
    // Extract room_id from URI path "/room/{room_id}" during the WS upgrade.
    let captured_room: Arc<Mutex<Option<String>>> = Arc::new(Mutex::new(None));
    let captured_room_cb = captured_room.clone();
    let ws_stream = accept_hdr_async(stream, move |req: &Request, response: Response| {
        let path = req.uri().path().to_string();
        let room = path
            .strip_prefix("/room/")
            .map(|s| s.to_string())
            .unwrap_or_else(|| "default".to_string());
        // captured_room_cb is moved into the callback; assign synchronously.
        if let Ok(mut slot) = captured_room_cb.try_lock() {
            *slot = Some(room);
        }
        Ok(response)
    })
    .await?;

    let room_id = captured_room
        .lock()
        .await
        .clone()
        .unwrap_or_else(|| "default".to_string());

    let (mut ws_sender, mut ws_receiver) = ws_stream.split();
    let (tx, mut rx) = mpsc::unbounded_channel::<Vec<u8>>();

    {
        let mut rooms_lock = rooms.lock().await;
        rooms_lock.entry(room_id.clone()).or_default().push(tx);
    }

    // Outbound pump.
    let send_task = tokio::spawn(async move {
        while let Some(data) = rx.recv().await {
            if ws_sender.send(Message::Binary(data)).await.is_err() {
                break;
            }
        }
    });

    // Inbound: broadcast EVERY binary frame to every channel in the room,
    // including the sender. This is the condition the envelope's sender
    // field is supposed to defend against.
    while let Some(Ok(msg)) = ws_receiver.next().await {
        if let Message::Binary(data) = msg {
            let rooms_lock = rooms.lock().await;
            if let Some(conns) = rooms_lock.get(&room_id) {
                for conn in conns {
                    let _ = conn.send(data.clone());
                }
            }
        }
    }

    send_task.abort();
    Ok(())
}

/// Full wire-path validation of the envelope work against a broadcast-to-
/// sender relay: SPAKE2/Noise handshake completes, both sides agree on the
/// SAS, and the first transport-mode message decrypts correctly.
///
/// If the envelope filter is broken — missing, missing sender field, sender
/// filter not consulted — every own-echoed handshake frame poisons the
/// peer's Noise state and the handshake never completes within the
/// configured 25s deadline. The test would hang to its outer timeout.
#[tokio::test]
async fn handshake_and_first_data_survive_broadcast_to_sender_relay() {
    let _ = env_logger::builder()
        .filter_level(log::LevelFilter::Info)
        .is_test(true)
        .try_init();

    let port = start_broadcast_to_sender_relay().await;
    let relay_url = format!("ws://127.0.0.1:{}", port);
    std::env::set_var("COSIGN_RELAY_URL", &relay_url);

    let mut initiator = SessionManager::new();
    let mut responder = SessionManager::new();

    // ---- init ----
    let init_result = initiator
        .init(json!({ "transport": "websocket", "ttl": 60 }))
        .await
        .expect("init");
    let init_sid = init_result["session_id"].as_str().unwrap().to_string();
    let invite_link = init_result["invite_link"].as_str().unwrap().to_string();
    assert!(
        invite_link.contains("&h="),
        "invite_link must carry handshake_id: {}",
        invite_link
    );

    // ---- join ----
    let join_result = responder
        .join(json!({ "invite_link": invite_link, "relay_url": relay_url }))
        .await
        .expect("join");
    let resp_sid = join_result["session_id"].as_str().unwrap().to_string();

    // ---- concurrent handshake ----
    let init_sid_clone = init_sid.clone();
    let resp_sid_clone = resp_sid.clone();
    let initiator_task = tokio::spawn(async move {
        let r = initiator
            .handshake_auto(json!({ "session_id": init_sid_clone, "is_initiator": true }))
            .await;
        (initiator, r)
    });
    let responder_task = tokio::spawn(async move {
        let r = responder
            .handshake_auto(json!({ "session_id": resp_sid_clone, "is_initiator": false }))
            .await;
        (responder, r)
    });

    // 30s outer deadline; the inner handshake deadline is 25s. If the
    // envelope filter ever regresses, this will time out instead of hanging
    // forever and lock up CI.
    let joined = tokio::time::timeout(
        std::time::Duration::from_secs(30),
        async {
            let i = initiator_task.await.unwrap();
            let r = responder_task.await.unwrap();
            (i, r)
        },
    )
    .await
    .expect("handshake_auto must finish within 30s against broadcast-to-sender relay");

    let ((mut initiator, init_handshake), (mut responder, resp_handshake)) = joined;
    let init_handshake = init_handshake.expect("initiator handshake");
    let resp_handshake = resp_handshake.expect("responder handshake");

    assert_eq!(init_handshake["handshake_complete"], true);
    assert_eq!(resp_handshake["handshake_complete"], true);
    assert_eq!(
        init_handshake["sas"], resp_handshake["sas"],
        "both sides must derive the same SAS",
    );

    // ---- first transport message: initiator → responder ----
    initiator
        .send(json!({
            "session_id": init_sid,
            "payload": { "hello": "from initiator" },
        }))
        .await
        .expect("send i→r");

    let resp_recv = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        responder.recv(json!({ "session_id": resp_sid, "timeout_ms": 4000 })),
    )
    .await
    .expect("recv i→r must finish within 5s")
    .expect("recv ok");

    assert_eq!(
        resp_recv["payload"]["hello"], "from initiator",
        "responder must decrypt initiator's first transport message; got {:?}",
        resp_recv,
    );

    // ---- first transport message: responder → initiator ----
    responder
        .send(json!({
            "session_id": resp_sid,
            "payload": { "hello": "from responder" },
        }))
        .await
        .expect("send r→i");

    let init_recv = tokio::time::timeout(
        std::time::Duration::from_secs(5),
        initiator.recv(json!({ "session_id": init_sid, "timeout_ms": 4000 })),
    )
    .await
    .expect("recv r→i must finish within 5s")
    .expect("recv ok");

    assert_eq!(
        init_recv["payload"]["hello"], "from responder",
        "initiator must decrypt responder's first transport message; got {:?}",
        init_recv,
    );
}

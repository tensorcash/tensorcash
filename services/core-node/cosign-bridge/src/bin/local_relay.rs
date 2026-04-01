use std::collections::{HashMap, VecDeque};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use anyhow::Result;
use clap::Parser;
use futures_util::{SinkExt, StreamExt};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::mpsc;
use tokio_tungstenite::{
    accept_hdr_async,
    tungstenite::{
        handshake::server::{Request, Response},
        Message,
    },
};

#[derive(Clone)]
struct ClientEntry {
    id: u64,
    sender: mpsc::UnboundedSender<Vec<u8>>,
}

#[derive(Clone)]
struct BufferedMessage {
    data: Vec<u8>,
    timestamp: u64, // milliseconds since epoch
    sender_id: u64, // ID of client who sent this message
}

struct RoomState {
    clients: Vec<ClientEntry>,
    message_buffer: VecDeque<BufferedMessage>,
}

impl RoomState {
    fn new() -> Self {
        Self {
            clients: Vec::new(),
            message_buffer: VecDeque::new(),
        }
    }

    fn add_client(&mut self, client: ClientEntry) {
        self.clients.push(client);
    }

    fn buffer_message(&mut self, data: Vec<u8>, sender_id: u64) {
        let now = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis() as u64;

        self.message_buffer.push_back(BufferedMessage {
            data,
            timestamp: now,
            sender_id,
        });

        // Keep last 10 messages, drop older
        const MAX_BUFFERED: usize = 10;
        while self.message_buffer.len() > MAX_BUFFERED {
            self.message_buffer.pop_front();
        }

        // Also drop messages older than 30 seconds
        const MAX_AGE_MS: u64 = 30_000;
        while let Some(front) = self.message_buffer.front() {
            if now - front.timestamp > MAX_AGE_MS {
                self.message_buffer.pop_front();
            } else {
                break;
            }
        }
    }

    fn get_buffered_messages(&self, recipient_id: u64) -> Vec<Vec<u8>> {
        self.message_buffer
            .iter()
            .filter(|msg| msg.sender_id != recipient_id) // Skip messages from this client
            .map(|msg| msg.data.clone())
            .collect()
    }
}

type Rooms = Arc<Mutex<HashMap<String, RoomState>>>;
static NEXT_CLIENT_ID: AtomicU64 = AtomicU64::new(0);

#[derive(Parser, Debug)]
#[command(
    author,
    version,
    about = "Minimal local relay for cosign WebSocket tests"
)]
struct Args {
    /// Host to bind (default: 127.0.0.1)
    #[arg(short, long, default_value = "127.0.0.1")]
    host: String,

    /// Port to listen on (default: 9736)
    #[arg(short, long, default_value_t = 9736)]
    port: u16,

    /// Health check port (HTTP GET /health). 0 = disabled.
    #[arg(long, default_value_t = 0)]
    health_port: u16,
}

async fn serve_health(stream: TcpStream) {
    let mut buf = [0u8; 1024];
    let mut stream = stream;
    let _ = stream.read(&mut buf).await;
    let body = r#"{"status":"ok"}"#;
    let resp = format!(
        "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(), body
    );
    let _ = stream.write_all(resp.as_bytes()).await;
}

#[tokio::main]
async fn main() -> Result<()> {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("info")).init();

    let args = Args::parse();
    let bind_addr = format!("{}:{}", args.host, args.port);

    log::info!("Starting local cosign relay on ws://{}", bind_addr);

    // Optional health check server for load balancer probes
    if args.health_port > 0 {
        let health_addr = format!("{}:{}", args.host, args.health_port);
        log::info!("Health check on http://{}/health", health_addr);
        let health_listener = TcpListener::bind(&health_addr).await?;
        tokio::spawn(async move {
            loop {
                if let Ok((stream, _)) = health_listener.accept().await {
                    tokio::spawn(serve_health(stream));
                }
            }
        });
    }

    let listener = TcpListener::bind(&bind_addr).await?;
    let rooms: Rooms = Arc::new(Mutex::new(HashMap::new()));

    loop {
        let (stream, addr) = listener.accept().await?;
        log::debug!("Accepted connection from {}", addr);

        let rooms_clone = rooms.clone();
        let client_id = NEXT_CLIENT_ID.fetch_add(1, Ordering::Relaxed);
        tokio::spawn(async move {
            if let Err(e) = handle_connection(stream, rooms_clone, client_id).await {
                log::error!("Connection error: {}", e);
            }
        });
    }
}

async fn handle_connection(stream: TcpStream, rooms: Rooms, client_id: u64) -> Result<()> {
    let selected_room: Arc<Mutex<String>> = Arc::new(Mutex::new(String::new()));
    let room_capture = selected_room.clone();

    let ws_stream = accept_hdr_async(stream, move |req: &Request, response: Response| {
        let path = req.uri().path();
        let room = path
            .strip_prefix("/room/")
            .filter(|segment| !segment.is_empty())
            .map(|segment| segment.trim_matches('/').to_string())
            .unwrap_or_else(|| "default".to_string());

        if let Ok(mut slot) = room_capture.lock() {
            *slot = room;
        }

        Ok(response)
    })
    .await?;

    let room_id = {
        let slot = selected_room.lock().unwrap();
        if slot.is_empty() {
            "default".to_string()
        } else {
            slot.clone()
        }
    };

    log::debug!("Client joined room {}", room_id);

    let (mut ws_sender, mut ws_receiver) = ws_stream.split();
    let (tx, mut rx) = mpsc::unbounded_channel::<Vec<u8>>();
    let client_entry = ClientEntry {
        id: client_id,
        sender: tx.clone(),
    };

    // Send buffered messages to new client, then add them to room
    {
        let mut rooms_lock = rooms.lock().unwrap();
        let room_state = rooms_lock
            .entry(room_id.clone())
            .or_insert_with(RoomState::new);

        // Send all buffered messages to the new client (except their own)
        let all_buffered = room_state.message_buffer.len();
        let buffered = room_state.get_buffered_messages(client_id);
        let buffered_count = buffered.len();

        log::info!(
            "Client {} joining room {} (total buffered: {}, sending: {})",
            client_id,
            room_id,
            all_buffered,
            buffered_count
        );

        for msg in buffered {
            let _ = tx.send(msg);
        }

        room_state.add_client(client_entry);
    }

    let rooms_cleanup = rooms.clone();
    let room_for_sender = room_id.clone();
    let send_task = tokio::spawn(async move {
        while let Some(data) = rx.recv().await {
            if let Err(e) = ws_sender.send(Message::Binary(data)).await {
                log::warn!(
                    "Failed to forward message to room {}: {}",
                    room_for_sender,
                    e
                );
                break;
            }
        }
    });

    while let Some(msg) = ws_receiver.next().await {
        match msg {
            Ok(Message::Binary(data)) => {
                let mut rooms_lock = rooms.lock().unwrap();
                if let Some(room_state) = rooms_lock.get_mut(&room_id) {
                    // Buffer the message for future clients (with sender ID)
                    room_state.buffer_message(data.clone(), client_id);

                    let forward_count = room_state
                        .clients
                        .iter()
                        .filter(|c| c.id != client_id)
                        .count();

                    log::info!("📤 Client {} sent message in room {} (buffered count now: {}, forwarding to {} clients)",
                               client_id, room_id, room_state.message_buffer.len(), forward_count);

                    // Forward to currently connected clients (except sender)
                    for conn in &room_state.clients {
                        if conn.id != client_id {
                            let _ = conn.sender.send(data.clone());
                        }
                    }
                }
            }
            Ok(Message::Close(_)) => {
                log::debug!("Client in room {} closed connection", room_id);
                break;
            }
            Ok(Message::Ping(_)) => {
                log::debug!("Ignoring ping from room {}", room_id);
            }
            Ok(Message::Pong(_)) => {}
            Ok(Message::Text(text)) => {
                log::debug!("Ignoring text message in room {}: {}", room_id, text);
            }
            Ok(Message::Frame(_)) => {
                // Frame variants are not expected in this simple relay; ignore safely.
            }
            Err(e) => {
                log::warn!("WebSocket error in room {}: {}", room_id, e);
                break;
            }
        }
    }

    send_task.abort();

    {
        let mut rooms_lock = rooms_cleanup.lock().unwrap();
        if let Some(room_state) = rooms_lock.get_mut(&room_id) {
            room_state
                .clients
                .retain(|conn| conn.id != client_id && !conn.sender.is_closed());
            if room_state.clients.is_empty() {
                let dropped = room_state.message_buffer.len();
                log::info!(
                    "Room {} empty; clearing {} buffered message(s)",
                    room_id,
                    dropped
                );
                rooms_lock.remove(&room_id);
            }
        }
    }

    log::debug!("Client disconnected from room {}", room_id);
    Ok(())
}

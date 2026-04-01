// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Message framing and protocol structures

use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};

/// Message frame with encryption and metadata
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Frame {
    pub api_version: u32,
    pub msg_type: String,
    pub seq: u64,
    pub ts: u64, // Unix timestamp
    pub pad_len: usize,
    pub ciphertext: Vec<u8>,
    #[serde(with = "serde_bytes")]
    pub tag: Vec<u8>,
}

impl Frame {
    /// TODO: Integrate with actual message framing in send/recv
    #[allow(dead_code)]
    pub fn new(msg_type: String, seq: u64, ciphertext: Vec<u8>, tag: Vec<u8>) -> Self {
        Self {
            api_version: 1,
            msg_type,
            seq,
            ts: current_timestamp(),
            pad_len: 0,
            ciphertext,
            tag,
        }
    }

    /// Apply padding to reach target bucket size (256, 512, or 1024 bytes)
    /// TODO: Use for traffic analysis resistance
    #[allow(dead_code)]
    pub fn apply_padding(&mut self) {
        let current_size = self.ciphertext.len();
        let target_size = if current_size <= 256 {
            256
        } else if current_size <= 512 {
            512
        } else {
            1024
        };

        if current_size < target_size {
            let pad_len = target_size - current_size;
            self.ciphertext.resize(target_size, 0);
            self.pad_len = pad_len;
        }
    }

    /// Remove padding
    /// TODO: Use when receiving padded frames
    #[allow(dead_code)]
    pub fn remove_padding(&mut self) {
        if self.pad_len > 0 {
            let new_len = self.ciphertext.len() - self.pad_len;
            self.ciphertext.truncate(new_len);
            self.pad_len = 0;
        }
    }

    /// Validate frame timestamp (within ±120s window)
    /// TODO: Use for replay attack protection
    #[allow(dead_code)]
    pub fn validate_timestamp(&self) -> bool {
        let now = current_timestamp();
        let diff = if now > self.ts {
            now - self.ts
        } else {
            self.ts - now
        };
        diff <= 120
    }
}

/// Payload structure for messages
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Payload {
    #[serde(rename = "type")]
    pub payload_type: String,
    pub data: serde_json::Value,
}

#[allow(dead_code)]
fn current_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap()
        .as_secs()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_frame_creation() {
        let frame = Frame::new("test".to_string(), 1, vec![1, 2, 3], vec![4, 5, 6]);
        assert_eq!(frame.api_version, 1);
        assert_eq!(frame.seq, 1);
        assert_eq!(frame.msg_type, "test");
        assert_eq!(frame.ciphertext, vec![1, 2, 3]);
        assert_eq!(frame.tag, vec![4, 5, 6]);
        assert_eq!(frame.pad_len, 0);
        // Timestamp should be recent
        let now = current_timestamp();
        assert!(frame.ts <= now);
        assert!(frame.ts >= now - 5); // Within last 5 seconds
    }

    #[test]
    fn test_padding_small() {
        let mut frame = Frame::new(
            "test".to_string(),
            1,
            vec![1, 2, 3], // 3 bytes -> should pad to 256
            vec![],
        );
        frame.apply_padding();
        assert_eq!(frame.ciphertext.len(), 256);
        assert_eq!(frame.pad_len, 253);

        frame.remove_padding();
        assert_eq!(frame.ciphertext.len(), 3);
        assert_eq!(frame.pad_len, 0);
    }

    #[test]
    fn test_padding_medium() {
        let mut frame = Frame::new(
            "test".to_string(),
            1,
            vec![0u8; 300], // 300 bytes -> should pad to 512
            vec![],
        );
        frame.apply_padding();
        assert_eq!(frame.ciphertext.len(), 512);
        assert_eq!(frame.pad_len, 212);

        frame.remove_padding();
        assert_eq!(frame.ciphertext.len(), 300);
        assert_eq!(frame.pad_len, 0);
    }

    #[test]
    fn test_padding_large() {
        let mut frame = Frame::new(
            "test".to_string(),
            1,
            vec![0u8; 600], // 600 bytes -> should pad to 1024
            vec![],
        );
        frame.apply_padding();
        assert_eq!(frame.ciphertext.len(), 1024);
        assert_eq!(frame.pad_len, 424);

        frame.remove_padding();
        assert_eq!(frame.ciphertext.len(), 600);
        assert_eq!(frame.pad_len, 0);
    }

    #[test]
    fn test_padding_exact_boundary() {
        let mut frame = Frame::new(
            "test".to_string(),
            1,
            vec![0u8; 256], // Exactly 256 bytes
            vec![],
        );
        frame.apply_padding();
        // Already at boundary, should not pad
        assert_eq!(frame.ciphertext.len(), 256);
        assert_eq!(frame.pad_len, 0);
    }

    #[test]
    fn test_remove_padding_without_padding() {
        let mut frame = Frame::new("test".to_string(), 1, vec![1, 2, 3], vec![]);
        // No padding applied
        frame.remove_padding();
        assert_eq!(frame.ciphertext.len(), 3);
        assert_eq!(frame.pad_len, 0);
    }

    #[test]
    fn test_timestamp_validation_current() {
        let frame = Frame::new("test".to_string(), 1, vec![], vec![]);
        // Current timestamp should be valid
        assert!(frame.validate_timestamp());
    }

    #[test]
    fn test_timestamp_validation_old() {
        let mut frame = Frame::new("test".to_string(), 1, vec![], vec![]);
        // Set timestamp to 119 seconds ago (within 120s window)
        frame.ts = current_timestamp() - 119;
        assert!(frame.validate_timestamp());

        // Set timestamp to 121 seconds ago (outside window)
        frame.ts = current_timestamp() - 121;
        assert!(!frame.validate_timestamp());
    }

    #[test]
    fn test_timestamp_validation_future() {
        let mut frame = Frame::new("test".to_string(), 1, vec![], vec![]);
        // Set timestamp to 119 seconds in future (within 120s window)
        frame.ts = current_timestamp() + 119;
        assert!(frame.validate_timestamp());

        // Set timestamp to 121 seconds in future (outside window)
        frame.ts = current_timestamp() + 121;
        assert!(!frame.validate_timestamp());
    }

    #[test]
    fn test_frame_sequence_numbers() {
        let frame1 = Frame::new("test".to_string(), 1, vec![], vec![]);
        let frame2 = Frame::new("test".to_string(), 2, vec![], vec![]);
        let frame3 = Frame::new("test".to_string(), 999, vec![], vec![]);

        assert_eq!(frame1.seq, 1);
        assert_eq!(frame2.seq, 2);
        assert_eq!(frame3.seq, 999);
    }

    #[test]
    fn test_frame_message_types() {
        let frame1 = Frame::new("init".to_string(), 1, vec![], vec![]);
        let frame2 = Frame::new("data".to_string(), 2, vec![], vec![]);
        let frame3 = Frame::new("close".to_string(), 3, vec![], vec![]);

        assert_eq!(frame1.msg_type, "init");
        assert_eq!(frame2.msg_type, "data");
        assert_eq!(frame3.msg_type, "close");
    }

    #[test]
    fn test_payload_serialization() {
        let payload = Payload {
            payload_type: "test".to_string(),
            data: serde_json::json!({"key": "value"}),
        };

        let serialized = serde_json::to_string(&payload).unwrap();
        assert!(serialized.contains("test"));
        assert!(serialized.contains("key"));
        assert!(serialized.contains("value"));

        let deserialized: Payload = serde_json::from_str(&serialized).unwrap();
        assert_eq!(deserialized.payload_type, "test");
    }

    #[test]
    fn test_frame_serialization() {
        let frame = Frame::new("test".to_string(), 42, vec![1, 2, 3], vec![4, 5, 6]);

        let serialized = serde_json::to_string(&frame).unwrap();
        let deserialized: Frame = serde_json::from_str(&serialized).unwrap();

        assert_eq!(deserialized.msg_type, "test");
        assert_eq!(deserialized.seq, 42);
        assert_eq!(deserialized.ciphertext, vec![1, 2, 3]);
        assert_eq!(deserialized.tag, vec![4, 5, 6]);
    }
}

// Copyright (c) 2025 The TensorCash developers
// Distributed under the MIT software license, see the accompanying
// file COPYING or https://opensource.org/license/mit/.

//! Minimal Ethereum JSON-RPC client.
//!
//! No heavy framework — just raw HTTP + JSON-RPC 2.0 over reqwest.
//! Covers the exact calls needed for HTLC interaction:
//!   eth_blockNumber, eth_getTransactionReceipt, eth_getTransactionCount,
//!   eth_sendRawTransaction, eth_call, eth_getLogs, eth_gasPrice,
//!   eth_maxPriorityFeePerGas, eth_estimateGas, eth_chainId

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::sync::atomic::{AtomicU64, Ordering};

/// Ethereum JSON-RPC client.
#[derive(Debug, Clone)]
pub struct EthRpcClient {
    url: String,
    client: reqwest::Client,
    request_id: std::sync::Arc<AtomicU64>,
}

/// Transaction receipt from eth_getTransactionReceipt.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct TxReceipt {
    pub transaction_hash: String,
    pub block_number: Option<String>, // hex
    pub block_hash: Option<String>,
    pub status: Option<String>, // "0x1" success, "0x0" failure
    pub gas_used: Option<String>,
    #[serde(default)]
    pub logs: Vec<LogEntry>,
}

/// Log entry from transaction receipt or eth_getLogs.
#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct LogEntry {
    pub address: String,
    pub topics: Vec<String>,
    pub data: String,
    pub block_number: Option<String>,
    pub transaction_hash: Option<String>,
    pub log_index: Option<String>,
}

/// Filter for eth_getLogs.
#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct LogFilter {
    #[serde(skip_serializing_if = "Option::is_none")]
    pub from_block: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub to_block: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub address: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub topics: Option<Vec<Option<String>>>,
}

#[derive(Debug, thiserror::Error)]
pub enum RpcError {
    #[error("HTTP error: {0}")]
    Http(#[from] reqwest::Error),
    #[error("JSON-RPC error {code}: {message}")]
    JsonRpc { code: i64, message: String },
    #[error("unexpected response: {0}")]
    Unexpected(String),
    #[error("null result for {0}")]
    NullResult(String),
}

impl EthRpcClient {
    /// Create a new client pointing at the given JSON-RPC endpoint.
    pub fn new(url: &str) -> Self {
        Self {
            url: url.to_string(),
            client: reqwest::Client::new(),
            request_id: std::sync::Arc::new(AtomicU64::new(1)),
        }
    }

    /// Send a raw JSON-RPC request and return the `result` field.
    async fn call(&self, method: &str, params: Value) -> Result<Value, RpcError> {
        let id = self.request_id.fetch_add(1, Ordering::Relaxed);

        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
            "id": id,
        });

        let resp = self
            .client
            .post(&self.url)
            .json(&body)
            .send()
            .await?
            .json::<Value>()
            .await?;

        if let Some(err) = resp.get("error") {
            let code = err.get("code").and_then(|c| c.as_i64()).unwrap_or(-1);
            let message = err
                .get("message")
                .and_then(|m| m.as_str())
                .unwrap_or("unknown")
                .to_string();
            return Err(RpcError::JsonRpc { code, message });
        }

        match resp.get("result") {
            Some(Value::Null) => Err(RpcError::NullResult(method.to_string())),
            Some(result) => Ok(result.clone()),
            None => Err(RpcError::Unexpected("missing result field".into())),
        }
    }

    // ---------------------------------------------------------------
    // Public API
    // ---------------------------------------------------------------

    /// Get the current block number (hex-encoded).
    pub async fn block_number(&self) -> Result<u64, RpcError> {
        let result = self.call("eth_blockNumber", Value::Array(vec![])).await?;
        parse_hex_u64(&result)
    }

    /// Get the chain ID.
    pub async fn chain_id(&self) -> Result<u64, RpcError> {
        let result = self.call("eth_chainId", Value::Array(vec![])).await?;
        parse_hex_u64(&result)
    }

    /// Get the transaction receipt, or None if not yet mined.
    pub async fn get_tx_receipt(&self, tx_hash: &str) -> Result<Option<TxReceipt>, RpcError> {
        let result = self
            .call("eth_getTransactionReceipt", serde_json::json!([tx_hash]))
            .await;

        match result {
            Ok(val) => {
                let receipt: TxReceipt =
                    serde_json::from_value(val).map_err(|e| RpcError::Unexpected(e.to_string()))?;
                Ok(Some(receipt))
            }
            Err(RpcError::NullResult(_)) => Ok(None),
            Err(e) => Err(e),
        }
    }

    /// Get the nonce (transaction count) for an address.
    pub async fn get_nonce(&self, address: &str) -> Result<u64, RpcError> {
        let result = self
            .call(
                "eth_getTransactionCount",
                serde_json::json!([address, "latest"]),
            )
            .await?;
        parse_hex_u64(&result)
    }

    /// Broadcast a signed raw transaction. Returns the tx hash.
    pub async fn send_raw_tx(&self, raw_tx_hex: &str) -> Result<String, RpcError> {
        let result = self
            .call("eth_sendRawTransaction", serde_json::json!([raw_tx_hex]))
            .await?;
        result
            .as_str()
            .map(|s| s.to_string())
            .ok_or_else(|| RpcError::Unexpected("tx hash not a string".into()))
    }

    /// Execute a read-only call (eth_call). Returns the return data hex.
    pub async fn eth_call(&self, to: &str, data: &str, block: &str) -> Result<String, RpcError> {
        let result = self
            .call(
                "eth_call",
                serde_json::json!([{"to": to, "data": data}, block]),
            )
            .await?;
        result
            .as_str()
            .map(|s| s.to_string())
            .ok_or_else(|| RpcError::Unexpected("call result not a string".into()))
    }

    /// Get logs matching a filter.
    pub async fn get_logs(&self, filter: &LogFilter) -> Result<Vec<LogEntry>, RpcError> {
        let result = self
            .call("eth_getLogs", serde_json::json!([filter]))
            .await?;
        let logs: Vec<LogEntry> =
            serde_json::from_value(result).map_err(|e| RpcError::Unexpected(e.to_string()))?;
        Ok(logs)
    }

    /// Get the current base fee gas price (legacy).
    pub async fn gas_price(&self) -> Result<u64, RpcError> {
        let result = self.call("eth_gasPrice", Value::Array(vec![])).await?;
        parse_hex_u64(&result)
    }

    /// Get the suggested max priority fee (EIP-1559).
    pub async fn max_priority_fee(&self) -> Result<u64, RpcError> {
        let result = self
            .call("eth_maxPriorityFeePerGas", Value::Array(vec![]))
            .await?;
        parse_hex_u64(&result)
    }

    /// Estimate gas for a transaction.
    pub async fn estimate_gas(
        &self,
        from: &str,
        to: &str,
        data: &str,
        value: Option<&str>,
    ) -> Result<u64, RpcError> {
        let mut tx = serde_json::json!({"from": from, "to": to, "data": data});
        if let Some(v) = value {
            tx["value"] = Value::String(v.to_string());
        }
        let result = self
            .call("eth_estimateGas", serde_json::json!([tx]))
            .await?;
        parse_hex_u64(&result)
    }
}

/// Parse a hex-encoded quantity ("0x...") to u64.
fn parse_hex_u64(val: &Value) -> Result<u64, RpcError> {
    let s = val
        .as_str()
        .ok_or_else(|| RpcError::Unexpected("expected hex string".into()))?;
    let s = s.strip_prefix("0x").unwrap_or(s);
    u64::from_str_radix(s, 16).map_err(|e| RpcError::Unexpected(format!("hex parse: {}", e)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_parse_hex_u64() {
        assert_eq!(parse_hex_u64(&Value::String("0x10".into())).unwrap(), 16);
        assert_eq!(parse_hex_u64(&Value::String("0x0".into())).unwrap(), 0);
        assert_eq!(parse_hex_u64(&Value::String("0xff".into())).unwrap(), 255);
    }
}

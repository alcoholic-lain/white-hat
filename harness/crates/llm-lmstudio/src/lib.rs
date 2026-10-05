use futures_util::StreamExt;
use tokio::sync::mpsc;
use serde::{Deserialize, Serialize};
use reqwest::Client;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Message {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatRequest {
    pub messages: Vec<Message>,
    pub temperature: f32,
    pub max_tokens: i32,
    pub stream: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChatResponse {
    pub choices: Vec<Choice>,
    pub usage: Usage,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Choice {
    pub message: ChoiceMessage,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChoiceMessage {
    pub role: String,
    pub content: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Delta {
    pub content: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Usage {
    pub prompt_tokens: i32,
    pub completion_tokens: i32,
    pub total_tokens: i32,
}

pub struct LmStudioClient {
    base_url: String,
    client: Client,
    model: String,
}

impl LmStudioClient {
    pub fn new(base_url: String, model: String) -> Self {
        Self {
            base_url,
            client: Client::new(),
            model,
        }
    }

    /// Chat completion - get response from LM Studio
    pub async fn chat(&self, messages: Vec<Message>, temperature: f32, max_tokens: i32) -> Result<String, String> {
        let payload = serde_json::json!({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": false
        });

        let response = self.client
            .post(&format!("{}/v1/chat/completions", self.base_url))
            .json(&payload)
            .send()
            .await
            .map_err(|e| format!("Request failed: {}", e))?;

        if !response.status().is_success() {
            return Err(format!("API error: {}", response.status()));
        }

        let data: serde_json::Value = response.json().await
            .map_err(|e| format!("Parse error: {}", e))?;

        // Extract content from first choice
        let content = data
            .get("choices")
            .and_then(|c| c.get(0))
            .and_then(|c| c.get("message"))
            .and_then(|m| m.get("content"))
            .and_then(|c| c.as_str())
            .ok_or_else(|| "No content in response".to_string())?;

        Ok(content.to_string())
    }

    /// List available models from LM Studio
    pub async fn list_models(&self) -> Result<Vec<String>, String> {
        let response = self.client
            .get(&format!("{}/v1/models", self.base_url))
            .send()
            .await
            .map_err(|e| format!("Request failed: {}", e))?;

        if !response.status().is_success() {
            return Err(format!("API error: {}", response.status()));
        }

        let data: serde_json::Value = response.json().await
            .map_err(|e| format!("Parse error: {}", e))?;

        let models: Vec<String> = data
            .get("data")
            .and_then(|d| d.as_array())
            .ok_or_else(|| "No data in response".to_string())?
            .iter()
            .filter_map(|m| m.get("id").and_then(|id| id.as_str()).map(|s| s.to_string()))
            .collect();

        Ok(models)
    }

    /// Streaming chat completion.
    ///
    /// Returns a channel of text deltas. The channel closes when the model is done.
    /// Dropping the receiver aborts the upstream request (this is how cancellation works).
    pub async fn chat_stream(
        &self,
        messages: Vec<Message>,
        temperature: f32,
        max_tokens: i32,
    ) -> Result<mpsc::Receiver<Result<String, String>>, String> {
        let payload = serde_json::json!({
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": true
        });

        let response = self.client
            .post(&format!("{}/v1/chat/completions", self.base_url))
            .json(&payload)
            .send()
            .await
            .map_err(|e| format!("Request failed: {}", e))?;

        if !response.status().is_success() {
            let status = response.status();
            let body = response.text().await.unwrap_or_default();
            let body: String = body.chars().take(300).collect();
            return Err(format!("API error: {} {}", status, body));
        }

        let (tx, rx) = mpsc::channel(64);

        tokio::spawn(async move {
            let mut stream = response.bytes_stream();
            // Buffer raw bytes and split on '\n' so UTF-8 sequences and SSE lines
            // that straddle network chunks are reassembled before decoding.
            let mut buf: Vec<u8> = Vec::new();

            while let Some(chunk) = stream.next().await {
                match chunk {
                    Ok(bytes) => buf.extend_from_slice(&bytes),
                    Err(e) => {
                        let _ = tx.send(Err(format!("Stream error: {}", e))).await;
                        return;
                    }
                }

                while let Some(pos) = buf.iter().position(|&b| b == b'\n') {
                    let line_bytes: Vec<u8> = buf.drain(..=pos).collect();
                    let line = String::from_utf8_lossy(&line_bytes);
                    let Some(data) = line.trim().strip_prefix("data:") else {
                        continue;
                    };
                    let data = data.trim();
                    if data == "[DONE]" {
                        return;
                    }
                    let Ok(value) = serde_json::from_str::<serde_json::Value>(data) else {
                        continue;
                    };
                    if let Some(text) = value["choices"][0]["delta"]["content"].as_str() {
                        if !text.is_empty() && tx.send(Ok(text.to_string())).await.is_err() {
                            return; // receiver dropped: stop reading, closing the upstream connection
                        }
                    }
                }
            }
        });

        Ok(rx)
    }
}
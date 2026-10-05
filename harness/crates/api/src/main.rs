use axum::{
    extract::Json,
    response::sse::{Event, KeepAlive, Sse},
    routing::{get, post},
    Router,
};
use harness_llm_lmstudio::{ChatRequest, LmStudioClient, Message};
use serde_json::json;
use std::convert::Infallible;
use std::net::SocketAddr;
use std::sync::Arc;
use tokio_stream::wrappers::ReceiverStream;
use tokio_stream::{Stream, StreamExt};

struct AppState {
    llm_client: LmStudioClient,
}

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    // Initialize LM Studio client
    let llm_client = LmStudioClient::new(
        "http://localhost:1234".to_string(),
        "gemma-4-31b-it-heretic-i1".to_string(),
    );

    let state = Arc::new(AppState { llm_client });

    let app = Router::new()
        .route("/status", get(status_handler))
        .route("/models", get(models_handler))
        .route("/chat", post(chat_handler))
        .route("/chat/stream", post(chat_stream_handler))
        .with_state(state);

    let addr = SocketAddr::from(([127, 0, 0, 1], 8787));
    tracing::info!("Listening on {}", addr);

    let listener = tokio::net::TcpListener::bind(addr)
        .await
        .expect("Failed to bind");

    axum::serve(listener, app)
        .await
        .expect("Server error");
}

async fn status_handler() -> Json<serde_json::Value> {
    Json(json!({"status": "ok", "version": "0.1.0"}))
}

async fn models_handler(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
) -> Json<serde_json::Value> {
    match state.llm_client.list_models().await {
        Ok(models) => Json(json!({"models": models})),
        Err(e) => Json(json!({"error": e})),
    }
}

async fn chat_handler(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    Json(payload): Json<ChatRequest>,
) -> Json<serde_json::Value> {
    match state.llm_client.chat(payload.messages, payload.temperature, payload.max_tokens).await {
        Ok(content) => Json(json!({"response": content})),
        Err(e) => Json(json!({"error": e})),
    }
}

fn sse_event(value: serde_json::Value) -> Event {
    Event::default().data(value.to_string())
}

/// Streaming chat. Emits SSE events in the spec's shape:
/// {"type":"token","text":"..."}, then {"type":"final","text":"<full>"}
/// or {"type":"error","code":"...","message":"..."}.
async fn chat_stream_handler(
    axum::extract::State(state): axum::extract::State<Arc<AppState>>,
    Json(payload): Json<ChatRequest>,
) -> Sse<impl Stream<Item = Result<Event, Infallible>>> {
    let (tx, rx) = tokio::sync::mpsc::channel::<Event>(64);

    tokio::spawn(async move {
        let messages: Vec<Message> = payload.messages;
        let mut tokens = match state
            .llm_client
            .chat_stream(messages, payload.temperature, payload.max_tokens)
            .await
        {
            Ok(rx) => rx,
            Err(e) => {
                let code = if e.starts_with("Request failed") { "lm_unreachable" } else { "lm_error" };
                let _ = tx
                    .send(sse_event(json!({"type": "error", "code": code, "message": e})))
                    .await;
                return;
            }
        };

        let mut full = String::new();
        while let Some(item) = tokens.recv().await {
            match item {
                Ok(text) => {
                    full.push_str(&text);
                    // If the client went away, stop; dropping `tokens` aborts the upstream call.
                    if tx
                        .send(sse_event(json!({"type": "token", "text": text})))
                        .await
                        .is_err()
                    {
                        return;
                    }
                }
                Err(e) => {
                    let _ = tx
                        .send(sse_event(json!({"type": "error", "code": "lm_error", "message": e})))
                        .await;
                    return;
                }
            }
        }

        let _ = tx
            .send(sse_event(json!({"type": "final", "text": full})))
            .await;
    });

    Sse::new(ReceiverStream::new(rx).map(Ok::<Event, Infallible>)).keep_alive(KeepAlive::default())
}

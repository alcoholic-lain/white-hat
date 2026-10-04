use axum::{
    routing::get,
    Router,
    Json,
};
use serde_json::json;
use std::net::SocketAddr;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt::init();

    let app = Router::new()
        .route("/status", get(status_handler));

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
    Json(json!({
        "status": "ok",
        "version": "0.1.0"
    }))
}

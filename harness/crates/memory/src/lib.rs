use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use sqlx::sqlite::{SqlitePool, SqlitePoolOptions};
use uuid::Uuid;

/// Session record - tracks conversation sessions
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Session {
    pub id: String,
    pub client: String,                      // "discord", "web", etc.
    pub external_id: String,                 // Discord user ID, etc.
    pub user_id: Option<String>,
    pub model: String,                       // Selected LM Studio model
    pub system_prompt: Option<String>,
    pub params_json: Option<String>,         // JSON serialized parameters
    pub mode: String,                        // "chat", "plan", etc.
    pub summary: Option<String>,
    pub created_at: i64,                     // Unix timestamp
}

/// Message record - individual messages in a session
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Message {
    pub id: String,
    pub session_id: String,
    pub role: String,                        // "user", "assistant", "system"
    pub content: String,
    pub tool_calls_json: Option<String>,     // JSON serialized tool calls
    pub created_at: i64,                     // Unix timestamp
}

/// Run record - tracks agent execution runs
#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct Run {
    pub id: String,
    pub session_id: String,
    pub mode: String,                        // "chat", "agent", "compiler"
    pub rounds: i32,
    pub status: String,                      // "pending", "running", "complete", "error"
    pub tokens: i32,
    pub llm_calls: i32,
    pub started_at: i64,                     // Unix timestamp
    pub finished_at: Option<i64>,            // Unix timestamp, null if not complete
}

pub struct MemoryDb {
    pool: SqlitePool,
}

impl MemoryDb {
    /// Initialize database with connection pool and schema
    pub async fn new(database_url: &str) -> Result<Self, sqlx::Error> {
        let pool = SqlitePoolOptions::new()
            .max_connections(5)
            .connect(database_url)
            .await?;

        // Create tables if they don't exist
        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                client TEXT NOT NULL,
                external_id TEXT NOT NULL,
                user_id TEXT,
                model TEXT NOT NULL,
                system_prompt TEXT,
                params_json TEXT,
                mode TEXT NOT NULL,
                summary TEXT,
                created_at INTEGER NOT NULL
            )
            "#,
        )
        .execute(&pool)
        .await?;

        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                tool_calls_json TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
            "#,
        )
        .execute(&pool)
        .await?;

        sqlx::query(
            r#"
            CREATE TABLE IF NOT EXISTS runs (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                mode TEXT NOT NULL,
                rounds INTEGER NOT NULL,
                status TEXT NOT NULL,
                tokens INTEGER NOT NULL,
                llm_calls INTEGER NOT NULL,
                started_at INTEGER NOT NULL,
                finished_at INTEGER,
                FOREIGN KEY(session_id) REFERENCES sessions(id)
            )
            "#,
        )
        .execute(&pool)
        .await?;

        tracing::info!("Database initialized successfully");

        Ok(Self { pool })
    }

    /// Create a new session
    pub async fn create_session(
        &self,
        client: &str,
        external_id: &str,
        model: &str,
    ) -> Result<Session, sqlx::Error> {
        let id = Uuid::new_v4().to_string();
        let now = Utc::now().timestamp();

        let session = Session {
            id: id.clone(),
            client: client.to_string(),
            external_id: external_id.to_string(),
            user_id: None,
            model: model.to_string(),
            system_prompt: None,
            params_json: None,
            mode: "chat".to_string(),
            summary: None,
            created_at: now,
        };

        sqlx::query(
            "INSERT INTO sessions (id, client, external_id, user_id, model, system_prompt, params_json, mode, summary, created_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        .bind(&session.id)
        .bind(&session.client)
        .bind(&session.external_id)
        .bind(&session.user_id)
        .bind(&session.model)
        .bind(&session.system_prompt)
        .bind(&session.params_json)
        .bind(&session.mode)
        .bind(&session.summary)
        .bind(&session.created_at)
        .execute(&self.pool)
        .await?;

        Ok(session)
    }

    /// Get a session by ID
    pub async fn get_session(&self, id: &str) -> Result<Option<Session>, sqlx::Error> {
        sqlx::query_as::<_, Session>("SELECT * FROM sessions WHERE id = ?")
            .bind(id)
            .fetch_optional(&self.pool)
            .await
    }

    /// Add a message to a session
    pub async fn add_message(
        &self,
        session_id: &str,
        role: &str,
        content: &str,
    ) -> Result<Message, sqlx::Error> {
        let id = Uuid::new_v4().to_string();
        let now = Utc::now().timestamp();

        let message = Message {
            id: id.clone(),
            session_id: session_id.to_string(),
            role: role.to_string(),
            content: content.to_string(),
            tool_calls_json: None,
            created_at: now,
        };

        sqlx::query(
            "INSERT INTO messages (id, session_id, role, content, tool_calls_json, created_at)
             VALUES (?, ?, ?, ?, ?, ?)"
        )
        .bind(&message.id)
        .bind(&message.session_id)
        .bind(&message.role)
        .bind(&message.content)
        .bind(&message.tool_calls_json)
        .bind(&message.created_at)
        .execute(&self.pool)
        .await?;

        Ok(message)
    }

    /// Get all messages for a session
    pub async fn get_session_messages(&self, session_id: &str) -> Result<Vec<Message>, sqlx::Error> {
        sqlx::query_as::<_, Message>("SELECT * FROM messages WHERE session_id = ? ORDER BY created_at")
            .bind(session_id)
            .fetch_all(&self.pool)
            .await
    }

    /// Create a new run
    pub async fn create_run(&self, session_id: &str, mode: &str) -> Result<Run, sqlx::Error> {
        let id = Uuid::new_v4().to_string();
        let now = Utc::now().timestamp();

        let run = Run {
            id: id.clone(),
            session_id: session_id.to_string(),
            mode: mode.to_string(),
            rounds: 0,
            status: "pending".to_string(),
            tokens: 0,
            llm_calls: 0,
            started_at: now,
            finished_at: None,
        };

        sqlx::query(
            "INSERT INTO runs (id, session_id, mode, rounds, status, tokens, llm_calls, started_at, finished_at)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        .bind(&run.id)
        .bind(&run.session_id)
        .bind(&run.mode)
        .bind(&run.rounds)
        .bind(&run.status)
        .bind(&run.tokens)
        .bind(&run.llm_calls)
        .bind(&run.started_at)
        .bind(&run.finished_at)
        .execute(&self.pool)
        .await?;

        Ok(run)
    }

    /// Get a run by ID
    pub async fn get_run(&self, id: &str) -> Result<Option<Run>, sqlx::Error> {
        sqlx::query_as::<_, Run>("SELECT * FROM runs WHERE id = ?")
            .bind(id)
            .fetch_optional(&self.pool)
            .await
    }

    /// Update run status
    pub async fn update_run_status(&self, id: &str, status: &str) -> Result<(), sqlx::Error> {
        sqlx::query("UPDATE runs SET status = ? WHERE id = ?")
            .bind(status)
            .bind(id)
            .execute(&self.pool)
            .await?;

        Ok(())
    }
}

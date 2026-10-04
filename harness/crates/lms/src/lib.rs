use serde::{Deserialize, Serialize};
use std::process::Command;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Model {
    pub name: String,
    pub identifier: Option<String>,
}

#[derive(Debug)]
pub enum LmsError {
    NotFound,
    ParseError(String),
    CommandError(String),
}

pub struct LmsManager;

impl LmsManager {
    /// List all downloaded models
    pub fn list_models() -> Result<Vec<Model>, LmsError> {
        // Stub for P1 - to be implemented
        Ok(vec![])
    }

    /// List loaded models
    pub fn list_loaded() -> Result<Vec<Model>, LmsError> {
        // Stub for P1 - to be implemented
        Ok(vec![])
    }
}

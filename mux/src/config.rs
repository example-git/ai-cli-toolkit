use std::collections::HashMap;
use std::fs;
use std::path::Path;

use serde::Deserialize;

#[derive(Debug, Deserialize)]
pub struct MuxConfig {
    pub tabs: Vec<TabDef>,
    #[serde(default)]
    pub session_name: Option<String>,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TabDef {
    pub label: String,
    pub cmd: Vec<String>,
    #[serde(default)]
    pub env: HashMap<String, String>,
    pub cwd: Option<String>,
    #[serde(default)]
    pub primary: bool,
}

impl MuxConfig {
    pub fn from_path(path: &Path) -> Result<Self, String> {
        let raw = fs::read_to_string(path)
            .map_err(|e| format!("failed reading config {}: {e}", path.display()))?;
        let parsed: MuxConfig = serde_json::from_str(&raw)
            .map_err(|e| format!("failed parsing config {}: {e}", path.display()))?;
        if parsed.tabs.is_empty() {
            return Err("config must contain at least one tab".to_string());
        }
        Ok(parsed)
    }
}

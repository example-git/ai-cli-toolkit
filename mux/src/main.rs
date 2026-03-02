mod config;

use std::collections::hash_map::DefaultHasher;
use std::fs;
use std::hash::{Hash, Hasher};
use std::path::PathBuf;
use std::process::Command;

use config::MuxConfig;

const TMUX_SOCKET: &str = "ai-mux";

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(err) => {
            eprintln!("ai-mux: {err}");
            std::process::exit(1);
        }
    }
}

fn run() -> Result<i32, String> {
    let (config_path, session_name_override) = parse_args(std::env::args().skip(1).collect())?;
    let config = MuxConfig::from_path(&config_path)?;

    let session_name = if let Some(name) = session_name_override {
        sanitize_session_name(&name)
            .ok_or_else(|| "invalid --session-name".to_string())?
    } else {
        derive_session_name(&config)
    };

    // Check if session already exists
    if session_exists(&session_name) {
        return attach_session(&session_name);
    }

    // Generate tmux config
    let tmux_conf = generate_tmux_conf(&config);
    let conf_path = write_tmux_conf(&tmux_conf, &session_name)?;

    // Create new session with first tab
    let first = &config.tabs[0];
    let shell_cmd = build_env_cmd(&first.env, &first.cmd);
    let win_name = window_name_with_hint(&first.label, 0);
    let mut args = vec![
        "-L", TMUX_SOCKET,
        "-f", conf_path.to_str().unwrap(),
        "new-session", "-d",
        "-s", &session_name,
        "-n", &win_name,
    ];
    if let Some(ref cwd) = first.cwd {
        args.extend_from_slice(&["-c", cwd]);
    }
    args.push(&shell_cmd);

    let status = Command::new("tmux")
        .args(&args)
        .status()
        .map_err(|e| format!("tmux new-session failed: {e}"))?;
    if !status.success() {
        return Err("tmux new-session failed".into());
    }

    // Set session-level env vars from the primary tab so they propagate
    // to all windows (tmux set-environment affects new processes in the session).
    for (k, v) in &first.env {
        let _ = Command::new("tmux")
            .args(["-L", TMUX_SOCKET, "set-environment", "-t", &session_name, k, v])
            .status();
    }

    // Add remaining tabs as new windows
    for (i, tab) in config.tabs.iter().enumerate().skip(1) {
        let shell_cmd = build_env_cmd(&tab.env, &tab.cmd);
        let win_name = window_name_with_hint(&tab.label, i);
        let mut args = vec![
            "-L", TMUX_SOCKET,
            "new-window", "-t", &session_name,
            "-n", &win_name,
        ];
        if let Some(ref cwd) = tab.cwd {
            args.extend_from_slice(&["-c", cwd]);
        }
        args.push(&shell_cmd);

        let status = Command::new("tmux")
            .args(&args)
            .status();
        if let Err(e) = status {
            return Err(format!("tmux new-window '{}' failed: {e}", tab.label));
        }
    }

    // Select first window
    let _ = Command::new("tmux")
        .args(["-L", TMUX_SOCKET, "select-window", "-t", &format!("{session_name}:0")])
        .status();

    // Attach
    attach_session(&session_name)
}

fn session_exists(name: &str) -> bool {
    Command::new("tmux")
        .args(["-L", TMUX_SOCKET, "has-session", "-t", name])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn attach_session(name: &str) -> Result<i32, String> {
    let status = Command::new("tmux")
        .args(["-L", TMUX_SOCKET, "attach-session", "-t", name])
        .status()
        .map_err(|e| format!("tmux attach failed: {e}"))?;
    Ok(status.code().unwrap_or(0))
}

fn derive_session_name(config: &MuxConfig) -> String {
    if let Some(name) = config.session_name.as_deref().and_then(sanitize_session_name) {
        return name;
    }

    for tab in &config.tabs {
        if tab.primary {
            if let Some(session_id) = tab.env.get("AI_CLI_SESSION") {
                let mut cleaned = String::new();
                for ch in session_id.chars() {
                    if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
                        cleaned.push(ch);
                    } else {
                        cleaned.push('-');
                    }
                }
                if let Some(cleaned) = sanitize_session_name(&format!("ai-{cleaned}")) {
                    return cleaned;
                }
            }
        }
    }

    let mut h = DefaultHasher::new();
    for tab in &config.tabs {
        if tab.primary {
            tab.label.hash(&mut h);
            tab.cwd.hash(&mut h);
        }
    }
    format!("ai-{:08x}", h.finish() as u32)
}

fn sanitize_session_name(name: &str) -> Option<String> {
    let mut cleaned = String::new();
    for ch in name.chars() {
        if ch.is_ascii_alphanumeric() || ch == '-' || ch == '_' {
            cleaned.push(ch);
        } else {
            cleaned.push('-');
        }
    }
    let cleaned = cleaned.trim_matches('-');
    if cleaned.is_empty() {
        None
    } else {
        Some(cleaned.to_string())
    }
}

/// Build a shell command string with env vars prepended.
/// Produces: `env K1=V1 K2=V2 ... cmd arg1 arg2`
fn build_env_cmd(env: &std::collections::HashMap<String, String>, cmd: &[String]) -> String {
    let mut parts = Vec::new();
    if !env.is_empty() {
        parts.push("env".to_string());
        let mut sorted: Vec<_> = env.iter().collect();
        sorted.sort_by_key(|(k, _)| k.as_str());
        for (k, v) in sorted {
            parts.push(format!("{}={}", shell_escape(k), shell_escape(v)));
        }
    }
    for arg in cmd {
        parts.push(shell_escape(arg));
    }
    parts.join(" ")
}

fn shell_escape(s: &str) -> String {
    if s.is_empty() {
        return "''".to_string();
    }
    if s.bytes().all(|b| matches!(b, b'a'..=b'z' | b'A'..=b'Z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'/' | b':' | b'=' | b'+' | b',' | b'@')) {
        return s.to_string();
    }
    format!("'{}'", s.replace('\'', "'\\''"))
}

fn generate_tmux_conf(_config: &MuxConfig) -> String {
    let mut conf = String::new();

    // Core settings
    conf.push_str("# ai-mux tmux configuration (auto-generated, do not edit)\n");
    conf.push_str("# User overrides: ~/.config/ai-cli/tmux.conf\n\n");
    conf.push_str("set -g mouse on\n");
    conf.push_str("set -g status-position top\n");
    conf.push_str("set -sg escape-time 0\n");
    conf.push_str("set -g default-terminal 'screen-256color'\n");
    conf.push_str("set -g focus-events on\n");
    conf.push_str("set -g history-limit 50000\n");
    conf.push_str("set -g remain-on-exit off\n");
    conf.push_str("set -g renumber-windows off\n");
    conf.push_str("set -g base-index 0\n");
    // Lock window names so the shell can't rename them
    conf.push_str("set -g allow-rename off\n");
    conf.push_str("set -g automatic-rename off\n");

    // Status bar styling — dark background, matches original tab bar
    conf.push_str("\n# Status bar\n");
    conf.push_str("set -g status-style 'bg=#333333,fg=white,dim'\n");
    conf.push_str("set -g status-left ''\n");
    conf.push_str("set -g status-right ''\n");
    conf.push_str("set -g status-left-length 0\n");
    conf.push_str("set -g status-right-length 0\n");
    // Key hints are baked into window names (e.g. "claude(F2/M-2)")
    conf.push_str("set -g window-status-format ' #W '\n");
    conf.push_str("set -g window-status-current-format '#[bold,noreverse] #W '\n");
    conf.push_str("set -g window-status-current-style 'bg=default,fg=white,bold'\n");
    conf.push_str("set -g window-status-style 'bg=#333333,fg=#999999'\n");
    conf.push_str("set -g window-status-separator ''\n");

    // Unbind default prefix — use C-] to stay out of the way
    conf.push_str("\n# Use C-] as prefix (avoids conflicts with tools)\n");
    conf.push_str("unbind C-b\n");
    conf.push_str("set -g prefix C-]\n");
    conf.push_str("bind C-] send-prefix\n");

    // Key bindings — no prefix required
    conf.push_str("\n# Key bindings (no prefix)\n");
    conf.push_str("bind -n F2 select-window -t :0\n");
    conf.push_str("bind -n F3 select-window -t :1\n");
    conf.push_str("bind -n F4 select-window -t :2\n");
    conf.push_str("bind -n F5 select-window -t :3\n");
    conf.push_str("bind -n F6 select-window -t :4\n");
    conf.push_str("bind -n F10 select-window -t :5\n");
    conf.push_str("bind -n F11 select-window -t :6\n");
    conf.push_str("bind -n F9 select-window -t :7\n");

    let edit_global_cmd = "sh -lc 'f=\"${AI_CLI_GLOBAL_PROMPT_FILE:-$HOME/.ai-cli/system_instructions.txt}\"; d=\"$(dirname \"$f\")\"; mkdir -p \"$d\"; [ -f \"$f\" ] || : > \"$f\"; ed=\"${VISUAL:-${EDITOR:-}}\"; if [ -z \"$ed\" ]; then for c in nano vi vim; do if command -v \"$c\" >/dev/null 2>&1; then ed=\"$c\"; break; fi; done; fi; if [ -z \"$ed\" ]; then echo \"No editor found (set VISUAL/EDITOR)\"; read -r _; exit 1; fi; exec $ed \"$f\"'";
    let edit_tool_cmd = "sh -lc 'f=\"${AI_CLI_TOOL_PROMPT_FILE:-$HOME/.ai-cli/instructions/${AI_CLI_TOOL:-codex}.txt}\"; d=\"$(dirname \"$f\")\"; mkdir -p \"$d\"; [ -f \"$f\" ] || : > \"$f\"; ed=\"${VISUAL:-${EDITOR:-}}\"; if [ -z \"$ed\" ]; then for c in nano vi vim; do if command -v \"$c\" >/dev/null 2>&1; then ed=\"$c\"; break; fi; done; fi; if [ -z \"$ed\" ]; then echo \"No editor found (set VISUAL/EDITOR)\"; read -r _; exit 1; fi; exec $ed \"$f\"'";
    conf.push_str(&format!(
        "bind -n F7 new-window -n edit-global {}\n",
        shell_escape(edit_global_cmd)
    ));
    conf.push_str(&format!(
        "bind -n F8 new-window -n edit-tool {}\n",
        shell_escape(edit_tool_cmd)
    ));

    conf.push_str("bind -n M-2 select-window -t :0\n");
    conf.push_str("bind -n M-3 select-window -t :1\n");
    conf.push_str("bind -n M-4 select-window -t :2\n");
    conf.push_str("bind -n M-5 select-window -t :3\n");
    conf.push_str("bind -n M-6 select-window -t :4\n");
    conf.push_str("bind -n M-7 select-window -t :5\n");
    conf.push_str("bind -n M-8 select-window -t :6\n");
    conf.push_str("bind -n M-9 select-window -t :7\n");

    conf.push_str("bind -n M-1 choose-tree -s\n");
    conf.push_str("bind -n F1 choose-tree -s\n");

    conf.push_str("bind -n C-n next-window\n");
    conf.push_str("bind -n C-p previous-window\n");

    conf.push_str("bind -n M-Left previous-window\n");
    conf.push_str("bind -n M-Right next-window\n");

    conf.push_str("bind q detach-client\n");

    // Source user overrides if present (always last so they win)
    conf.push_str("\n# User overrides\n");
    conf.push_str("if-shell 'test -f ~/.config/ai-cli/tmux.conf' 'source-file ~/.config/ai-cli/tmux.conf'\n");

    conf
}

/// Window name with embedded key hint, e.g. "claude(F2/M-2)"
fn window_name_with_hint(label: &str, index: usize) -> String {
    let alt = index + 2; // M-2 for window 0, M-3 for window 1, etc.
    let fkey = match index {
        0 => Some(2),
        1 => Some(3),
        2 => Some(4),
        3 => Some(5),
        4 => Some(6),
        7 => Some(9),
        _ => None,
    };
    if let Some(key) = fkey {
        format!("{label}(F{key}/M-{alt})")
    } else {
        format!("{label}(M-{alt})")
    }
}

fn write_tmux_conf(conf: &str, session_name: &str) -> Result<PathBuf, String> {
    let dir = std::env::temp_dir().join("ai-mux-tmux");
    let _ = fs::create_dir_all(&dir);
    // Per-session config file so concurrent launches don't race
    let path = dir.join(format!("{session_name}.conf"));
    fs::write(&path, conf).map_err(|e| format!("failed writing tmux.conf: {e}"))?;
    Ok(path)
}

fn parse_args(args: Vec<String>) -> Result<(PathBuf, Option<String>), String> {
    if args.len() == 2 && args[0] == "--config" {
        return Ok((PathBuf::from(&args[1]), None));
    }
    if args.len() == 4 && args[0] == "--config" && args[2] == "--session-name" {
        return Ok((PathBuf::from(&args[1]), Some(args[3].clone())));
    }
    Err("usage: ai-mux --config <path> [--session-name <name>]".to_string())
}

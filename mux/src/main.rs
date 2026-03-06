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
    let (config_path, session_name_override, socket_name) =
        parse_args(std::env::args().skip(1).collect())?;
    let config = MuxConfig::from_path(&config_path)?;

    let session_name = if let Some(name) = session_name_override {
        sanitize_session_name(&name)
            .ok_or_else(|| "invalid --session-name".to_string())?
    } else {
        derive_session_name(&config)
    };

    // Generate tmux config
    let tmux_conf = generate_tmux_conf(&config, &socket_name);
    let conf_path = write_tmux_conf(&tmux_conf, &session_name)?;

    // Check if session already exists
    if session_exists(&session_name, &socket_name) {
        // Kill all other clients attached to this session so input isn't stolen
        detach_other_clients(&session_name, &socket_name);

        let _ = Command::new("tmux")
            .args(["-L", &socket_name, "source-file", conf_path.to_str().unwrap()])
            .status();

        if let Some(first) = config.tabs.first() {
            for (k, v) in &first.env {
                let _ = Command::new("tmux")
                    .args(["-L", &socket_name, "set-environment", "-t", &session_name, k, v])
                    .status();
            }
        }
        return attach_session(&session_name, &socket_name);
    }

    // Create new session with first tab
    let first = &config.tabs[0];
    let shell_cmd = build_env_cmd(&first.env, &first.cmd);
    let win_name = window_name_with_hint(&first.label, 0);
    let mut args = vec![
        "-L", &socket_name,
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
            .args(["-L", &socket_name, "set-environment", "-t", &session_name, k, v])
            .status();
    }

    // Add remaining tabs as new windows
    for (i, tab) in config.tabs.iter().enumerate().skip(1) {
        let shell_cmd = build_env_cmd(&tab.env, &tab.cmd);
        let win_name = window_name_with_hint(&tab.label, i);
        let mut args = vec![
            "-L", &socket_name,
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
        .args(["-L", &socket_name, "select-window", "-t", &format!("{session_name}:0")])
        .status();

    // Attach
    attach_session(&session_name, &socket_name)
}

fn session_exists(name: &str, socket_name: &str) -> bool {
    Command::new("tmux")
        .args(["-L", socket_name, "has-session", "-t", name])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

/// Detach (and kill the client connection for) every client currently
/// attached to the given session so the new attach gets exclusive input.
fn detach_other_clients(session_name: &str, socket_name: &str) {
    let output = Command::new("tmux")
        .args(["-L", socket_name, "list-clients", "-t", session_name, "-F", "#{client_tty}"])
        .stdout(std::process::Stdio::piped())
        .stderr(std::process::Stdio::null())
        .output();
    if let Ok(out) = output {
        let ttys = String::from_utf8_lossy(&out.stdout);
        for tty in ttys.lines() {
            let tty = tty.trim();
            if !tty.is_empty() {
                let _ = Command::new("tmux")
                    .args(["-L", socket_name, "detach-client", "-t", tty])
                    .stdout(std::process::Stdio::null())
                    .stderr(std::process::Stdio::null())
                    .status();
            }
        }
    }
}

fn attach_session(name: &str, socket_name: &str) -> Result<i32, String> {
    let status = Command::new("tmux")
        .args(["-L", socket_name, "attach-session", "-t", name])
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

fn editor_launcher_cmd(window_name: &str, target: &str, socket_name: &str) -> String {
    let launcher_fallback = format!(
        "{}/.ai-cli/bin/ai-prompt-editor",
        std::env::var("HOME").unwrap_or_else(|_| "~".to_string())
    );
    format!(
        "{launcher_fallback} open --tmux-socket {socket_name} --window-name {window_name} --target {target}"
    )
}

fn editor_binding(key: &str, editor_cmd: &str) -> String {
    let guard = "#{m:^edit-(global|base|tool|project)$,#{window_name}}";
    format!(
        "bind -n {key} if-shell -F {} {{ run-shell true }} {{ run-shell -b {} }}\n",
        shell_escape(guard),
        shell_escape(editor_cmd)
    )
}

fn generate_tmux_conf(_config: &MuxConfig, socket_name: &str) -> String {
    let mut conf = String::new();

    // Core settings
    conf.push_str("# ai-mux tmux configuration (auto-generated, do not edit)\n");
    conf.push_str("# User overrides: ~/.config/ai-cli/tmux.conf\n\n");
    conf.push_str("set -g mouse on\n");
    conf.push_str("set -g status-position top\n");
    conf.push_str("set -sg escape-time 0\n");
    conf.push_str("set -g xterm-keys on\n");
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
    conf.push_str("set -g status-right '#[fg=#ffffff,bold] F5:global F6:base F7:tool F8:project │ C-] prefix '\n");
    conf.push_str("set -g status-left-length 0\n");
    conf.push_str("set -g status-right-length 60\n");
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
    conf.push_str("bind -n F10 select-window -t :5\n");
    conf.push_str("bind -n F11 select-window -t :6\n");
    conf.push_str("bind -n F9 select-window -t :7\n");

    let edit_global_cmd = editor_launcher_cmd("edit-global", "global", socket_name);
    let edit_base_cmd = editor_launcher_cmd("edit-base", "base", socket_name);
    let edit_tool_cmd = editor_launcher_cmd("edit-tool", "tool", socket_name);
    let edit_project_cmd = editor_launcher_cmd("edit-project", "project", socket_name);
    conf.push_str(&editor_binding("F5", &edit_global_cmd));
    conf.push_str(&editor_binding("F6", &edit_base_cmd));
    conf.push_str(&editor_binding("F7", &edit_tool_cmd));
    conf.push_str(&editor_binding("F8", &edit_project_cmd));

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
        5 => Some(10),
        6 => Some(11),
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

fn parse_args(args: Vec<String>) -> Result<(PathBuf, Option<String>, String), String> {
    let mut config_path: Option<PathBuf> = None;
    let mut session_name: Option<String> = None;
    let mut socket_name = TMUX_SOCKET.to_string();
    let mut i = 0;

    while i < args.len() {
        match args[i].as_str() {
            "--config" if i + 1 < args.len() => {
                config_path = Some(PathBuf::from(&args[i + 1]));
                i += 2;
            }
            "--session-name" if i + 1 < args.len() => {
                session_name = Some(args[i + 1].clone());
                i += 2;
            }
            "--socket-name" if i + 1 < args.len() => {
                socket_name = args[i + 1].clone();
                i += 2;
            }
            _ => {
                return Err(
                    "usage: ai-mux --config <path> [--session-name <name>] [--socket-name <name>]"
                        .to_string(),
                )
            }
        }
    }

    if let Some(path) = config_path {
        return Ok((path, session_name, socket_name));
    }
    Err("usage: ai-mux --config <path> [--session-name <name>] [--socket-name <name>]".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_tmux_conf_uses_requested_socket_for_editor_bindings() {
        let config = MuxConfig {
            session_name: Some("codex-test".to_string()),
            tabs: vec![],
        };

        let conf = generate_tmux_conf(&config, "ai-cli-codex");

        assert!(conf.contains("--tmux-socket ai-cli-codex --window-name edit-global"));
        assert!(conf.contains("--tmux-socket ai-cli-codex --window-name edit-project"));
        assert!(!conf.contains("--tmux-socket ai-mux --window-name edit-global"));
    }
}

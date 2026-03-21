mod config;

use crossterm::cursor::{Hide, MoveTo, Show};
use crossterm::event::{read, Event, KeyCode, KeyEvent, KeyModifiers};
use crossterm::execute;
use crossterm::queue;
use crossterm::style::{Attribute, Color, Print, ResetColor, SetAttribute, SetForegroundColor};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, size, Clear, ClearType, EnterAlternateScreen,
    LeaveAlternateScreen,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::hash_map::DefaultHasher;
use std::fs;
use std::hash::{Hash, Hasher};
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::Command;

use config::MuxConfig;

const TMUX_SOCKET: &str = "ai-mux";
const CODEX_PERSONALITY_WINDOW: &str = "codex-personality";

fn main() {
    match run() {
        Ok(code) => std::process::exit(code),
        Err(err) => {
            eprintln!("ai-mux: {err}");
            std::process::exit(1);
        }
    }
}

#[derive(Debug)]
enum CliCommand {
    Run {
        config_path: PathBuf,
        session_name_override: Option<String>,
        socket_name: String,
    },
    CodexPersonalityOpen {
        file: Option<PathBuf>,
        window_name: String,
        tmux_socket: Option<String>,
    },
    CodexPersonalityMenu {
        file: Option<PathBuf>,
        window_name: String,
        tmux_socket: Option<String>,
        lock_file: PathBuf,
        lock_token: String,
    },
}

#[derive(Clone, Debug, Default, PartialEq, Eq)]
struct PersonalitySections {
    personality: String,
    interaction_style: String,
    escalation: String,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
struct MenuLock {
    file: String,
    pid: u32,
    token: String,
    window_name: String,
}

#[derive(Clone, Debug)]
struct TextBuffer {
    lines: Vec<String>,
    row: usize,
    col: usize,
}

#[derive(Clone, Debug)]
struct WrappedDisplayLine {
    text: String,
    source_row: usize,
    start_col: usize,
    end_col: usize,
}

impl TextBuffer {
    fn from_text(text: &str) -> Self {
        let mut lines: Vec<String> = text.split('\n').map(|line| line.to_string()).collect();
        if lines.is_empty() {
            lines.push(String::new());
        }
        let row = lines.len().saturating_sub(1);
        let col = lines[row].chars().count();
        Self { lines, row, col }
    }

    fn to_text(&self) -> String {
        self.lines.join("\n")
    }

    fn current_line_len(&self) -> usize {
        self.lines
            .get(self.row)
            .map(|line| line.chars().count())
            .unwrap_or(0)
    }

    fn insert_char(&mut self, ch: char) {
        let line = self.lines.get_mut(self.row).expect("row in range");
        let idx = char_to_byte_index(line, self.col);
        line.insert(idx, ch);
        self.col += 1;
    }

    fn insert_newline(&mut self) {
        let current = self.lines.get_mut(self.row).expect("row in range");
        let idx = char_to_byte_index(current, self.col);
        let rest = current.split_off(idx);
        self.row += 1;
        self.col = 0;
        self.lines.insert(self.row, rest);
    }

    fn backspace(&mut self) {
        if self.col > 0 {
            let line = self.lines.get_mut(self.row).expect("row in range");
            let end = char_to_byte_index(line, self.col);
            let start = char_to_byte_index(line, self.col - 1);
            line.replace_range(start..end, "");
            self.col -= 1;
            return;
        }
        if self.row == 0 {
            return;
        }
        let current = self.lines.remove(self.row);
        self.row -= 1;
        let prev = self.lines.get_mut(self.row).expect("row in range");
        self.col = prev.chars().count();
        prev.push_str(&current);
    }

    fn move_left(&mut self) {
        if self.col > 0 {
            self.col -= 1;
        } else if self.row > 0 {
            self.row -= 1;
            self.col = self.current_line_len();
        }
    }

    fn move_right(&mut self) {
        let line_len = self.current_line_len();
        if self.col < line_len {
            self.col += 1;
        } else if self.row + 1 < self.lines.len() {
            self.row += 1;
            self.col = 0;
        }
    }

    fn move_up(&mut self) {
        if self.row > 0 {
            self.row -= 1;
            self.col = self.col.min(self.current_line_len());
        }
    }

    fn move_down(&mut self) {
        if self.row + 1 < self.lines.len() {
            self.row += 1;
            self.col = self.col.min(self.current_line_len());
        }
    }

    fn move_home(&mut self) {
        self.col = 0;
    }

    fn move_end(&mut self) {
        self.col = self.current_line_len();
    }
}

fn char_to_byte_index(text: &str, char_idx: usize) -> usize {
    if char_idx == 0 {
        return 0;
    }
    text.char_indices()
        .nth(char_idx)
        .map(|(idx, _)| idx)
        .unwrap_or_else(|| text.len())
}

fn run() -> Result<i32, String> {
    match parse_args(std::env::args().skip(1).collect())? {
        CliCommand::Run {
            config_path,
            session_name_override,
            socket_name,
        } => run_mux(config_path, session_name_override, socket_name),
        CliCommand::CodexPersonalityOpen {
            file,
            window_name,
            tmux_socket,
        } => open_codex_personality_menu(file, window_name, tmux_socket),
        CliCommand::CodexPersonalityMenu {
            file,
            window_name,
            tmux_socket,
            lock_file,
            lock_token,
        } => run_codex_personality_menu(file, window_name, tmux_socket, lock_file, lock_token),
    }
}

fn run_mux(
    config_path: PathBuf,
    session_name_override: Option<String>,
    socket_name: String,
) -> Result<i32, String> {
    let config = MuxConfig::from_path(&config_path)?;

    let session_name = if let Some(name) = session_name_override {
        sanitize_session_name(&name).ok_or_else(|| "invalid --session-name".to_string())?
    } else {
        derive_session_name(&config)
    };

    let tmux_conf = generate_tmux_conf(&config, &socket_name);
    let conf_path = write_tmux_conf(&tmux_conf, &session_name)?;

    if session_exists(&session_name, &socket_name) {
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

    let first = &config.tabs[0];
    let shell_cmd = build_env_cmd(&first.env, &first.cmd);
    let win_name = window_name_with_hint(&first.label, 0);
    let mut args = vec![
        "-L",
        &socket_name,
        "-f",
        conf_path.to_str().unwrap(),
        "new-session",
        "-d",
        "-s",
        &session_name,
        "-n",
        &win_name,
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
        let _ = Command::new("tmux")
            .args(["-L", &socket_name, "kill-server"])
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .status();
        let status2 = Command::new("tmux")
            .args(&args)
            .status()
            .map_err(|e| format!("tmux new-session failed after cleanup: {e}"))?;
        if !status2.success() {
            return Err("tmux new-session failed".into());
        }
    }

    for (k, v) in &first.env {
        let _ = Command::new("tmux")
            .args(["-L", &socket_name, "set-environment", "-t", &session_name, k, v])
            .status();
    }

    for (i, tab) in config.tabs.iter().enumerate().skip(1) {
        let shell_cmd = build_env_cmd(&tab.env, &tab.cmd);
        let win_name = window_name_with_hint(&tab.label, i);
        let mut args = vec!["-L", &socket_name, "new-window", "-t", &session_name, "-n", &win_name];
        if let Some(ref cwd) = tab.cwd {
            args.extend_from_slice(&["-c", cwd]);
        }
        args.push(&shell_cmd);

        let status = Command::new("tmux").args(&args).status();
        if let Err(e) = status {
            return Err(format!("tmux new-window '{}' failed: {e}", tab.label));
        }
    }

    let _ = Command::new("tmux")
        .args(["-L", &socket_name, "select-window", "-t", &format!("{session_name}:0")])
        .status();

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

fn detach_other_clients(session_name: &str, socket_name: &str) {
    let output = Command::new("tmux")
        .args([
            "-L",
            socket_name,
            "list-clients",
            "-t",
            session_name,
            "-F",
            "#{client_tty}",
        ])
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
    if s.bytes().all(|b| {
        matches!(
            b,
            b'a'..=b'z'
                | b'A'..=b'Z'
                | b'0'..=b'9'
                | b'-'
                | b'_'
                | b'.'
                | b'/'
                | b':'
                | b'='
                | b'+'
                | b','
                | b'@'
        )
    }) {
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

fn ai_mux_binary() -> String {
    std::env::current_exe()
        .map(|path| path.to_string_lossy().to_string())
        .unwrap_or_else(|_| {
            format!(
                "{}/.ai-cli/bin/ai-mux",
                std::env::var("HOME").unwrap_or_else(|_| "~".to_string())
            )
        })
}

fn codex_personality_binding(socket_name: &str) -> String {
    let launcher = format!(
        "{} codex-personality open --tmux-socket {} --window-name {}",
        ai_mux_binary(),
        socket_name,
        CODEX_PERSONALITY_WINDOW,
    );
    let guard = format!("#{{m:^{}$,#{{window_name}}}}", CODEX_PERSONALITY_WINDOW);
    format!(
        "bind -n F9 if-shell -F {} {{ run-shell true }} {{ run-shell -b {} }}\n",
        shell_escape(&guard),
        shell_escape(&launcher),
    )
}

fn primary_tool_name(config: &MuxConfig) -> String {
    for tab in &config.tabs {
        if !tab.primary {
            continue;
        }
        if let Some(tool_name) = tab.env.get("AI_CLI_TOOL") {
            return tool_name.trim().to_ascii_lowercase();
        }
        return tab.label.trim().to_ascii_lowercase();
    }
    String::new()
}

fn generate_tmux_conf(config: &MuxConfig, socket_name: &str) -> String {
    let mut conf = String::new();
    let is_codex = primary_tool_name(config) == "codex";

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
    conf.push_str("set -g allow-rename off\n");
    conf.push_str("set -g automatic-rename off\n");

    conf.push_str("\n# Status bar\n");
    conf.push_str("set -g status-style 'bg=#333333,fg=white,dim'\n");
    conf.push_str("set -g status-left ''\n");
    let status_right = if is_codex {
        "#[fg=#ffffff,bold] F5:global F6:base F7:tool F8:project F9:persona │ C-] prefix "
    } else {
        "#[fg=#ffffff,bold] F5:global F6:base F7:tool F8:project │ C-] prefix "
    };
    conf.push_str(&format!("set -g status-right '{}'\n", status_right));
    conf.push_str("set -g status-left-length 0\n");
    conf.push_str("set -g status-right-length 72\n");
    conf.push_str("set -g window-status-format ' #W '\n");
    conf.push_str("set -g window-status-current-format '#[bold,noreverse] #W '\n");
    conf.push_str("set -g window-status-current-style 'bg=default,fg=white,bold'\n");
    conf.push_str("set -g window-status-style 'bg=#333333,fg=#999999'\n");
    conf.push_str("set -g window-status-separator ''\n");

    conf.push_str("\n# Use C-] as prefix (avoids conflicts with tools)\n");
    conf.push_str("unbind C-b\n");
    conf.push_str("set -g prefix C-]\n");
    conf.push_str("bind C-] send-prefix\n");

    conf.push_str("\n# Key bindings (no prefix)\n");
    conf.push_str("bind -n F2 select-window -t :0\n");
    conf.push_str("bind -n F3 select-window -t :1\n");
    conf.push_str("bind -n F4 select-window -t :2\n");
    conf.push_str("bind -n F10 select-window -t :5\n");
    conf.push_str("bind -n F11 select-window -t :6\n");

    let edit_global_cmd = editor_launcher_cmd("edit-global", "global", socket_name);
    let edit_base_cmd = editor_launcher_cmd("edit-base", "base", socket_name);
    let edit_tool_cmd = editor_launcher_cmd("edit-tool", "tool", socket_name);
    let edit_project_cmd = editor_launcher_cmd("edit-project", "project", socket_name);
    conf.push_str(&editor_binding("F5", &edit_global_cmd));
    conf.push_str(&editor_binding("F6", &edit_base_cmd));
    conf.push_str(&editor_binding("F7", &edit_tool_cmd));
    conf.push_str(&editor_binding("F8", &edit_project_cmd));
    if is_codex {
        conf.push_str(&codex_personality_binding(socket_name));
    }

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
    conf.push_str("\n# User overrides\n");
    conf.push_str("if-shell 'test -f ~/.config/ai-cli/tmux.conf' 'source-file ~/.config/ai-cli/tmux.conf'\n");
    conf
}

fn window_name_with_hint(label: &str, index: usize) -> String {
    let alt = index + 2;
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
    let path = dir.join(format!("{session_name}.conf"));
    fs::write(&path, conf).map_err(|e| format!("failed writing tmux.conf: {e}"))?;
    Ok(path)
}

fn parse_args(args: Vec<String>) -> Result<CliCommand, String> {
    if args.first().map(String::as_str) == Some("codex-personality") {
        return parse_codex_personality_args(args.into_iter().skip(1).collect());
    }
    parse_run_args(args)
}

fn parse_run_args(args: Vec<String>) -> Result<CliCommand, String> {
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
                    "usage: ai-mux --config <path> [--session-name <name>] [--socket-name <name>]".to_string(),
                )
            }
        }
    }

    let Some(config_path) = config_path else {
        return Err(
            "usage: ai-mux --config <path> [--session-name <name>] [--socket-name <name>]".to_string(),
        );
    };

    Ok(CliCommand::Run {
        config_path,
        session_name_override: session_name,
        socket_name,
    })
}

fn parse_codex_personality_args(args: Vec<String>) -> Result<CliCommand, String> {
    if args.is_empty() {
        return Err("usage: ai-mux codex-personality <open|menu> ...".to_string());
    }
    let action = args[0].clone();
    let mut file: Option<PathBuf> = None;
    let mut window_name: Option<String> = None;
    let mut tmux_socket: Option<String> = None;
    let mut lock_file: Option<PathBuf> = None;
    let mut lock_token: Option<String> = None;
    let mut i = 1;

    while i < args.len() {
        match args[i].as_str() {
            "--file" if i + 1 < args.len() => {
                file = Some(PathBuf::from(&args[i + 1]));
                i += 2;
            }
            "--window-name" if i + 1 < args.len() => {
                window_name = Some(args[i + 1].clone());
                i += 2;
            }
            "--tmux-socket" if i + 1 < args.len() => {
                tmux_socket = Some(args[i + 1].clone());
                i += 2;
            }
            "--lock-file" if i + 1 < args.len() => {
                lock_file = Some(PathBuf::from(&args[i + 1]));
                i += 2;
            }
            "--lock-token" if i + 1 < args.len() => {
                lock_token = Some(args[i + 1].clone());
                i += 2;
            }
            _ => return Err("usage: ai-mux codex-personality <open|menu> ...".to_string()),
        }
    }

    let Some(window_name) = window_name else {
        return Err("codex-personality requires --window-name".to_string());
    };

    match action.as_str() {
        "open" => Ok(CliCommand::CodexPersonalityOpen {
            file,
            window_name,
            tmux_socket,
        }),
        "menu" => Ok(CliCommand::CodexPersonalityMenu {
            file,
            window_name,
            tmux_socket,
            lock_file: lock_file.ok_or_else(|| "codex-personality menu requires --lock-file".to_string())?,
            lock_token: lock_token.ok_or_else(|| "codex-personality menu requires --lock-token".to_string())?,
        }),
        _ => Err("usage: ai-mux codex-personality <open|menu> ...".to_string()),
    }
}

fn home_dir() -> PathBuf {
    std::env::var("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|_| PathBuf::from("~"))
}

fn resolve_personality_file(path_arg: Option<PathBuf>) -> PathBuf {
    if let Some(path) = path_arg {
        return path;
    }
    if let Ok(env_path) = std::env::var("AI_CLI_CODEX_PERSONALITY_PROMPT_FILE") {
        if !env_path.trim().is_empty() {
            return PathBuf::from(env_path);
        }
    }
    home_dir()
        .join(".ai-cli")
        .join("instructions")
        .join("codex-personality.txt")
}

fn ensure_file(path: &Path) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("failed creating {}: {e}", parent.display()))?;
    }
    if !path.exists() {
        fs::write(path, "").map_err(|e| format!("failed creating {}: {e}", path.display()))?;
    }
    Ok(())
}

fn resolve_defaults_file(path: &Path) -> PathBuf {
    let stem = path
        .file_stem()
        .and_then(|value| value.to_str())
        .unwrap_or("codex-personality");
    path.with_file_name(format!("{stem}.defaults.json"))
}

fn lock_dir() -> PathBuf {
    home_dir().join(".ai-cli").join("locks").join("codex-personality")
}

fn lock_path(target: &Path) -> PathBuf {
    let mut hasher = DefaultHasher::new();
    target.to_string_lossy().hash(&mut hasher);
    lock_dir().join(format!("{:016x}.json", hasher.finish()))
}

fn pid_alive(pid: u32) -> bool {
    Command::new("kill")
        .args(["-0", &pid.to_string()])
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|status| status.success())
        .unwrap_or(false)
}

fn read_lock(path: &Path) -> Option<MenuLock> {
    let text = fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}

fn write_lock(path: &Path, payload: &MenuLock) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|e| format!("failed creating {}: {e}", parent.display()))?;
    }
    let text = serde_json::to_string(payload).map_err(|e| format!("failed encoding lock: {e}"))?;
    fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(path)
        .and_then(|mut file| file.write_all(text.as_bytes()))
        .map_err(|e| format!("failed writing lock {}: {e}", path.display()))
}

fn replace_stale_lock(path: &Path, payload: &MenuLock) -> bool {
    let Some(current) = read_lock(path) else {
        let _ = fs::remove_file(path);
        return write_lock(path, payload).is_ok();
    };
    if pid_alive(current.pid) {
        return false;
    }
    if fs::remove_file(path).is_err() && path.exists() {
        return false;
    }
    write_lock(path, payload).is_ok()
}

fn acquire_lock(path: &Path, payload: &MenuLock) -> bool {
    write_lock(path, payload).is_ok() || replace_stale_lock(path, payload)
}

fn release_lock(path: &Path, token: &str) {
    let should_remove = read_lock(path)
        .map(|current| current.token == token)
        .unwrap_or(false);
    if should_remove {
        let _ = fs::remove_file(path);
    }
}

fn tmux_command(socket_name: Option<&str>, args: &[String]) -> Result<std::process::Output, String> {
    let mut command = Command::new("tmux");
    if let Some(socket_name) = socket_name {
        command.args(["-L", socket_name]);
    }
    command.args(args);
    command
        .output()
        .map_err(|e| format!("tmux command failed: {e}"))
}

fn select_existing_window(socket_name: Option<&str>, window_name: &str) -> bool {
    let args = vec!["select-window".to_string(), "-t".to_string(), window_name.to_string()];
    tmux_command(socket_name, &args)
        .map(|output| output.status.success())
        .unwrap_or(false)
}

fn display_message(socket_name: Option<&str>, message: &str) {
    let args = vec!["display-message".to_string(), message.to_string()];
    let _ = tmux_command(socket_name, &args);
}

fn self_command(
    file: Option<&Path>,
    tmux_socket: Option<&str>,
    lock_file: &Path,
    lock_token: &str,
    window_name: &str,
) -> String {
    let mut parts = vec![
        ai_mux_binary(),
        "codex-personality".to_string(),
        "menu".to_string(),
        "--lock-file".to_string(),
        lock_file.to_string_lossy().to_string(),
        "--lock-token".to_string(),
        lock_token.to_string(),
        "--window-name".to_string(),
        window_name.to_string(),
    ];
    if let Some(file) = file {
        parts.push("--file".to_string());
        parts.push(file.to_string_lossy().to_string());
    }
    if let Some(tmux_socket) = tmux_socket {
        parts.push("--tmux-socket".to_string());
        parts.push(tmux_socket.to_string());
    }
    parts
        .iter()
        .map(|part| shell_escape(part))
        .collect::<Vec<_>>()
        .join(" ")
}

fn open_codex_personality_menu(
    file: Option<PathBuf>,
    window_name: String,
    tmux_socket: Option<String>,
) -> Result<i32, String> {
    let target = resolve_personality_file(file);
    ensure_file(&target)?;
    let lock_file = lock_path(&target);
    let token = format!("{}-{}", std::process::id(), std::time::SystemTime::now().elapsed().map(|d| d.as_nanos()).unwrap_or(0));
    let payload = MenuLock {
        file: target.to_string_lossy().to_string(),
        pid: std::process::id(),
        token: token.clone(),
        window_name: window_name.clone(),
    };

    if !acquire_lock(&lock_file, &payload) {
        if !select_existing_window(tmux_socket.as_deref(), &window_name) {
            display_message(tmux_socket.as_deref(), &format!("{} already open", target.display()));
        }
        return Ok(0);
    }

    let cwd = target.parent().unwrap_or_else(|| Path::new("."));
    let command = self_command(
        Some(&target),
        tmux_socket.as_deref(),
        &lock_file,
        &token,
        &window_name,
    );
    let args = vec![
        "new-window".to_string(),
        "-n".to_string(),
        window_name,
        "-c".to_string(),
        cwd.to_string_lossy().to_string(),
        command,
    ];
    let output = tmux_command(tmux_socket.as_deref(), &args)?;
    if !output.status.success() {
        release_lock(&lock_file, &token);
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        return Err(if stderr.is_empty() {
            "tmux new-window failed".to_string()
        } else {
            stderr
        });
    }
    Ok(0)
}

fn normalize_text(value: Option<&Value>) -> String {
    value
        .and_then(Value::as_str)
        .map(str::trim)
        .unwrap_or("")
        .to_string()
}

fn heading_key(line: &str) -> Option<&'static str> {
    let trimmed = line.trim();
    if !trimmed.starts_with('#') {
        return None;
    }
    let text = trimmed.trim_start_matches('#').trim().to_ascii_lowercase();
    match text.as_str() {
        "personality" | "values" => Some("personality"),
        "interaction style" => Some("interaction_style"),
        "escalation" => Some("escalation"),
        _ => None,
    }
}

fn parse_sections(raw: &str) -> PersonalitySections {
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return PersonalitySections::default();
    }

    if let Ok(value) = serde_json::from_str::<Value>(trimmed) {
        if let Some(obj) = value.as_object() {
            return PersonalitySections {
                personality: normalize_text(obj.get("personality")),
                interaction_style: normalize_text(
                    obj.get("interaction_style").or_else(|| obj.get("interaction style")),
                ),
                escalation: normalize_text(obj.get("escalation")),
            };
        }
    }

    let mut current_key: Option<&str> = None;
    let mut personality = Vec::new();
    let mut interaction_style = Vec::new();
    let mut escalation = Vec::new();
    let mut saw_heading = false;

    for line in raw.lines() {
        if let Some(key) = heading_key(line) {
            current_key = Some(key);
            saw_heading = true;
            continue;
        }
        match current_key {
            Some("personality") => personality.push(line),
            Some("interaction_style") => interaction_style.push(line),
            Some("escalation") => escalation.push(line),
            _ => {}
        }
    }

    if !saw_heading {
        return PersonalitySections {
            personality: trimmed.to_string(),
            interaction_style: String::new(),
            escalation: String::new(),
        };
    }

    PersonalitySections {
        personality: personality.join("\n").trim().to_string(),
        interaction_style: interaction_style.join("\n").trim().to_string(),
        escalation: escalation.join("\n").trim().to_string(),
    }
}

fn has_values(sections: &PersonalitySections) -> bool {
    !sections.personality.trim().is_empty()
        || !sections.interaction_style.trim().is_empty()
        || !sections.escalation.trim().is_empty()
}

fn load_sections_file(path: &Path) -> PersonalitySections {
    fs::read_to_string(path)
        .map(|raw| parse_sections(&raw))
        .unwrap_or_default()
}

fn load_existing_sections(path: &Path) -> PersonalitySections {
    let override_sections = load_sections_file(path);
    if has_values(&override_sections) {
        return override_sections;
    }
    let defaults_path = resolve_defaults_file(path);
    let default_sections = load_sections_file(&defaults_path);
    if has_values(&default_sections) {
        return default_sections;
    }
    override_sections
}

fn write_payload(path: &Path, sections: &PersonalitySections) -> Result<(), String> {
    let payload = json!({
        "personality": sections.personality.trim_end(),
        "interaction_style": sections.interaction_style.trim_end(),
        "escalation": sections.escalation.trim_end(),
    });
    let encoded = serde_json::to_string_pretty(&payload)
        .map_err(|e| format!("failed encoding {}: {e}", path.display()))?;
    fs::write(path, format!("{encoded}\n"))
        .map_err(|e| format!("failed writing {}: {e}", path.display()))
}

fn buffers_from_sections(sections: &PersonalitySections) -> [TextBuffer; 3] {
    [
        TextBuffer::from_text(&sections.personality),
        TextBuffer::from_text(&sections.interaction_style),
        TextBuffer::from_text(&sections.escalation),
    ]
}

fn sections_from_buffers(buffers: &[TextBuffer; 3]) -> PersonalitySections {
    PersonalitySections {
        personality: buffers[0].to_text(),
        interaction_style: buffers[1].to_text(),
        escalation: buffers[2].to_text(),
    }
}

fn persist_buffers(path: &Path, buffers: &[TextBuffer; 3]) -> Result<(), String> {
    write_payload(path, &sections_from_buffers(buffers))
}

struct TerminalGuard;

impl TerminalGuard {
    fn enter(stdout: &mut io::Stdout) -> Result<Self, String> {
        enable_raw_mode().map_err(|e| format!("failed enabling raw mode: {e}"))?;
        execute!(stdout, EnterAlternateScreen, Hide)
            .map_err(|e| format!("failed entering alternate screen: {e}"))?;
        Ok(Self)
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        let _ = disable_raw_mode();
        let mut stdout = io::stdout();
        let _ = execute!(stdout, Show, LeaveAlternateScreen);
    }
}

fn render_editor(
    stdout: &mut io::Stdout,
    target: &Path,
    buffers: &[TextBuffer; 3],
    active: usize,
) -> Result<(), String> {
    let (width, height) = size().map_err(|e| format!("failed reading terminal size: {e}"))?;
    let width = width.max(40);
    let height = height.max(16);
    let field_titles = ["Personality", "Interaction Style", "Escalation"];
    let header_lines = 4u16;
    let content_width = width.saturating_sub(2) as usize;
    let wrapped_fields: Vec<Vec<WrappedDisplayLine>> = buffers
        .iter()
        .map(|buffer| wrap_buffer_for_display(buffer, content_width.max(1)))
        .collect();
    let mut body_lines_remaining = height.saturating_sub(header_lines) as usize;
    let mut field_line_allocations = vec![1usize; field_titles.len()];
    for idx in 0..field_titles.len() {
        let remaining_fields = field_titles.len().saturating_sub(idx + 1);
        let reserved_for_remaining = remaining_fields.saturating_mul(2);
        let desired_lines = wrapped_fields[idx].len().max(1);
        let available_for_field = body_lines_remaining
            .saturating_sub(1)
            .saturating_sub(reserved_for_remaining)
            .max(1);
        let allocated = desired_lines.min(available_for_field);
        field_line_allocations[idx] = allocated;
        body_lines_remaining = body_lines_remaining.saturating_sub(1 + allocated);
    }

    queue!(stdout, MoveTo(0, 0), Clear(ClearType::All))
        .map_err(|e| format!("failed drawing editor: {e}"))?;
    queue!(
        stdout,
        SetAttribute(Attribute::Bold),
        Print("Codex Personality\n"),
        SetAttribute(Attribute::Reset),
        Print("Enter: next field/save  Shift+Enter: newline  Tab: next  Shift+Tab: previous\n"),
        Print("Esc/Ctrl+C: close  Changes persist immediately to "),
        SetForegroundColor(Color::Cyan),
        Print(target.display().to_string()),
        ResetColor,
        Print("\n\n"),
    )
    .map_err(|e| format!("failed drawing header: {e}"))?;

    let mut cursor_x = 0u16;
    let mut cursor_y = header_lines;
    let mut current_y = header_lines;
    for (idx, title) in field_titles.iter().enumerate() {
        let top = current_y;
        let body_top = top + 1;
        let available_lines = field_line_allocations[idx];
        let is_active = idx == active;
        let wrapped_lines = &wrapped_fields[idx];
        let (display_cursor_row, display_cursor_col) = if is_active {
            find_display_cursor(wrapped_lines, &buffers[idx])
        } else {
            (0usize, 0usize)
        };
        let scroll = if is_active {
            display_cursor_row.saturating_sub(available_lines.saturating_sub(1))
        } else {
            0
        };

        queue!(stdout, MoveTo(0, top)).map_err(|e| format!("failed positioning title: {e}"))?;
        if is_active {
            queue!(
                stdout,
                SetForegroundColor(Color::Black),
                crossterm::style::SetBackgroundColor(Color::White),
                SetAttribute(Attribute::Bold)
            )
            .map_err(|e| format!("failed styling title: {e}"))?;
        } else {
            queue!(stdout, SetAttribute(Attribute::Bold), SetForegroundColor(Color::Grey))
                .map_err(|e| format!("failed styling title: {e}"))?;
        }
        let title_text = format!(" {} ", title);
        queue!(
            stdout,
            Print(truncate_text(&title_text, width as usize)),
            ResetColor,
            SetAttribute(Attribute::Reset)
        )
        .map_err(|e| format!("failed drawing title: {e}"))?;

        for row in 0..available_lines {
            queue!(stdout, MoveTo(0, body_top + row as u16))
                .map_err(|e| format!("failed positioning body: {e}"))?;
            let line = wrapped_lines
                .get(scroll + row)
                .map(|line| line.text.as_str())
                .unwrap_or("");
            let marker = if is_active { ">" } else { " " };
            let rendered = format!("{marker} {line}");
            queue!(stdout, Print(pad_text(&rendered, width as usize)))
                .map_err(|e| format!("failed drawing line: {e}"))?;
        }

        if is_active {
            let visible_row = display_cursor_row.saturating_sub(scroll);
            cursor_y = body_top + visible_row.min(available_lines.saturating_sub(1)) as u16;
            cursor_x = (2 + display_cursor_col.min(content_width.saturating_sub(1))) as u16;
        }
        current_y = body_top + available_lines as u16;
    }

    queue!(stdout, MoveTo(cursor_x, cursor_y), Show)
        .map_err(|e| format!("failed positioning cursor: {e}"))?;
    stdout.flush().map_err(|e| format!("failed flushing editor: {e}"))
}

fn wrap_buffer_for_display(buffer: &TextBuffer, width: usize) -> Vec<WrappedDisplayLine> {
    let mut wrapped = Vec::new();
    for (row, line) in buffer.lines.iter().enumerate() {
        wrapped.extend(wrap_line_for_display(line, row, width));
    }
    if wrapped.is_empty() {
        wrapped.push(WrappedDisplayLine {
            text: String::new(),
            source_row: 0,
            start_col: 0,
            end_col: 0,
        });
    }
    wrapped
}

fn wrap_line_for_display(line: &str, source_row: usize, width: usize) -> Vec<WrappedDisplayLine> {
    let width = width.max(1);
    let chars: Vec<char> = line.chars().collect();
    if chars.is_empty() {
        return vec![WrappedDisplayLine {
            text: String::new(),
            source_row,
            start_col: 0,
            end_col: 0,
        }];
    }

    let mut wrapped = Vec::new();
    let mut start = 0usize;
    while start < chars.len() {
        let hard_end = (start + width).min(chars.len());
        let mut end = hard_end;
        if hard_end < chars.len() {
            for idx in (start..=hard_end).rev() {
                if chars[idx].is_whitespace() {
                    end = idx + 1;
                    break;
                }
            }
            if end == start {
                end = hard_end;
            }
        }

        let segment: String = chars[start..end].iter().collect();
        let rendered = segment
            .trim_end_matches(|ch: char| ch.is_whitespace())
            .to_string();
        wrapped.push(WrappedDisplayLine {
            text: rendered,
            source_row,
            start_col: start,
            end_col: end,
        });
        start = end;
    }

    wrapped
}

fn find_display_cursor(lines: &[WrappedDisplayLine], buffer: &TextBuffer) -> (usize, usize) {
    let mut fallback = (0usize, 0usize);
    for (idx, line) in lines.iter().enumerate() {
        if line.source_row != buffer.row {
            continue;
        }
        fallback = (idx, line.text.chars().count());
        let next_same_row = lines
            .get(idx + 1)
            .map(|next| next.source_row == buffer.row)
            .unwrap_or(false);
        if buffer.col < line.end_col || (buffer.col == line.end_col && !next_same_row) {
            let col = buffer
                .col
                .saturating_sub(line.start_col)
                .min(line.text.chars().count());
            return (idx, col);
        }
    }
    fallback
}

fn truncate_text(text: &str, max_chars: usize) -> String {
    text.chars().take(max_chars).collect()
}

fn pad_text(text: &str, width: usize) -> String {
    let visible = text.chars().count();
    if visible >= width {
        return truncate_text(text, width);
    }
    format!("{text}{}", " ".repeat(width - visible))
}

fn run_editor_loop(target: &Path, buffers: &mut [TextBuffer; 3]) -> Result<(), String> {
    let mut stdout = io::stdout();
    let _guard = TerminalGuard::enter(&mut stdout)?;
    let mut active = 0usize;
    render_editor(&mut stdout, target, buffers, active)?;

    loop {
        let event = read().map_err(|e| format!("failed reading terminal event: {e}"))?;
        let Event::Key(KeyEvent { code, modifiers, .. }) = event else {
            continue;
        };

        let mut should_render = true;
        let mut should_persist = false;
        match (code, modifiers) {
            (KeyCode::Char('c'), KeyModifiers::CONTROL) | (KeyCode::Esc, _) => break,
            (KeyCode::Enter, m) if m.contains(KeyModifiers::SHIFT) => {
                buffers[active].insert_newline();
                should_persist = true;
            }
            (KeyCode::Enter, _) => {
                if active + 1 < buffers.len() {
                    active += 1;
                } else {
                    break;
                }
            }
            (KeyCode::Tab, _) => active = (active + 1) % buffers.len(),
            (KeyCode::BackTab, _) => active = if active == 0 { buffers.len() - 1 } else { active - 1 },
            (KeyCode::Backspace, _) => {
                buffers[active].backspace();
                should_persist = true;
            }
            (KeyCode::Left, _) => buffers[active].move_left(),
            (KeyCode::Right, _) => buffers[active].move_right(),
            (KeyCode::Up, _) => buffers[active].move_up(),
            (KeyCode::Down, _) => buffers[active].move_down(),
            (KeyCode::Home, _) => buffers[active].move_home(),
            (KeyCode::End, _) => buffers[active].move_end(),
            (KeyCode::Char(ch), m) if !m.contains(KeyModifiers::CONTROL) && !m.contains(KeyModifiers::ALT) => {
                buffers[active].insert_char(ch);
                should_persist = true;
            }
            _ => should_render = false,
        }

        if should_persist {
            persist_buffers(target, buffers)?;
        }
        if should_render {
            render_editor(&mut stdout, target, buffers, active)?;
        }
    }

    persist_buffers(target, buffers)?;
    Ok(())
}

fn run_codex_personality_menu(
    file: Option<PathBuf>,
    window_name: String,
    tmux_socket: Option<String>,
    lock_file: PathBuf,
    lock_token: String,
) -> Result<i32, String> {
    let target = resolve_personality_file(file);
    ensure_file(&target)?;
    let current = read_lock(&lock_file);
    let Some(mut current) = current else {
        display_message(tmux_socket.as_deref(), &format!("{} is already managed elsewhere", target.display()));
        return Ok(0);
    };
    if current.token != lock_token {
        display_message(tmux_socket.as_deref(), &format!("{} is already managed elsewhere", target.display()));
        return Ok(0);
    }

    current.pid = std::process::id();
    current.file = target.to_string_lossy().to_string();
    current.window_name = window_name;
    fs::write(
        &lock_file,
        serde_json::to_string(&current).map_err(|e| format!("failed encoding lock: {e}"))?,
    )
    .map_err(|e| format!("failed refreshing lock {}: {e}", lock_file.display()))?;

    let existing = load_existing_sections(&target);
    let mut buffers = buffers_from_sections(&existing);
    persist_buffers(&target, &buffers)?;
    let result = run_editor_loop(&target, &mut buffers);
    release_lock(&lock_file, &lock_token);
    result.map(|_| 0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn codex_config() -> MuxConfig {
        MuxConfig {
            session_name: Some("codex-test".to_string()),
            tabs: vec![config::TabDef {
                label: "codex".to_string(),
                cmd: vec![],
                env: std::collections::HashMap::from([
                    ("AI_CLI_TOOL".to_string(), "codex".to_string()),
                ]),
                cwd: None,
                primary: true,
            }],
        }
    }

    #[test]
    fn generated_tmux_conf_uses_requested_socket_for_editor_bindings() {
        let conf = generate_tmux_conf(&codex_config(), "ai-cli-codex");

        assert!(conf.contains("--tmux-socket ai-cli-codex --window-name edit-global"));
        assert!(conf.contains("--tmux-socket ai-cli-codex --window-name edit-project"));
        assert!(!conf.contains("--tmux-socket ai-mux --window-name edit-global"));
        assert!(conf.contains("F9:persona"));
        assert!(conf.contains("codex-personality open --tmux-socket ai-cli-codex"));
        assert!(!conf.contains("ai-codex-personality-menu"));
    }

    #[test]
    fn generated_tmux_conf_hides_f9_for_non_codex_sessions() {
        let config = MuxConfig {
            session_name: Some("gemini-test".to_string()),
            tabs: vec![config::TabDef {
                label: "gemini".to_string(),
                cmd: vec![],
                env: std::collections::HashMap::from([
                    ("AI_CLI_TOOL".to_string(), "gemini".to_string()),
                ]),
                cwd: None,
                primary: true,
            }],
        };

        let conf = generate_tmux_conf(&config, "ai-mux");

        assert!(!conf.contains("F9:persona"));
        assert!(!conf.contains("codex-personality open"));
    }

    #[test]
    fn parse_sections_reads_json_payload() {
        let raw = r#"{
  "personality": "Be blunt.",
  "interaction_style": "Stay direct.",
  "escalation": "Challenge weak assumptions."
}"#;

        let sections = parse_sections(raw);

        assert_eq!(sections.personality, "Be blunt.");
        assert_eq!(sections.interaction_style, "Stay direct.");
        assert_eq!(sections.escalation, "Challenge weak assumptions.");
    }

    #[test]
    fn load_existing_sections_falls_back_to_defaults_snapshot() {
        let dir = std::env::temp_dir().join(format!("ai-mux-test-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("codex-personality.txt");
        fs::write(&path, "").unwrap();
        fs::write(
            resolve_defaults_file(&path),
            r#"{
  "personality": "API personality",
  "interaction_style": "API interaction",
  "escalation": "API escalation"
}"#,
        )
        .unwrap();

        let sections = load_existing_sections(&path);

        assert_eq!(sections.personality, "API personality");
        assert_eq!(sections.interaction_style, "API interaction");
        assert_eq!(sections.escalation, "API escalation");

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn write_payload_emits_stable_json_shape() {
        let dir = std::env::temp_dir().join(format!("ai-mux-write-{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let path = dir.join("codex-personality.txt");
        let sections = PersonalitySections {
            personality: "One".to_string(),
            interaction_style: "Two".to_string(),
            escalation: "Three".to_string(),
        };

        write_payload(&path, &sections).unwrap();

        let encoded = fs::read_to_string(&path).unwrap();
        let value: Value = serde_json::from_str(&encoded).unwrap();
        assert_eq!(value["personality"], "One");
        assert_eq!(value["interaction_style"], "Two");
        assert_eq!(value["escalation"], "Three");

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn wrap_line_for_display_prefers_word_boundaries() {
        let wrapped = wrap_line_for_display("one two three four", 0, 8);
        let lines: Vec<String> = wrapped.into_iter().map(|line| line.text).collect();

        assert_eq!(lines, vec!["one two", "three", "four"]);
    }
}

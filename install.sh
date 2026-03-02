#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="${HOME}/.local/bin"
ALIAS_DIR="${HOME}/.ai-cli/bin"
CLAUDE_DIR="${HOME}/.claude"
STATUSLINE_DEST="${CLAUDE_DIR}/statusline-command.sh"
MUX_DIR="${SCRIPT_DIR}/mux"
PKG_MUX_DIR="${SCRIPT_DIR}/ai_cli/bin"

TOOLS=(claude codex copilot gemini)

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Options:
  --reinstall, -f      Force reinstall of the Python package
  --alias-all          Create command aliases for all wrapped tools
  --alias TOOL         Create alias for one tool (repeatable)
  --no-alias           Do not create aliases
  --install-tools      Install underlying CLI tools (claude, codex, copilot, gemini)
  --install-tool TOOL  Install a specific CLI tool (repeatable)
  --method METHOD      Install method for tools (npm, brew, macports, curl)
  --auto-install-deps  Allow installer to install required system deps (tmux)
  --yes, -y            Assume yes for interactive confirmations
  --non-interactive    Never prompt; use provided flags only
  --help, -h           Show this help
EOF
}

ensure_rc_line() {
  local rc_file="$1"
  local marker="$2"
  local line="$3"
  [ -f "$rc_file" ] || return 0
  if ! grep -qF "$marker" "$rc_file"; then
    printf '\n# %s\n%s\n' "$marker" "$line" >> "$rc_file"
    echo "Updated $rc_file ($marker)"
  fi
}

set_config_value() {
  local tool="$1"
  local binary="$2"
  local alias_state="$3"

  python3 - "$tool" "$binary" "$alias_state" <<'PY'
import sys
from ai_cli.config import ensure_config, save_config

tool = sys.argv[1]
binary = sys.argv[2]
alias_state = sys.argv[3].lower() == "true"
cfg = ensure_config()
tool_cfg = cfg.setdefault("tools", {}).setdefault(tool, {})
if binary != "__KEEP__":
    tool_cfg["binary"] = binary
cfg.setdefault("aliases", {})[tool] = alias_state
save_config(cfg)
PY
}

copy_completions() {
  local comp_src="${SCRIPT_DIR}/completions"

  # Zsh
  local omz_dir="${ZSH:-${HOME}/.oh-my-zsh}"
  local zsh_comp_dir=""
  if [ -d "$omz_dir" ]; then
    zsh_comp_dir="${ZSH_CUSTOM:-${omz_dir}/custom}/completions"
    mkdir -p "$zsh_comp_dir"
    cp "$comp_src/_ai-cli" "$zsh_comp_dir/_ai-cli"
    echo "Installed zsh completion: $zsh_comp_dir/_ai-cli"
  else
    zsh_comp_dir="${HOME}/.zsh/completions"
    mkdir -p "$zsh_comp_dir"
    cp "$comp_src/_ai-cli" "$zsh_comp_dir/_ai-cli"
    echo "Installed zsh completion: $zsh_comp_dir/_ai-cli"
    ensure_rc_line "${HOME}/.zshrc" "ai-cli: zsh completion path" "fpath=(\"${zsh_comp_dir}\" \$fpath)"
    ensure_rc_line "${HOME}/.zshrc" "ai-cli: compinit" "autoload -Uz compinit && compinit"
  fi

  # Bash
  local bash_comp_dir="${HOME}/.local/share/bash-completion/completions"
  mkdir -p "$bash_comp_dir"
  cp "$comp_src/ai-cli.bash" "$bash_comp_dir/ai-cli"
  echo "Installed bash completion: $bash_comp_dir/ai-cli"
  ensure_rc_line "${HOME}/.bashrc" "ai-cli: bash completion" "[ -f \"${bash_comp_dir}/ai-cli\" ] && source \"${bash_comp_dir}/ai-cli\""
}

install_statusline() {
  mkdir -p "$CLAUDE_DIR"
  cp "${SCRIPT_DIR}/statusline/statusline-command.sh" "$STATUSLINE_DEST"
  chmod +x "$STATUSLINE_DEST"
  echo "Installed statusline: $STATUSLINE_DEST"

  local settings="${CLAUDE_DIR}/settings.json"
  local status_json='{"type":"command","command":"~/.claude/statusline-command.sh"}'

  if [ ! -f "$settings" ]; then
    printf '{\n  "statusLine": %s\n}\n' "$status_json" > "$settings"
    echo "Created $settings"
    return
  fi

  if command -v jq >/dev/null 2>&1; then
    local tmp
    tmp="$(mktemp)"
    jq --argjson sl "$status_json" '. + {statusLine: $sl} | del(.statusCommand)' "$settings" > "$tmp"
    mv "$tmp" "$settings"
    echo "Updated $settings"
  else
    echo "jq not found; update this manually in $settings:"
    echo '  "statusLine": {"type":"command","command":"~/.claude/statusline-command.sh"}'
  fi
}

reinstall=false
alias_all=false
no_alias=false
non_interactive=false
install_tools=false
install_method=""
auto_install_deps=false
yes_all=false
alias_tools=()
install_tool_list=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --reinstall|-f)
      reinstall=true
      ;;
    --alias-all)
      alias_all=true
      ;;
    --alias)
      shift
      if [[ $# -eq 0 ]]; then
        echo "--alias requires a tool name" >&2
        exit 1
      fi
      alias_tools+=("$1")
      ;;
    --no-alias)
      no_alias=true
      ;;
    --install-tools)
      install_tools=true
      ;;
    --install-tool)
      shift
      if [[ $# -eq 0 ]]; then
        echo "--install-tool requires a tool name" >&2
        exit 1
      fi
      install_tool_list+=("$1")
      ;;
    --method)
      shift
      if [[ $# -eq 0 ]]; then
        echo "--method requires a method name (npm, brew, macports, curl)" >&2
        exit 1
      fi
      install_method="$1"
      ;;
    --auto-install-deps)
      auto_install_deps=true
      ;;
    --yes|-y)
      yes_all=true
      ;;
    --non-interactive)
      non_interactive=true
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage
      exit 1
      ;;
  esac
  shift
done

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 is required" >&2
  exit 1
fi

# Ensure tmux is installed (required for ai-mux)
if ! command -v tmux >/dev/null 2>&1; then
  echo "tmux not found."
  if ! $auto_install_deps; then
    if $non_interactive; then
      echo "tmux is required. Re-run with --auto-install-deps to allow automatic installation." >&2
      exit 1
    fi
    if ! $yes_all && [ -t 0 ]; then
      read -r -p "Install tmux automatically now? [y/N] " deps_answer
      case "${deps_answer,,}" in
        y|yes) auto_install_deps=true ;;
      esac
    elif $yes_all; then
      auto_install_deps=true
    fi
  fi

  if ! $auto_install_deps; then
    echo "Please install tmux manually, then re-run install.sh." >&2
    exit 1
  fi

  echo "Installing tmux..."
  if command -v brew >/dev/null 2>&1; then
    brew install tmux
  elif command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y tmux
  elif command -v dnf >/dev/null 2>&1; then
    sudo dnf install -y tmux
  elif command -v pacman >/dev/null 2>&1; then
    sudo pacman -S --noconfirm tmux
  else
    echo "Could not auto-install tmux. Please install it manually." >&2
    exit 1
  fi
fi

mkdir -p "$BIN_DIR"

install_python_package() {
  local pip_flags=(-q)
  if $reinstall; then
    pip_flags+=(--upgrade --force-reinstall)
  fi

  local -a pip_user_cmd=(/usr/bin/env python3 -m pip install "${pip_flags[@]}" --user -e "$SCRIPT_DIR")
  local -a pip_venv_cmd=(/usr/bin/env python3 -m pip install "${pip_flags[@]}" -e "$SCRIPT_DIR")
  local user_install_error="Can not perform a '--user' install. User site-packages are not visible in this virtualenv."

  local output
  set +e
  output="$("${pip_user_cmd[@]}" 2>&1)"
  local status=$?
  set -e
  if [ $status -eq 0 ]; then
    return 0
  fi

  if printf '%s' "$output" | grep -qF "$user_install_error"; then
    echo "Detected virtualenv user-site restriction; retrying pip install without --user."
    "${pip_venv_cmd[@]}"
    return 0
  fi

  printf '%s\n' "$output" >&2
  return "$status"
}

install_python_package

if [ -d "$MUX_DIR" ] && command -v cargo >/dev/null 2>&1; then
  echo "Building ai-mux (tmux orchestrator)..."
  (cd "$MUX_DIR" && cargo build --release)
  if [ -x "$MUX_DIR/target/release/ai-mux" ]; then
    mkdir -p "$PKG_MUX_DIR"
    cp "$MUX_DIR/target/release/ai-mux" "$PKG_MUX_DIR/ai-mux"
    chmod +x "$PKG_MUX_DIR/ai-mux"
    cp "$MUX_DIR/target/release/ai-mux" "$BIN_DIR/ai-mux"
    chmod +x "$BIN_DIR/ai-mux"
    echo "Installed ai-mux: $BIN_DIR/ai-mux"
  fi
fi

# Ensure ai-cli executable is present.
AI_CLI_BIN="${BIN_DIR}/ai-cli"
if [ ! -x "$AI_CLI_BIN" ]; then
  AI_CLI_BIN="$(command -v ai-cli || true)"
fi
if [ -z "$AI_CLI_BIN" ] || [ ! -x "$AI_CLI_BIN" ]; then
  AI_CLI_BIN="${BIN_DIR}/ai-cli"
  cat > "$AI_CLI_BIN" <<'EOF'
#!/usr/bin/env bash
exec python3 -m ai_cli "$@"
EOF
  chmod +x "$AI_CLI_BIN"
fi

ensure_rc_line "${HOME}/.zshrc" "ai-cli: user bin" "export PATH=\"${BIN_DIR}:\$PATH\""
ensure_rc_line "${HOME}/.bashrc" "ai-cli: user bin" "export PATH=\"${BIN_DIR}:\$PATH\""

copy_completions
install_statusline

mkdir -p "$ALIAS_DIR"
alias_targets=()

if ! $no_alias; then
  if $alias_all; then
    alias_targets=("${TOOLS[@]}")
  elif [ "${#alias_tools[@]}" -gt 0 ]; then
    alias_targets=("${alias_tools[@]}")
  elif $yes_all; then
    alias_targets=("${TOOLS[@]}")
  elif ! $non_interactive && [ -t 0 ]; then
    read -r -p "Install command aliases for all tools? [y/N] " answer
    case "${answer,,}" in
      y|yes)
        alias_targets=("${TOOLS[@]}")
        ;;
    esac
  fi
fi

if [ "${#alias_targets[@]}" -gt 0 ]; then
  ensure_rc_line "${HOME}/.zshrc" "ai-cli: alias path" "export PATH=\"${ALIAS_DIR}:\$PATH\""
  ensure_rc_line "${HOME}/.bashrc" "ai-cli: alias path" "export PATH=\"${ALIAS_DIR}:\$PATH\""
fi

for tool in "${TOOLS[@]}"; do
  enabled_alias=false
  for selected in "${alias_targets[@]}"; do
    if [ "$selected" = "$tool" ]; then
      enabled_alias=true
      break
    fi
  done

  if $enabled_alias; then
    current_bin="$(command -v "$tool" || true)"
    if [ -n "$current_bin" ] && [ "$current_bin" != "${ALIAS_DIR}/${tool}" ]; then
      set_config_value "$tool" "$current_bin" "true"
    else
      set_config_value "$tool" "__KEEP__" "true"
    fi
    ln -sf "$AI_CLI_BIN" "${ALIAS_DIR}/${tool}"
    chmod +x "${ALIAS_DIR}/${tool}"
    echo "Alias installed: ${ALIAS_DIR}/${tool} -> ${AI_CLI_BIN}"
  else
    set_config_value "$tool" "__KEEP__" "false"
  fi
done

# ---------------------------------------------------------------------------
# Install underlying CLI tools (optional)
# ---------------------------------------------------------------------------

# Detect which package managers are available
has_npm=false; command -v npm >/dev/null 2>&1 && has_npm=true
has_brew=false; command -v brew >/dev/null 2>&1 && has_brew=true
has_port=false; command -v port >/dev/null 2>&1 && has_port=true
has_curl=false; command -v curl >/dev/null 2>&1 && has_curl=true

# Per-tool method info: tool -> "method1:label1|method2:label2|..."
# First listed = recommended; native installers preferred
declare -A TOOL_METHODS
TOOL_METHODS=(
  [claude]="native:Native installer (recommended, auto-updates)|brew:Homebrew (brew install --cask)|npm:npm"
  [codex]="npm:npm (recommended)|brew:Homebrew (brew install --cask)"
  [copilot]="npm:npm (recommended)|brew:Homebrew"
  [gemini]="npm:npm (recommended)|brew:Homebrew|macports:MacPorts"
)

# Check if a method's prerequisite is available
method_available() {
  case "$1" in
    native) $has_curl ;;
    npm|npx) $has_npm ;;
    brew) $has_brew ;;
    macports) $has_port ;;
    *) return 0 ;;
  esac
}

# Pick the best available method for a tool (first available in preference order)
auto_detect_method() {
  local tool="$1"
  local methods="${TOOL_METHODS[$tool]:-}"
  IFS='|' read -ra entries <<< "$methods"
  for entry in "${entries[@]}"; do
    local method="${entry%%:*}"
    if method_available "$method"; then
      echo "$method"
      return
    fi
  done
  echo ""
}

# Show method choices and prompt user for a tool
prompt_method_for_tool() {
  local tool="$1"
  local methods="${TOOL_METHODS[$tool]:-}"
  local default_method
  default_method="$(auto_detect_method "$tool")"

  if [ -z "$methods" ]; then
    echo "$default_method"
    return
  fi

  IFS='|' read -ra entries <<< "$methods"
  local available_entries=()
  for entry in "${entries[@]}"; do
    local method="${entry%%:*}"
    if method_available "$method"; then
      available_entries+=("$entry")
    fi
  done

  # Only one option — use it
  if [ "${#available_entries[@]}" -le 1 ]; then
    echo "$default_method"
    return
  fi

  echo "  Install methods for $tool:" >&2
  local i=1
  for entry in "${available_entries[@]}"; do
    local method="${entry%%:*}"
    local label="${entry#*:}"
    local marker=""
    [ "$method" = "$default_method" ] && marker=" [default]"
    echo "    $i) $label${marker}" >&2
    i=$((i + 1))
  done

  read -r -p "  Choose method [1]: " choice
  choice="${choice:-1}"
  if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -ge 1 ] && [ "$choice" -le "${#available_entries[@]}" ]; then
    local selected="${available_entries[$((choice - 1))]}"
    echo "${selected%%:*}"
  else
    echo "$default_method"
  fi
}

install_one_tool() {
  local tool="$1"
  local method="$2"
  if [ -n "$method" ]; then
    python3 -m ai_cli.update "$tool" --method "$method" || true
  else
    python3 -m ai_cli.update "$tool" || true
  fi
}

install_cli_tools() {
  if $install_tools; then
    # --install-tools: install all, auto-detect or use --method
    echo
    echo "Installing all CLI tools..."
    for tool in "${TOOLS[@]}"; do
      local method="$install_method"
      if [ -z "$method" ]; then
        method="$(auto_detect_method "$tool")"
      fi
      echo
      install_one_tool "$tool" "$method"
    done
  elif [ "${#install_tool_list[@]}" -gt 0 ]; then
    # --install-tool TOOL: specific tools
    for tool in "${install_tool_list[@]}"; do
      local method="$install_method"
      if [ -z "$method" ] && ! $non_interactive && [ -t 0 ]; then
        method="$(prompt_method_for_tool "$tool")"
      elif [ -z "$method" ]; then
        method="$(auto_detect_method "$tool")"
      fi
      echo
      install_one_tool "$tool" "$method"
    done
  elif $yes_all; then
    for tool in "${TOOLS[@]}"; do
      local method="$install_method"
      if [ -z "$method" ]; then
        method="$(auto_detect_method "$tool")"
      fi
      echo
      install_one_tool "$tool" "$method"
    done
  elif ! $non_interactive && [ -t 0 ]; then
    # Interactive: ask per tool
    echo
    echo "=== CLI Tool Installation ==="
    echo "Available tools: ${TOOLS[*]}"
    echo
    read -r -p "Install underlying CLI tools? [y/N] " answer
    case "${answer,,}" in
      y|yes)
        for tool in "${TOOLS[@]}"; do
          echo
          # Check if already installed
          if command -v "$tool" >/dev/null 2>&1; then
            local ver
            ver="$("$tool" --version 2>/dev/null || echo "unknown")"
            read -r -p "$tool is already installed ($ver). Reinstall/update? [y/N] " up_answer
            case "${up_answer,,}" in
              y|yes) ;;
              *) continue ;;
            esac
          fi

          local method
          if [ -n "$install_method" ]; then
            method="$install_method"
          else
            method="$(prompt_method_for_tool "$tool")"
          fi

          if [ -n "$method" ]; then
            install_one_tool "$tool" "$method"
          else
            echo "  No available install method for $tool (missing npm/brew/etc). Skipping."
          fi
        done
        ;;
    esac
  fi
}

install_cli_tools

# ---------------------------------------------------------------------------
# Regenerate completions (picks up flags from any newly installed tools)
# ---------------------------------------------------------------------------

echo
echo "Regenerating shell completions..."
python3 -m ai_cli.completion_gen generate --shell all 2>/dev/null || true

# Reinstall the freshly generated completions
copy_completions

# ---------------------------------------------------------------------------
# Source the user's rc file to load new completions into current shell
# ---------------------------------------------------------------------------

source_rc_file() {
  local shell_name
  shell_name="$(basename "${SHELL:-}")"
  case "$shell_name" in
    zsh)
      if [ -f "${HOME}/.zshrc" ]; then
        echo "Sourcing ~/.zshrc to load completions..."
        # Can't source zsh rc from bash; print instructions instead
        echo "  Run: source ~/.zshrc"
        echo "  (or open a new terminal)"
      fi
      ;;
    bash)
      if [ -f "${HOME}/.bashrc" ]; then
        echo "Sourcing ~/.bashrc to load completions..."
        # shellcheck disable=SC1091
        source "${HOME}/.bashrc" 2>/dev/null || true
      fi
      ;;
    *)
      echo "Open a new shell to pick up completions."
      ;;
  esac
}

source_rc_file

echo
echo "Install complete."
echo "- ai-cli bin: $AI_CLI_BIN"
echo
echo "To install/update individual tools later:"
echo "  ai-cli update --list-methods     # show available install methods"
echo "  ai-cli update gemini             # auto-detect best method"
echo "  ai-cli update gemini -m brew     # install with Homebrew"
echo "  ai-cli update claude -m native   # native installer (auto-updates)"
echo "  ai-cli update --all              # install/update all tools"

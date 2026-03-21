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

# UI state
DEBUG=false
SPIN_PID=0

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
  --method METHOD      Install method for tools (for example: npm, brew, macports, curl, stable, prerelease, latest, preview, nightly)
  --auto-install-deps  Allow installer to install required system deps (tmux)
  --yes, -y            Assume yes for interactive confirmations
  --non-interactive    Never prompt; use provided flags only
  --debug              Show verbose output from background commands
  --help, -h           Show this help
EOF
}

# --- Visual UI Helpers ---

# Spinner function to run in background
spinner() {
  local msg="$1"
  local delay=0.3
  local frames=("..." ".." "." ".." "...")
  while true; do
    for frame in "${frames[@]}"; do
      printf "\r  [ %s ] %s    " "$frame" "$msg"
      sleep "$delay"
    done
  done
}

# Start a named stage with a spinner
start_stage() {
  if $DEBUG; then
    echo "--- Stage: $1 ---"
  else
    spinner "$1" &
    SPIN_PID=$!
  fi
}

# Stop the spinner and mark stage as done
stop_stage() {
  local msg="${1:-Done}"
  if ! $DEBUG && [ "$SPIN_PID" -ne 0 ]; then
    kill "$SPIN_PID" >/dev/null 2>&1 || true
    wait "$SPIN_PID" 2>/dev/null || true
    SPIN_PID=0
    # \r = return to start, \033[K = clear to end of line
    printf "\r\033[K  [ OK ] %s\n" "$msg"
  elif $DEBUG; then
    echo "  [ OK ] $msg"
  fi
}

# Run a command with hidden output unless DEBUG is true
run_quiet() {
  if $DEBUG; then
    "$@"
  else
    "$@" >/dev/null 2>&1
  fi
}

# --- Core Functions ---

ensure_rc_line() {
  local rc_file="$1"
  local marker="$2"
  local line="$3"
  [ -f "$rc_file" ] || return 0
  if ! grep -qF "$marker" "$rc_file"; then
    printf '\n# %s\n%s\n' "$marker" "$line" >> "$rc_file"
    if $DEBUG; then echo "Updated $rc_file ($marker)"; fi
  fi
}

set_config_value() {
  local tool="$1"
  local binary="$2"
  local alias_state="$3"

  run_quiet python3 - "$tool" "$binary" "$alias_state" <<'PY'
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

tool_managed_binary() {
  local tool="$1"
  python3 - "$tool" <<'PY'
import sys
from ai_cli.tools import load_registry

tool = sys.argv[1]
spec = load_registry().get(tool)
if spec and spec.managed_binary:
    print(spec.resolve_binary(spec.managed_binary))
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
  else
    zsh_comp_dir="${HOME}/.zsh/completions"
    mkdir -p "$zsh_comp_dir"
    cp "$comp_src/_ai-cli" "$zsh_comp_dir/_ai-cli"
    ensure_rc_line "${HOME}/.zshrc" "ai-cli: zsh completion path" "fpath=(\"${zsh_comp_dir}\" \$fpath)"
    ensure_rc_line "${HOME}/.zshrc" "ai-cli: compinit" "autoload -Uz compinit && compinit"
  fi

  # Bash
  local bash_comp_dir="${HOME}/.local/share/bash-completion/completions"
  mkdir -p "$bash_comp_dir"
  cp "$comp_src/ai-cli.bash" "$bash_comp_dir/ai-cli"
  ensure_rc_line "${HOME}/.bashrc" "ai-cli: bash completion" "[ -f \"${bash_comp_dir}/ai-cli\" ] && source \"${bash_comp_dir}/ai-cli\""
}

install_statusline() {
  mkdir -p "$CLAUDE_DIR"
  cp "${SCRIPT_DIR}/statusline/statusline-command.sh" "$STATUSLINE_DEST"
  chmod +x "$STATUSLINE_DEST"

  local settings="${CLAUDE_DIR}/settings.json"
  local status_json='{"type":"command","command":"~/.claude/statusline-command.sh"}'

  if [ ! -f "$settings" ]; then
    printf '{\n  "statusLine": %s\n}\n' "$status_json" > "$settings"
    return
  fi

  if command -v jq >/dev/null 2>&1; then
    local tmp
    tmp="$(mktemp)"
    jq --argjson sl "$status_json" '. + {statusLine: $sl} | del(.statusCommand)' "$settings" > "$tmp"
    mv "$tmp" "$settings"
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
    --reinstall|-f) reinstall=true ;;
    --alias-all) alias_all=true ;;
    --alias)
      shift
      if [[ $# -eq 0 ]]; then echo "--alias requires a tool name" >&2; exit 1; fi
      alias_tools+=("$1")
      ;;
    --no-alias) no_alias=true ;;
    --install-tools) install_tools=true ;;
    --install-tool)
      shift
      if [[ $# -eq 0 ]]; then echo "--install-tool requires a tool name" >&2; exit 1; fi
      install_tool_list+=("$1")
      ;;
    --method)
      shift
      if [[ $# -eq 0 ]]; then echo "--method requires a method (for example: npm, brew, macports, curl, stable, prerelease, latest, preview, nightly)" >&2; exit 1; fi
      install_method="$1"
      ;;
    --auto-install-deps) auto_install_deps=true ;;
    --yes|-y) yes_all=true ;;
    --non-interactive) non_interactive=true ;;
    --debug) DEBUG=true ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage; exit 1 ;;
  esac
  shift
done

echo "=== AI Cli Toolkit Installer ==="
echo

if ! command -v python3 >/dev/null 2>&1; then
  echo "Error: python3 is required" >&2
  exit 1
fi

# --- Dependency Check (System) ---
REQUIRED_DEPS=(tmux rsync curl git)
MISSING_DEPS=()
for dep in "${REQUIRED_DEPS[@]}"; do
  if ! command -v "$dep" >/dev/null 2>&1; then
    MISSING_DEPS+=("$dep")
  fi
done

if [ "${#MISSING_DEPS[@]}" -gt 0 ]; then
  if ! $auto_install_deps; then
    if $non_interactive; then
      echo "Error: Missing system dependencies: ${MISSING_DEPS[*]}. Re-run with --auto-install-deps." >&2
      exit 1
    fi
    if ! $yes_all && [ -t 0 ]; then
      read -r -p "Install missing system dependencies (${MISSING_DEPS[*]}) automatically? [y/N] " deps_answer
      case "${deps_answer,,}" in
        y|yes) auto_install_deps=true ;;
      esac
    elif $yes_all; then
      auto_install_deps=true
    fi
  fi

  if ! $auto_install_deps; then
    echo "Please install ${MISSING_DEPS[*]} manually, then re-run install.sh." >&2
    exit 1
  fi

  start_stage "Installing system dependencies"
  if command -v brew >/dev/null 2>&1; then run_quiet brew install "${MISSING_DEPS[@]}"
  elif command -v apt-get >/dev/null 2>&1; then
    run_quiet sudo apt-get update
    run_quiet sudo apt-get install -y "${MISSING_DEPS[@]}"
  elif command -v dnf >/dev/null 2>&1; then run_quiet sudo dnf install -y "${MISSING_DEPS[@]}"
  elif command -v pacman >/dev/null 2>&1; then run_quiet sudo pacman -S --noconfirm "${MISSING_DEPS[@]}"
  else
    stop_stage "Failed to auto-install dependencies. Unknown package manager."
    exit 1
  fi
  stop_stage "System dependencies installed"
fi

mkdir -p "$BIN_DIR"

# --- Python Package ---
start_stage "Installing Python package"
install_python_package() {
  local pip_flags=(-q)
  if $reinstall; then pip_flags+=(--upgrade --force-reinstall); fi

  local -a pip_user_cmd=(/usr/bin/env python3 -m pip install "${pip_flags[@]}" --user -e "$SCRIPT_DIR")
  local -a pip_venv_cmd=(/usr/bin/env python3 -m pip install "${pip_flags[@]}" -e "$SCRIPT_DIR")
  local user_install_error="Can not perform a '--user' install. User site-packages are not visible in this virtualenv."

  local output
  set +e
  output="$("${pip_user_cmd[@]}" 2>&1)"
  local status=$?
  set -e
  if [ $status -eq 0 ]; then return 0; fi

  if printf '%s' "$output" | grep -qF "$user_install_error"; then
    run_quiet "${pip_venv_cmd[@]}"
    return 0
  fi
  return "$status"
}
if install_python_package; then
  stop_stage "Python package installed"
else
  stop_stage "Python package installation failed"
  exit 1
fi

# --- ai-mux ---
INSTALLED_MUX_DIR="${HOME}/.ai-cli/bin"
if [ -d "$MUX_DIR" ] && command -v cargo >/dev/null 2>&1; then
  start_stage "Building ai-mux (tmux orchestrator)"
  if run_quiet bash -c "cd \"$MUX_DIR\" && cargo build --release"; then
    if [ -x "$MUX_DIR/target/release/ai-mux" ]; then
      mkdir -p "$PKG_MUX_DIR" "$INSTALLED_MUX_DIR"
      cp "$MUX_DIR/target/release/ai-mux" "$PKG_MUX_DIR/ai-mux"
      chmod +x "$PKG_MUX_DIR/ai-mux"
      cp "$MUX_DIR/target/release/ai-mux" "$BIN_DIR/ai-mux"
      chmod +x "$BIN_DIR/ai-mux"
      cp "$MUX_DIR/target/release/ai-mux" "$INSTALLED_MUX_DIR/ai-mux"
      chmod +x "$INSTALLED_MUX_DIR/ai-mux"
      xattr -cr "$INSTALLED_MUX_DIR/ai-mux" 2>/dev/null || true
      stop_stage "ai-mux built and installed"
    else
      stop_stage "ai-mux binary not found"
    fi
  else
    stop_stage "ai-mux build failed"
  fi
fi

# --- Executable & PATH ---
start_stage "Configuring environment"
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
install_statusline
stop_stage "Environment configured"

# --- Aliases ---
mkdir -p "$ALIAS_DIR"
alias_targets=()

if ! $no_alias; then
  if $alias_all; then alias_targets=("${TOOLS[@]}")
  elif [ "${#alias_tools[@]}" -gt 0 ]; then alias_targets=("${alias_tools[@]}")
  elif $yes_all; then alias_targets=("${TOOLS[@]}")
  elif ! $non_interactive && [ -t 0 ]; then
    printf "\n"
    read -r -p "Install command aliases for all tools? [y/N] " answer
    case "${answer,,}" in y|yes) alias_targets=("${TOOLS[@]}") ;; esac
  fi
fi

if [ "${#alias_targets[@]}" -gt 0 ]; then
  start_stage "Installing aliases"
  ensure_rc_line "${HOME}/.zshrc" "ai-cli: alias path" "export PATH=\"${ALIAS_DIR}:\$PATH\""
  ensure_rc_line "${HOME}/.bashrc" "ai-cli: alias path" "export PATH=\"${ALIAS_DIR}:\$PATH\""

  for tool in "${TOOLS[@]}"; do
    enabled_alias=false
    for selected in "${alias_targets[@]}"; do
      if [ "$selected" = "$tool" ]; then enabled_alias=true; break; fi
    done

    if $enabled_alias; then
      managed_bin="$(tool_managed_binary "$tool")"
      current_bin="$(command -v "$tool" || true)"
      if [ -n "$managed_bin" ]; then
        set_config_value "$tool" "$managed_bin" "true"
      elif [ -n "$current_bin" ] && [ "$current_bin" != "${ALIAS_DIR}/${tool}" ]; then
        set_config_value "$tool" "$current_bin" "true"
      else
        set_config_value "$tool" "__KEEP__" "true"
      fi
      ln -sf "$AI_CLI_BIN" "${ALIAS_DIR}/${tool}"
      chmod +x "${ALIAS_DIR}/${tool}"
    else
      set_config_value "$tool" "__KEEP__" "false"
    fi
  done
  stop_stage "Aliases installed"
fi

# --- CLI Tools ---
has_npm=false; command -v npm >/dev/null 2>&1 && has_npm=true
has_brew=false; command -v brew >/dev/null 2>&1 && has_brew=true
has_port=false; command -v port >/dev/null 2>&1 && has_port=true
has_curl=false; command -v curl >/dev/null 2>&1 && has_curl=true

declare -A TOOL_METHODS
TOOL_METHODS=(
  [claude]="native:Native installer|brew:Homebrew|npm:npm"
  [codex]="npm:npm|brew:Homebrew"
  [copilot]="stable:Stable|prerelease:Prerelease"
  [gemini]="latest:Latest|preview:Preview|nightly:Nightly|brew:Homebrew|macports:MacPorts"
)

method_available() {
  case "$1" in
    native) $has_curl ;;
    npm|npx|latest|preview|nightly) $has_npm ;;
    brew) $has_brew ;;
    macports) $has_port ;;
    *) return 0 ;;
  esac
}

auto_detect_method() {
  local tool="$1"
  local methods="${TOOL_METHODS[$tool]:-}"
  IFS='|' read -ra entries <<< "$methods"
  for entry in "${entries[@]}"; do
    local method="${entry%%:*}"
    if method_available "$method"; then echo "$method"; return; fi
  done
  echo ""
}

prompt_method_for_tool() {
  local tool="$1"
  local methods="${TOOL_METHODS[$tool]:-}"
  local default_method
  default_method="$(auto_detect_method "$tool")"
  if [ -z "$methods" ]; then echo "$default_method"; return; fi
  IFS='|' read -ra entries <<< "$methods"
  local available_entries=()
  for entry in "${entries[@]}"; do
    local method="${entry%%:*}"
    if method_available "$method"; then available_entries+=("$entry"); fi
  done
  if [ "${#available_entries[@]}" -le 1 ]; then echo "$default_method"; return; fi

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
  start_stage "Installing $tool ($method)"
  if [ -n "$method" ]; then
    run_quiet python3 -m ai_cli.update "$tool" --method "$method"
  else
    run_quiet python3 -m ai_cli.update "$tool"
  fi
  stop_stage "Installed $tool"
}

if $install_tools; then
  for tool in "${TOOLS[@]}"; do
    install_one_tool "$tool" "${install_method:-$(auto_detect_method "$tool")}"
  done
elif [ "${#install_tool_list[@]}" -gt 0 ]; then
  for tool in "${install_tool_list[@]}"; do
    install_one_tool "$tool" "${install_method:-$(auto_detect_method "$tool")}"
  done
elif $yes_all; then
  for tool in "${TOOLS[@]}"; do
    install_one_tool "$tool" "${install_method:-$(auto_detect_method "$tool")}"
  done
elif ! $non_interactive && [ -t 0 ]; then
  ANY_TOOL_MISSING=false
  for tool in "${TOOLS[@]}"; do
    if ! command -v "$tool" >/dev/null 2>&1; then
      ANY_TOOL_MISSING=true
      break
    fi
  done

  if $ANY_TOOL_MISSING; then
    printf "\n=== CLI Tool Installation ===\n"
    read -r -p "Install underlying CLI tools? [y/N] " answer
    case "${answer,,}" in
      y|yes)
        for tool in "${TOOLS[@]}"; do
          if command -v "$tool" >/dev/null 2>&1; then
            read -r -p "$tool is already installed. Reinstall? [y/N] " up_answer
            case "${up_answer,,}" in y|yes) ;; *) continue ;; esac
          fi
          method="${install_method:-$(prompt_method_for_tool "$tool")}"
          [ -n "$method" ] && install_one_tool "$tool" "$method"
        done
        ;;
    esac
  fi
fi

# --- Final Steps ---
start_stage "Regenerating completions"
run_quiet bash -c "python3 -m ai_cli.completion_gen generate --shell all"
copy_completions
stop_stage "Completions updated"

echo
echo "Install complete."
echo "- ai-cli bin: $AI_CLI_BIN"
echo
echo "To finish, run: source ~/.zshrc (or ~/.bashrc)"

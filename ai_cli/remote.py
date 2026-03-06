"""Remote folder proxy — rsync-based bidirectional sync for user@host:/path specs.

When a tool is launched with a ``user@host:/path`` directory argument, this
module rsyncs the remote tree into a local tmpdir, lets the tool work locally,
then mirrors edits back with rsync on exit.  rsync is preferred over scp
because it transfers only changed bytes (delta compression) and can propagate
file deletions with ``--delete``.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, TextIO

# Pattern: user@host:/path  or  user@host:path  (colon required to disambiguate)
_REMOTE_RE = re.compile(
    r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9._-]+):(?P<path>.+)$"
)

_SSH_OPTS = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"


@dataclass(frozen=True)
class RemoteSpec:
    """Parsed remote folder specification."""

    user: str
    host: str
    path: str

    @property
    def ssh_target(self) -> str:
        """Return ``user@host`` for SSH/rsync commands."""
        return f"{self.user}@{self.host}"

    @property
    def display(self) -> str:
        return f"{self.user}@{self.host}:{self.path}"


def parse_remote_spec(arg: str) -> Optional[RemoteSpec]:
    """Parse a ``user@host:/path`` string. Returns *None* if not a remote spec."""
    m = _REMOTE_RE.match(arg.strip())
    if m is None:
        return None
    return RemoteSpec(user=m.group("user"), host=m.group("host"), path=m.group("path"))


def is_remote_spec(arg: str) -> bool:
    """Quick check whether *arg* looks like a remote folder spec."""
    return _REMOTE_RE.match(arg.strip()) is not None


def make_local_mirror(spec: RemoteSpec) -> Path:
    """Return a deterministic local mirror directory for *spec*, creating it if needed.

    Uses ``~/.ai-cli/remote/<host>__<slug>__<hash>/`` so repeated launches
    against the same remote reuse the same local dir (faster incremental rsync).
    """
    root = Path("~/.ai-cli/remote").expanduser()
    digest = hashlib.sha256(spec.display.encode()).hexdigest()[:16]
    safe_host = re.sub(r"[^A-Za-z0-9_-]", "_", spec.host)
    safe_path = re.sub(r"[^A-Za-z0-9_-]", "_", spec.path.strip("/"))[:40]
    local_dir = root / f"{safe_host}__{safe_path}__{digest}"
    local_dir.mkdir(parents=True, exist_ok=True)
    return local_dir


# ---------------------------------------------------------------------------
# rsync helpers
# ---------------------------------------------------------------------------

def _rsync_bin() -> str:
    path = shutil.which("rsync")
    if path is None:
        raise FileNotFoundError(
            "rsync is required for remote folder proxy but was not found on PATH.\n"
            "Install with: brew install rsync  (macOS) or apt install rsync  (Linux)"
        )
    return path


def _run_rsync(cmd: list[str], *, label: str) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"rsync {label} failed (rc={proc.returncode}): {proc.stderr.strip()}")


def sync_down(spec: RemoteSpec, local_dir: Path) -> None:
    """Pull remote folder contents into *local_dir* via rsync."""
    local_dir.mkdir(parents=True, exist_ok=True)
    src = f"{spec.ssh_target}:{spec.path.rstrip('/')}/"
    _run_rsync(
        [_rsync_bin(), "-az", "--delete", "--ignore-existing", "--progress", "--exclude=conda/", "--exclude=runtime/tools/analytics/", "-e", _SSH_OPTS, src, str(local_dir) + "/"],
        label="download",
    )


def sync_up(spec: RemoteSpec, local_dir: Path) -> None:
    """Push local changes back to remote via rsync."""
    dest = f"{spec.ssh_target}:{spec.path.rstrip('/')}/"
    _run_rsync(
        [_rsync_bin(), "-az", "--delete", "--progress", "--ignore-existing", "--exclude=conda/", "--exclude=runtime/tools/analytics/", "-e", _SSH_OPTS, str(local_dir) + "/", dest],
        label="upload",
    )


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------

def verify_ssh(spec: RemoteSpec) -> None:
    """Quick SSH connectivity + remote dir existence check; raises on failure."""
    ssh_cmd = ["ssh"] + _SSH_OPTS.split()[1:] + [spec.ssh_target]
    # connectivity
    proc = subprocess.run(
        [*ssh_cmd, "echo ok"], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Cannot connect to {spec.ssh_target}: {proc.stderr.strip()}\n"
            "Ensure SSH key-based auth is configured for this host."
        )
    # directory exists
    proc = subprocess.run(
        [*ssh_cmd, f"test -d {spec.path!r}"], capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Remote directory does not exist: {spec.display}\n"
            f"Create it first:  ssh {spec.ssh_target} 'mkdir -p {spec.path}'"
        )


def resolve_remote_tool_env(
    spec: RemoteSpec,
    tool_name: str,
    *,
    real_home: str,
) -> tuple[str, str]:
    """Resolve a real remote tool binary and usable PATH outside tmux."""
    import shlex as _shlex

    path_py = (
        "import os; "
        "drop = os.path.join(os.environ[\"REAL_HOME\"], \".ai-cli/bin\"); "
        "print(\":\".join(p for p in os.environ.get(\"PATH\", \"\").split(\":\") "
        "if p and p != drop))"
    )
    # Resolve tilde-prefixed binaries to $REAL_HOME and also try bare name
    bare_name = Path(tool_name).name  # e.g. "claude"
    if tool_name.startswith("~/"):
        explicit_path = "$REAL_HOME/" + tool_name[2:]
    elif tool_name.startswith("$HOME/"):
        explicit_path = "$REAL_HOME/" + tool_name[6:]
    else:
        explicit_path = ""
    bare_q = _shlex.quote(bare_name)
    home_q = _shlex.quote(real_home)
    # Try command -v on the bare name first; fall back to explicit tilde-expanded path
    if explicit_path:
        resolve_expr = (
            f'_ai_cli_bin=$(command -v {bare_q} 2>/dev/null || true)'
            f' ; [ -z "$_ai_cli_bin" ] && [ -x {explicit_path} ] && _ai_cli_bin={explicit_path}'
        )
    else:
        resolve_expr = f'_ai_cli_bin=$(command -v {bare_q} 2>/dev/null || true)'
    remote_cmd = (
        f"export REAL_HOME={home_q}"
        " ; . /etc/profile 2>/dev/null"
        " ; . $REAL_HOME/.profile 2>/dev/null"
        " ; . $REAL_HOME/.bashrc 2>/dev/null"
        " ; . $REAL_HOME/.zshrc 2>/dev/null"
        # nvm/fnm/volta often guard on interactive mode; source explicitly
        ' ; [ -s "${NVM_DIR:-$REAL_HOME/.nvm}/nvm.sh" ] && . "${NVM_DIR:-$REAL_HOME/.nvm}/nvm.sh" 2>/dev/null'
        ' ; [ -s "$REAL_HOME/.config/fnm/fnm_multishells" ] && eval "$(fnm env 2>/dev/null)" 2>/dev/null'
        ' ; [ -d "$REAL_HOME/.volta" ] && export VOLTA_HOME="$REAL_HOME/.volta" && export PATH="$VOLTA_HOME/bin:$PATH"'
        f" ; export REAL_HOME={home_q}"
        f" ; export PATH=$(python3 -c {_shlex.quote(path_py)})"
        " ; unalias codex claude gemini copilot 2>/dev/null || true"
        " ; hash -r 2>/dev/null || true"
        f" ; {resolve_expr}"
        " ; printf 'AI_CLI_REMOTE_PATH=%s\\nAI_CLI_REMOTE_BIN=%s\\n' \"$PATH\" \"$_ai_cli_bin\""
    )
    proc = subprocess.run(
        ["ssh", *_SSH_OPTS.split()[1:], spec.ssh_target, remote_cmd],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Could not resolve remote {tool_name} on {spec.ssh_target}: "
            f"{proc.stderr.strip() or 'ssh command failed'}"
        )

    resolved_path = ""
    resolved_bin = ""
    for line in proc.stdout.splitlines():
        if line.startswith("AI_CLI_REMOTE_PATH="):
            resolved_path = line.removeprefix("AI_CLI_REMOTE_PATH=")
        elif line.startswith("AI_CLI_REMOTE_BIN="):
            resolved_bin = line.removeprefix("AI_CLI_REMOTE_BIN=")

    if not resolved_bin:
        raise RuntimeError(
            f"Could not resolve remote {tool_name} on {spec.ssh_target}: "
            "tool not found after loading shell profiles"
        )
    if not resolved_path:
        raise RuntimeError(
            f"Could not resolve remote {tool_name} on {spec.ssh_target}: "
            "PATH probe returned an empty value"
        )
    return resolved_bin, resolved_path


def install_remote_tool(
    spec: RemoteSpec,
    tool_name: str,
    install_command: str,
    *,
    real_home: str,
) -> None:
    """Run *install_command* on the remote host via SSH.

    Sources the same shell profiles and node-version-manager scripts used by
    :func:`resolve_remote_tool_env` so that ``npm`` / ``npx`` are available
    even when they're managed by nvm/fnm/volta.
    """
    import shlex as _shlex

    home_q = _shlex.quote(real_home)
    remote_cmd = (
        f"export REAL_HOME={home_q}"
        " ; export HOME=$REAL_HOME"
        " ; . /etc/profile 2>/dev/null"
        " ; . $REAL_HOME/.profile 2>/dev/null"
        " ; . $REAL_HOME/.bashrc 2>/dev/null"
        " ; . $REAL_HOME/.zshrc 2>/dev/null"
        ' ; [ -s "${NVM_DIR:-$REAL_HOME/.nvm}/nvm.sh" ] && . "${NVM_DIR:-$REAL_HOME/.nvm}/nvm.sh" 2>/dev/null'
        ' ; [ -s "$REAL_HOME/.config/fnm/fnm_multishells" ] && eval "$(fnm env 2>/dev/null)" 2>/dev/null'
        ' ; [ -d "$REAL_HOME/.volta" ] && export VOLTA_HOME="$REAL_HOME/.volta" && export PATH="$VOLTA_HOME/bin:$PATH"'
        f" ; {install_command}"
    )
    print_sync_status(f"Installing {tool_name} on {spec.ssh_target}: {install_command}")
    proc = subprocess.run(
        ["ssh", *_SSH_OPTS.split()[1:], "-t", spec.ssh_target, remote_cmd],
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Failed to install {tool_name} on {spec.ssh_target} "
            f"(exit {proc.returncode})"
        )
    print_sync_status(f"Installed {tool_name} on {spec.ssh_target}")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

def cleanup_mirror(spec: RemoteSpec) -> bool:
    """Remove the local mirror directory for a remote spec. Returns True if removed."""
    d = make_local_mirror(spec)
    if d.is_dir():
        shutil.rmtree(d)
        return True
    return False


def print_sync_status(msg: str, *, file: TextIO | None = None) -> None:
    """Print a coloured status line for remote sync operations."""
    print(f"\033[1;36m[remote]\033[0m {msg}", file=file or sys.stderr)


# ---------------------------------------------------------------------------
# Remote Session Runner (uses remote-tty-wrapper)
# ---------------------------------------------------------------------------

def _find_remote_tty_wrapper() -> str:
    """Locate the bundled remote-tty-wrapper script."""
    bundled = Path(__file__).resolve().parent / "bin" / "remote-tty-wrapper"
    if bundled.is_file():
        return str(bundled)
    from_path = shutil.which("remote-tty-wrapper")
    if from_path:
        return from_path
    raise FileNotFoundError(
        "remote-tty-wrapper not found. Expected at:\n"
        f"  {bundled}\n"
        "or on PATH."
    )


class RemoteSessionRunner:
    """Run an AI tool inside a remote tmux session via remote-tty-wrapper.

    Instead of rsync-ing files locally, this launches the tool directly on the
    remote host so all file operations and shell commands happen natively.
    """

    def __init__(
        self,
        spec: RemoteSpec,
        session_name: str = "ai-cli",
        ssh_opts: Optional[list[str]] = None,
    ) -> None:
        self.spec = spec
        self.session_name = session_name
        self.ssh_opts = ssh_opts or []
        self._wrapper = _find_remote_tty_wrapper()

    def _base_cmd(self) -> list[str]:
        cmd = [self._wrapper, "-H", self.spec.ssh_target, "-s", self.session_name]
        for opt in self.ssh_opts:
            cmd.extend(["--ssh-opt", opt])
        return cmd

    def start(self, init_cmd: str = "") -> None:
        """Ensure the remote tmux session exists, optionally running *init_cmd*."""
        cmd = self._base_cmd() + ["start"]
        if init_cmd:
            cmd += ["--init", init_cmd]
        print_sync_status(f"Starting remote session '{self.session_name}' on {self.spec.ssh_target}")
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"remote-tty-wrapper start failed (rc={proc.returncode})"
            )

    def send(self, *commands: str) -> None:
        """Send one or more command lines into the remote tmux session."""
        if not commands:
            return
        if len(commands) == 1:
            cmd = self._base_cmd() + ["send", commands[0]]
        else:
            cmd = self._base_cmd() + ["send"]
            for c in commands:
                cmd += ["--", c]
        proc = subprocess.run(cmd, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"remote-tty-wrapper send failed (rc={proc.returncode})"
            )

    def shell(self, init_cmd: str = "") -> int:
        """Attach to the remote tmux session interactively. Returns exit code."""
        cmd = self._base_cmd() + ["shell"]
        if init_cmd:
            cmd += ["--init", init_cmd]
        print_sync_status(f"Attaching to remote session '{self.session_name}'")
        proc = subprocess.run(cmd, check=False)
        return proc.returncode

    # Remote path where we stash the CA cert for proxy trust
    _REMOTE_CA_PATH = "~/.ai-cli/remote-ca.pem"

    def _push_ca_cert(self, local_ca: Path) -> None:
        """Copy the local mitmproxy CA cert to the remote host."""
        if not local_ca.is_file():
            return
        dest = f"{self.spec.ssh_target}:{self._REMOTE_CA_PATH}"
        # Ensure the remote directory exists, then scp the cert
        ssh_base = [
            "ssh",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            "-o", "StrictHostKeyChecking=accept-new",
        ]
        subprocess.run(
            [*ssh_base, self.spec.ssh_target, "mkdir -p ~/.ai-cli"],
            check=False, capture_output=True,
        )
        proc = subprocess.run(
            ["scp", "-q", "-o", "BatchMode=yes", str(local_ca), dest],
            check=False, capture_output=True, text=True,
        )
        if proc.returncode != 0:
            print_sync_status(f"Warning: failed to push CA cert: {proc.stderr.strip()}")

    def run_attached(
        self,
        command: str,
        init_cmd: str = "",
        proxy_port: int = 0,
        ca_path: Optional[Path] = None,
        home_dir: Optional[str] = None,
        real_home: Optional[str] = None,
        launch_path: Optional[str] = None,
        tmux_socket: Optional[str] = None,
    ) -> int:
        """Create a tmux session running *command* as the pane process, then attach.

        When *proxy_port* is non-zero, an SSH reverse tunnel is established so
        that ``127.0.0.1:<proxy_port>`` on the remote reaches the local
        mitmproxy.  The tool's environment is set up with ``HTTP_PROXY``,
        ``HTTPS_PROXY``, ``SSL_CERT_FILE``, etc. pointing at the tunnel.

        When *home_dir* is set, the pane sources the real user's shell profile
        first (for PATH, nvm, conda, etc.) then overrides ``$HOME`` to point
        at the packaged session directory.

        When *tmux_socket* is set, all tmux commands use ``-L <socket>`` so
        the session lives on a dedicated, discoverable server.

        Any pre-existing session with the same name is killed first so we
        always get a fresh pane running the requested command.  When the tool
        exits the pane closes, ending the tmux session and SSH connection.
        """
        import shlex as _shlex

        tmux_L = f" -L {_shlex.quote(tmux_socket)}" if tmux_socket else ""
        tmux_conf = f" -f {_shlex.quote(home_dir + '/.tmux.conf')}" if home_dir else ""

        # ── Environment preamble ──────────────────────────────────────────
        env_parts: list[str] = []

        # Bootstrap the user's normal shell PATH, then remove ai-cli's
        # wrapper-bin directory so remote sessions invoke the real tool
        # binary instead of recursively calling back into ai-cli.
        real_home_expr = _shlex.quote(real_home) if real_home else "$HOME"
        shell_bootstrap = (
            f"export REAL_HOME={real_home_expr}"
            " ; . /etc/profile 2>/dev/null"
            " ; . $REAL_HOME/.profile 2>/dev/null"
            " ; . $REAL_HOME/.bashrc 2>/dev/null"
            " ; . $REAL_HOME/.zshrc 2>/dev/null"
            ' ; [ -s "${NVM_DIR:-$REAL_HOME/.nvm}/nvm.sh" ] && . "${NVM_DIR:-$REAL_HOME/.nvm}/nvm.sh" 2>/dev/null'
            ' ; [ -s "$REAL_HOME/.config/fnm/fnm_multishells" ] && eval "$(fnm env 2>/dev/null)" 2>/dev/null'
            ' ; [ -d "$REAL_HOME/.volta" ] && export VOLTA_HOME="$REAL_HOME/.volta" && export PATH="$VOLTA_HOME/bin:$PATH"'
            f" ; export REAL_HOME={real_home_expr}"
        )
        if launch_path:
            shell_bootstrap += f" ; export PATH={_shlex.quote(launch_path)}"
        else:
            shell_bootstrap += (
                " ; export PATH=$(python3 -c 'import os; "
                "drop = os.path.join(os.environ[\"REAL_HOME\"], \".ai-cli/bin\"); "
                "print(\":\".join(p for p in os.environ.get(\"PATH\", \"\").split(\":\") "
                "if p and p != drop))')"
            )
        shell_bootstrap += (
            " ; unalias codex claude gemini copilot 2>/dev/null || true"
            " ; hash -r 2>/dev/null || true"
        )
        if home_dir:
            home_q = _shlex.quote(home_dir)
            shell_bootstrap += (
                f" ; export HOME={home_q}"
                f" ; export ZDOTDIR={home_q}"
                f" ; export BASH_ENV={home_q}/.bash_env"
                f" ; export ENV={home_q}/.shrc"
                f" ; export KSHRC={home_q}/.kshrc"
            )
        env_parts.append(shell_bootstrap)

        # Proxy variables
        if proxy_port:
            proxy_url = f"http://127.0.0.1:{proxy_port}"
            env_parts += [
                f"export HTTP_PROXY={_shlex.quote(proxy_url)}",
                f"export HTTPS_PROXY={_shlex.quote(proxy_url)}",
                f"export http_proxy={_shlex.quote(proxy_url)}",
                f"export https_proxy={_shlex.quote(proxy_url)}",
            ]
            # CA cert paths — relative to the session dir when packaged
            if home_dir:
                remote_ca = f"{home_dir}/.ai-cli/remote-ca.pem"
            elif ca_path and ca_path.is_file():
                self._push_ca_cert(ca_path)
                remote_ca = self._REMOTE_CA_PATH
            else:
                remote_ca = ""
            if remote_ca:
                env_parts += [
                    f"export SSL_CERT_FILE={remote_ca}",
                    f"export REQUESTS_CA_BUNDLE={remote_ca}",
                    f"export NODE_EXTRA_CA_CERTS={remote_ca}",
                ]

        env_exports = " && ".join(env_parts) if env_parts else ""

        # ── Build pane command ────────────────────────────────────────────
        segments = []
        if env_exports:
            segments.append(env_exports)
        if init_cmd:
            segments.append(init_cmd)
        segments.append(command)
        pane_cmd = " && ".join(segments)

        sess_q = _shlex.quote(self.session_name)
        pane_q = _shlex.quote(pane_cmd)
        tmux_prefix = ""
        if home_dir:
            home_q = _shlex.quote(home_dir)
            tmux_prefix = (
                f"env HOME={home_q} ZDOTDIR={home_q} "
                f"BASH_ENV={home_q}/.bash_env ENV={home_q}/.shrc "
                f"KSHRC={home_q}/.kshrc "
            )
        # Kill any stale session, then create a fresh one with the command
        # as the pane process and attach.
        remote_cmd = (
            f"{tmux_prefix}tmux{tmux_conf}{tmux_L} kill-session -t {sess_q} 2>/dev/null;"
            f" {tmux_prefix}tmux{tmux_conf}{tmux_L} new-session -d -s {sess_q} {pane_q}"
            f" && {tmux_prefix}tmux{tmux_conf}{tmux_L} attach -t {sess_q}"
        )

        # ── SSH command ───────────────────────────────────────────────────
        ssh_cmd = [
            "ssh",
            "-o", "PermitLocalCommand=no",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "RequestTTY=force",
        ]
        # Reverse tunnel: remote port → local mitmproxy
        if proxy_port:
            ssh_cmd += ["-R", f"127.0.0.1:{proxy_port}:127.0.0.1:{proxy_port}"]
        for opt in self.ssh_opts:
            ssh_cmd.append(opt)
        ssh_cmd += [self.spec.ssh_target, remote_cmd]

        detail = f"proxy tunnel :{proxy_port}" if proxy_port else "no proxy"
        if home_dir:
            detail += f", HOME={home_dir}"
        print_sync_status(
            f"Launching in remote session '{self.session_name}' on "
            f"{self.spec.ssh_target} ({detail})"
        )
        proc = subprocess.run(ssh_cmd, check=False)
        return proc.returncode

    def exec_attached(
        self,
        command: str,
        proxy_port: int = 0,
        ca_path: Optional[Path] = None,
        home_dir: Optional[str] = None,
        real_home: Optional[str] = None,
        launch_path: Optional[str] = None,
    ) -> int:
        """Run a command directly on the remote host in an attached SSH TTY."""
        import shlex as _shlex

        env_parts: list[str] = []
        real_home_expr = _shlex.quote(real_home) if real_home else "$HOME"
        shell_bootstrap = (
            f"export REAL_HOME={real_home_expr}"
            " ; . /etc/profile 2>/dev/null"
            " ; . $REAL_HOME/.profile 2>/dev/null"
            " ; . $REAL_HOME/.bashrc 2>/dev/null"
            " ; . $REAL_HOME/.zshrc 2>/dev/null"
            ' ; [ -s "${NVM_DIR:-$REAL_HOME/.nvm}/nvm.sh" ] && . "${NVM_DIR:-$REAL_HOME/.nvm}/nvm.sh" 2>/dev/null'
            ' ; [ -s "$REAL_HOME/.config/fnm/fnm_multishells" ] && eval "$(fnm env 2>/dev/null)" 2>/dev/null'
            ' ; [ -d "$REAL_HOME/.volta" ] && export VOLTA_HOME="$REAL_HOME/.volta" && export PATH="$VOLTA_HOME/bin:$PATH"'
            f" ; export REAL_HOME={real_home_expr}"
        )
        if launch_path:
            shell_bootstrap += f" ; export PATH={_shlex.quote(launch_path)}"
        if home_dir:
            home_q = _shlex.quote(home_dir)
            shell_bootstrap += (
                f" ; export HOME={home_q}"
                f" ; export ZDOTDIR={home_q}"
                f" ; export BASH_ENV={home_q}/.bash_env"
                f" ; export ENV={home_q}/.shrc"
                f" ; export KSHRC={home_q}/.kshrc"
            )
        env_parts.append(shell_bootstrap)

        if proxy_port:
            proxy_url = f"http://127.0.0.1:{proxy_port}"
            env_parts += [
                f"export HTTP_PROXY={_shlex.quote(proxy_url)}",
                f"export HTTPS_PROXY={_shlex.quote(proxy_url)}",
                f"export http_proxy={_shlex.quote(proxy_url)}",
                f"export https_proxy={_shlex.quote(proxy_url)}",
            ]
            if home_dir:
                remote_ca = f"{home_dir}/.ai-cli/remote-ca.pem"
            elif ca_path and ca_path.is_file():
                self._push_ca_cert(ca_path)
                remote_ca = self._REMOTE_CA_PATH
            else:
                remote_ca = ""
            if remote_ca:
                env_parts += [
                    f"export SSL_CERT_FILE={remote_ca}",
                    f"export REQUESTS_CA_BUNDLE={remote_ca}",
                    f"export NODE_EXTRA_CA_CERTS={remote_ca}",
                ]

        remote_cmd = " && ".join([part for part in [" && ".join(env_parts), command] if part])

        ssh_cmd = [
            "ssh",
            "-o", "PermitLocalCommand=no",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-o", "RequestTTY=force",
        ]
        if proxy_port:
            ssh_cmd += ["-R", f"127.0.0.1:{proxy_port}:127.0.0.1:{proxy_port}"]
        for opt in self.ssh_opts:
            ssh_cmd.append(opt)
        ssh_cmd += [self.spec.ssh_target, remote_cmd]

        proc = subprocess.run(ssh_cmd, check=False)
        return proc.returncode

    _REMOTE_LOG_GLOBS = [
        "~/.ai-cli/logs/",
        "~/.claude/projects/",
        "~/.codex/sessions/",
        "~/.codex/projects/",
        "~/.copilot/sessions/",
        "~/.gemini/sessions/",
    ]

    def pull_logs(self, local_log_dir: Path, session_home: str = "") -> None:
        """Rsync tool logs and session data from the remote to the local host.

        When *session_home* is set, log globs are resolved relative to that
        directory (the packaged ``$HOME``).  Otherwise they resolve from the
        remote user's real ``~``.
        """
        local_log_dir.mkdir(parents=True, exist_ok=True)
        rsync_bin = shutil.which("rsync")
        if not rsync_bin:
            print_sync_status("Warning: rsync not found; skipping log pull")
            return

        if session_home:
            globs = [
                f"{session_home.rstrip('/')}/{g.lstrip('~/')}"
                for g in self._REMOTE_LOG_GLOBS
            ]
        else:
            globs = list(self._REMOTE_LOG_GLOBS)

        ssh_str = "ssh -o BatchMode=yes -o ConnectTimeout=10 -o StrictHostKeyChecking=accept-new"
        for remote_glob in globs:
            src = f"{self.spec.ssh_target}:{remote_glob}"
            safe_name = re.sub(r"[^A-Za-z0-9_-]", "-", remote_glob.strip("~/").strip("/"))
            dest = local_log_dir / f"remote-{self.spec.host}" / safe_name
            dest.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [rsync_bin, "-az", "--ignore-errors", "-e", ssh_str,
                 src, str(dest) + "/"],
                capture_output=True, text=True, check=False,
            )
            if proc.returncode == 0:
                print_sync_status(f"Pulled {remote_glob} → {dest}")

    def close(self) -> None:
        """Kill the remote tmux session."""
        cmd = self._base_cmd() + ["close"]
        subprocess.run(cmd, check=False)
        print_sync_status(f"Closed remote session '{self.session_name}'")

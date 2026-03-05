"""mitmdump lifecycle management: resolve, install, start, stop.

Handles finding or auto-installing mitmdump, building the command with
addon scripts, starting the proxy process, and setting up environment
variables for the wrapped CLI tool.
"""

from __future__ import annotations

import http.client
import http.server
import os
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any
from collections.abc import MutableMapping

from ai_cli.log import append_log, fmt_cmd, tail_file, tail_text

PINNED_MITM_ENV = "AI_CLI_PINNED_MITM_BIN"
PINNED_MITM_DIR = Path.home() / ".ai-cli" / "bin"
PINNED_MITMDUMP = PINNED_MITM_DIR / "mitmdump"


def _realpath_str(path: str) -> str:
    return os.path.realpath(os.path.expanduser(path))


def _is_pinned_mitmdump_path(path: str) -> bool:
    return _realpath_str(path) == _realpath_str(str(PINNED_MITMDUMP))


def _read_pinned_wrapper_target(wrapper_path: Path) -> str | None:
    """Return the wrapped target from a pinned mitmdump wrapper, if present."""
    try:
        text = wrapper_path.read_text(encoding="utf-8")
    except OSError:
        return None

    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith("exec "):
            continue
        try:
            parts = shlex.split(line)
        except ValueError:
            continue
        if len(parts) < 2:
            continue
        target = _realpath_str(parts[1])
        if target == _realpath_str(str(wrapper_path)):
            continue
        return target
    return None


def _prepend_path_dir(
    path_dir: str, env: MutableMapping[str, str] | None = None
) -> MutableMapping[str, str]:
    """Prepend *path_dir* to PATH in *env* (or os.environ), deduplicated."""
    target = os.environ if env is None else env
    current = target.get("PATH", "")
    parts = [part for part in current.split(os.pathsep) if part]
    parts = [part for part in parts if part != path_dir]
    parts.insert(0, path_dir)
    target["PATH"] = os.pathsep.join(parts)
    return target


def _prepend_user_bin_dirs() -> None:
    """Ensure common user-local bin directories are on PATH."""
    py_major = sys.version_info.major
    py_minor = sys.version_info.minor
    candidates = [
        Path.home() / ".local/bin",
        Path.home() / f"Library/Python/{py_major}.{py_minor}/bin",
    ]
    for candidate in candidates:
        _prepend_path_dir(str(candidate))


def _pin_mitmdump_binary(binary: str, log_path: Path | None = None) -> str:
    """Pin mitmdump to ~/.ai-cli/bin/mitmdump and prepend that dir to PATH."""
    resolved = _realpath_str(binary)
    pinned = str(PINNED_MITMDUMP)
    pinned_real = _realpath_str(pinned)

    # If caller passes the pinned wrapper path, unwrap to the real target so we
    # never generate an exec-to-self script.
    if resolved == pinned_real:
        target = _read_pinned_wrapper_target(PINNED_MITMDUMP)
        if target:
            resolved = target
        elif log_path is not None:
            append_log(
                log_path,
                "Warning: pinned mitmdump wrapper target could not be resolved; "
                "keeping existing wrapper unchanged.",
            )

    if resolved != pinned_real:
        try:
            PINNED_MITM_DIR.mkdir(parents=True, exist_ok=True)
            wrapper = (
                "#!/usr/bin/env bash\n"
                f"exec {shlex.quote(resolved)} \"$@\"\n"
            )
            PINNED_MITMDUMP.write_text(wrapper, encoding="utf-8")
            PINNED_MITMDUMP.chmod(0o755)
            selected = pinned
        except OSError as exc:
            selected = resolved
            if log_path is not None:
                append_log(
                    log_path,
                    f"Warning: failed to pin mitmdump at {pinned}: {exc}. "
                    f"Using resolved binary {resolved}.",
                )
    else:
        selected = pinned

    os.environ[PINNED_MITM_ENV] = selected
    os.environ["MITM_BIN"] = selected
    _prepend_path_dir(str(Path(selected).parent))
    return selected


def apply_pinned_mitmdump_path(env: dict[str, str]) -> dict[str, str]:
    """Ensure *env* resolves mitmdump to the pinned binary first."""
    pinned = (
        env.get(PINNED_MITM_ENV, "").strip()
        or os.environ.get(PINNED_MITM_ENV, "").strip()
        or env.get("MITM_BIN", "").strip()
        or os.environ.get("MITM_BIN", "").strip()
    )
    if not pinned:
        return env
    resolved = os.path.realpath(os.path.expanduser(pinned))
    env[PINNED_MITM_ENV] = resolved
    env["MITM_BIN"] = resolved
    _prepend_path_dir(str(Path(resolved).parent), env=env)
    return env


def _iter_path_executables(binary_name: str) -> list[str]:
    """Return executable matches for *binary_name* across PATH in order."""
    path_value = os.environ.get("PATH", "")
    if not path_value:
        return []

    matches: list[str] = []
    for raw_dir in path_value.split(os.pathsep):
        if not raw_dir:
            continue
        candidate = Path(raw_dir) / binary_name
        if not candidate.is_file():
            continue
        if not os.access(candidate, os.X_OK):
            continue
        matches.append(str(candidate))
    return matches


def _probe_mitmdump(binary: str) -> tuple[bool, str]:
    """Run a lightweight health-check against a mitmdump binary."""
    try:
        result = subprocess.run(
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)

    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        return False, output
    if "Mitmproxy:" not in output:
        return False, output
    return True, output


def resolve_mitmdump() -> str:
    """Find the mitmdump binary on PATH or known locations.

    Raises FileNotFoundError if not found, RuntimeError if unusable.
    """
    pinned_override = os.getenv(PINNED_MITM_ENV, "").strip()
    if pinned_override:
        resolved = shutil.which(pinned_override)
        if resolved:
            ok, details = _probe_mitmdump(resolved)
            if ok:
                return resolved
        # Stale runtime pin: drop it and continue normal discovery/install flow.
        os.environ.pop(PINNED_MITM_ENV, None)
        if _is_pinned_mitmdump_path(pinned_override):
            os.environ.pop("MITM_BIN", None)

    override = os.getenv("MITM_BIN", "").strip()
    if override:
        resolved = shutil.which(override)
        if not resolved:
            if _is_pinned_mitmdump_path(override):
                os.environ.pop("MITM_BIN", None)
            else:
                raise FileNotFoundError(
                    "MITM_BIN is set but does not resolve to an executable."
                )
        else:
            ok, details = _probe_mitmdump(resolved)
            if ok:
                return resolved
            if _is_pinned_mitmdump_path(override) or _is_pinned_mitmdump_path(resolved):
                # Recover from stale/broken internal pin by falling back to PATH
                # discovery instead of hard-failing.
                os.environ.pop("MITM_BIN", None)
            else:
                raise RuntimeError(
                    "MITM_BIN points to an unusable mitmdump binary. "
                    "Fix MITM_BIN or reinstall mitmproxy.\n"
                    f"{tail_text(details)}"
                )

    candidates: list[str] = []
    candidates.extend(_iter_path_executables("mitmdump"))
    candidates.extend(
        [
            "/opt/homebrew/bin/mitmdump",
            "/usr/local/bin/mitmdump",
        ]
    )

    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        unique.append(candidate)

    if not unique:
        raise FileNotFoundError(
            "mitmdump not found. Install mitmproxy or set MITM_BIN."
        )

    failures: list[str] = []
    for candidate in unique:
        ok, details = _probe_mitmdump(candidate)
        if ok:
            return candidate
        failures.append(f"{candidate}: {tail_text(details)}")

    joined = "\n".join(failures)
    raise RuntimeError(
        "Found mitmdump on PATH, but all candidates failed health checks.\n"
        f"{joined}"
    )


def _run_install_command(cmd: list[str]) -> tuple[bool, str]:
    """Run an install command. Returns (success, combined_output)."""
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
    except OSError as exc:
        return False, str(exc)
    combined = (result.stdout or "") + (result.stderr or "")
    if result.returncode == 0:
        return True, combined
    return False, f"exit={result.returncode}\n{combined}"


def _is_user_site_hidden_error(output: str) -> bool:
    """Return True when pip rejects --user inside a virtualenv."""
    return (
        "Can not perform a '--user' install." in output
        and "User site-packages are not visible in this virtualenv." in output
    )


def ensure_mitmdump(log_path: Path) -> str:
    """Find or auto-install mitmdump. Returns the binary path.

    Tries pipx, pip --user, and brew (macOS) in order.
    Raises FileNotFoundError if all attempts fail.
    """
    _prepend_user_bin_dirs()
    try:
        return _pin_mitmdump_binary(resolve_mitmdump(), log_path=log_path)
    except RuntimeError as exc:
        append_log(log_path, tail_text(f"Existing mitmdump is unusable:\n{exc}"))
    except FileNotFoundError:
        append_log(log_path, "mitmdump not found on PATH.")

    append_log(
        log_path, "Installing mitmproxy (first run setup or binary repair)."
    )

    install_attempts: list[list[str]] = []
    if shutil.which("pipx"):
        install_attempts.append(["pipx", "install", "--force", "mitmproxy"])
    install_attempts.append(
        [sys.executable, "-m", "pip", "install", "--user", "mitmproxy"]
    )
    if sys.platform == "darwin" and shutil.which("brew"):
        install_attempts.append(["brew", "install", "mitmproxy"])

    for attempt in install_attempts:
        append_log(log_path, f"Trying install command: {fmt_cmd(attempt)}")
        ok, output = _run_install_command(attempt)
        if not ok and "--user" in attempt and _is_user_site_hidden_error(output):
            retry = [part for part in attempt if part != "--user"]
            append_log(
                log_path,
                "pip rejected --user install in virtualenv; retrying without --user.",
            )
            append_log(log_path, f"Trying install command: {fmt_cmd(retry)}")
            retry_ok, retry_output = _run_install_command(retry)
            if retry_output.strip():
                output = f"{output}\n{retry_output}"
            ok = retry_ok
        if ok:
            _prepend_user_bin_dirs()
            try:
                return _pin_mitmdump_binary(resolve_mitmdump(), log_path=log_path)
            except RuntimeError as exc:
                append_log(
                    log_path,
                    tail_text(f"mitmdump still unhealthy after install attempt:\n{exc}"),
                )
            except FileNotFoundError:
                if output.strip():
                    append_log(log_path, tail_text(output))
                continue
        if output.strip():
            append_log(log_path, tail_text(output))

    raise FileNotFoundError(
        "Unable to install a usable mitmdump automatically. "
        "Install mitmproxy manually and retry."
    )


def allocate_port(host: str = "127.0.0.1", fallback: int = 0) -> int:
    """Allocate a random available port from the OS.

    Binds to port 0, reads the assigned port, closes the socket.
    Falls back to *fallback* if allocation fails.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind((host, 0))
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            port = sock.getsockname()[1]
            return port
    except OSError:
        if fallback:
            return fallback
        raise


def resolve_proxy_host(listen_host: str) -> str:
    """Map 0.0.0.0 to 127.0.0.1 for proxy URL construction."""
    if listen_host == "0.0.0.0":
        return "127.0.0.1"
    return listen_host


def stop_process(proc: subprocess.Popen[Any]) -> None:
    """Terminate a subprocess, escalating to kill after 3s timeout."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def build_mitmdump_cmd(
    mitmdump_bin: str,
    host: str,
    port: int,
    addon_paths: list[str],
    target_path: str,
    wrapper_log_file: str,
    instructions_file: str = "",
    instructions_text: str = "",
    tool_instructions_text: str = "",
    canary_rule: str = "",
    passthrough: bool = False,
    debug_requests: bool = False,
    rewrite_test_mode: str = "",
    developer_instructions_mode: str = "",
    rewrite_test_tag: str = "",
    codex_developer_prompt_file: str = "",
    traffic_caller: str = "",
    traffic_max_age_days: int = 0,
    traffic_redact: bool = True,
    prompt_recv_prefix_file: str = "",
    prompt_context_cwd: str = "",
) -> list[str]:
    """Build the mitmdump command line with addon scripts and options."""
    cmd = [
        mitmdump_bin,
        "--quiet",
        "--listen-host",
        host,
        "-p",
        str(port),
    ]

    for addon_path in addon_paths:
        cmd.extend(["-s", addon_path])

    cmd.extend(["--set", f"target_path={target_path}"])
    cmd.extend(["--set", f"wrapper_log_file={wrapper_log_file}"])

    if instructions_file:
        cmd.extend(["--set", f"system_instructions_file={instructions_file}"])
    if instructions_text:
        cmd.extend(["--set", f"system_instructions_text={instructions_text}"])
    if tool_instructions_text:
        cmd.extend(["--set", f"tool_instructions_text={tool_instructions_text}"])
    if canary_rule:
        cmd.extend(["--set", f"canary_rule={canary_rule}"])
    if passthrough:
        cmd.extend(["--set", "passthrough=true"])
    if debug_requests:
        cmd.extend(["--set", "debug_requests=true"])
    if rewrite_test_mode:
        cmd.extend(["--set", f"rewrite_test_mode={rewrite_test_mode}"])
    if developer_instructions_mode:
        cmd.extend(["--set", f"developer_instructions_mode={developer_instructions_mode}"])
    if rewrite_test_tag:
        cmd.extend(["--set", f"rewrite_test_tag={rewrite_test_tag}"])
    if codex_developer_prompt_file:
        cmd.extend(["--set", f"codex_developer_prompt_file={codex_developer_prompt_file}"])
    if traffic_caller:
        cmd.extend(["--set", f"traffic_caller={traffic_caller}"])
    if traffic_max_age_days > 0:
        cmd.extend(["--set", f"traffic_max_age_days={traffic_max_age_days}"])
    if not traffic_redact:
        cmd.extend(["--set", "traffic_redact=false"])
    if prompt_recv_prefix_file:
        cmd.extend(["--set", f"prompt_recv_prefix_file={prompt_recv_prefix_file}"])
    if prompt_context_cwd:
        cmd.extend(["--set", f"prompt_context_cwd={prompt_context_cwd}"])

    return cmd


def start_proxy(
    cmd: list[str],
    log_path: Path,
    mitm_log_path: Path,
) -> subprocess.Popen[Any]:
    """Start mitmdump as a background process.

    Returns the Popen object. Raises RuntimeError if proxy exits immediately.
    """
    append_log(log_path, f"Starting mitmdump: {fmt_cmd(cmd)}")
    append_log(log_path, f"mitmdump runtime log: {mitm_log_path}")

    log_handle = mitm_log_path.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=log_handle,
        stderr=log_handle,
        start_new_session=True,
    )
    log_handle.close()

    time.sleep(0.25)
    if proc.poll() is not None:
        append_log(log_path, "mitmdump exited early.")
        tail = tail_file(mitm_log_path, lines=80)
        if tail:
            append_log(log_path, "--- mitmdump startup log (tail) ---")
            append_log(log_path, tail)
            append_log(log_path, "--- end log tail ---")
        raise RuntimeError(
            f"mitmdump exited with code {proc.returncode or 1}"
        )

    return proc


def verify_proxy_flow(
    proxy_host: str,
    proxy_port: int,
    log_path: Path,
    startup_timeout_seconds: float = 4.0,
    retry_interval_seconds: float = 0.15,
) -> bool:
    """Verify request forwarding by routing a local HTTP request through the proxy.

    Mitmdump startup can be slightly delayed while loading addons, so probe the
    proxy for a short grace window instead of failing on the first refused
    connection.
    """

    class _HealthHandler(http.server.BaseHTTPRequestHandler):
        token = ""

        def do_GET(self) -> None:  # noqa: N802 (http.server naming)
            payload = self.token.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: object) -> None:
            return

    token = f"ai-cli-proxy-health-{time.time_ns()}"
    _HealthHandler.token = token
    health_server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
    thread = threading.Thread(target=health_server.serve_forever, daemon=True)
    thread.start()

    target_url = f"http://127.0.0.1:{health_server.server_port}/health"
    deadline = time.monotonic() + max(startup_timeout_seconds, 0.0)
    attempts = 0
    last_error = ""
    try:
        while True:
            attempts += 1
            conn: http.client.HTTPConnection | None = None
            try:
                conn = http.client.HTTPConnection(proxy_host, proxy_port, timeout=2)
                conn.request("GET", target_url)
                response = conn.getresponse()
                body = response.read().decode("utf-8", errors="ignore")
                if response.status == 200 and token in body:
                    append_log(
                        log_path,
                        "Proxy health check passed (request successfully forwarded through mitmdump). "
                        f"attempt={attempts}",
                    )
                    return True
                last_error = f"status={response.status}, body_len={len(body)}"
            except OSError as exc:
                last_error = str(exc)
            finally:
                if conn is not None:
                    conn.close()

            now = time.monotonic()
            if now >= deadline:
                append_log(
                    log_path,
                    "Proxy health check failed "
                    f"after {attempts} attempt(s): {last_error}",
                )
                return False

            time.sleep(max(retry_interval_seconds, 0.01))
    finally:
        health_server.shutdown()
        health_server.server_close()
        thread.join(timeout=1)


def build_proxy_env(
    proxy_url: str,
    ca_path: Path,
    log_path: Path,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Build environment dict with proxy and SSL cert variables."""
    env = os.environ.copy()

    # Proxy variables (both cases for compatibility)
    env["HTTP_PROXY"] = proxy_url
    env["HTTPS_PROXY"] = proxy_url
    env["ALL_PROXY"] = proxy_url
    env["http_proxy"] = proxy_url
    env["https_proxy"] = proxy_url
    env.setdefault("NO_PROXY", "localhost,127.0.0.1")
    env.setdefault("no_proxy", "localhost,127.0.0.1")

    # SSL certificate trust
    ca_path_str = str(ca_path)
    if ca_path.is_file():
        env["SSL_CERT_FILE"] = ca_path_str
        env["REQUESTS_CA_BUNDLE"] = ca_path_str
        env["NODE_EXTRA_CA_CERTS"] = ca_path_str
    else:
        append_log(
            log_path,
            f"Warning: CA cert not found at {ca_path_str}. "
            "TLS requests through mitmproxy may fail.",
        )

    if extra_env:
        env.update(extra_env)

    apply_pinned_mitmdump_path(env)
    return env

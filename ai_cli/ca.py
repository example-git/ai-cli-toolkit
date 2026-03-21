"""CA certificate bootstrap and optional trust-store installation.

Handles generating mitmproxy CA certificates on first run, and optionally
installing them into the macOS or Linux system trust store.
"""

from __future__ import annotations

import random
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from ai_cli.log import append_log, fmt_cmd

DEFAULT_CA_PATH = "~/.mitmproxy/mitmproxy-ca-cert.pem"


def _stop_process(proc: subprocess.Popen[Any]) -> None:
    """Terminate a subprocess, escalating to kill after timeout."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()


def bootstrap_ca_cert(
    ca_path: Path,
    mitmdump_bin: str,
    log_path: Path,
) -> bool:
    """Ensure mitmproxy CA cert exists at *ca_path*.

    If missing, runs a short-lived mitmdump process to generate CA material.
    Returns True if cert is available after bootstrap.
    """
    if ca_path.is_file():
        return True

    confdir = ca_path.parent
    generated_path = confdir / "mitmproxy-ca-cert.pem"
    try:
        confdir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        append_log(log_path, f"Failed to create CA directory {confdir}: {exc}")
        return False

    append_log(log_path, f"CA cert missing at {ca_path}. Bootstrapping with mitmdump.")

    for _ in range(3):
        port = random.randint(39000, 49000)
        bootstrap_cmd = [
            mitmdump_bin,
            "--quiet",
            "--set",
            f"confdir={confdir}",
            "--listen-host",
            "127.0.0.1",
            "-p",
            str(port),
        ]
        append_log(log_path, f"CA bootstrap command: {fmt_cmd(bootstrap_cmd)}")

        try:
            bootstrap_proc = subprocess.Popen(
                bootstrap_cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            append_log(log_path, f"Failed to start bootstrap mitmdump: {exc}")
            continue

        time.sleep(0.6)
        _stop_process(bootstrap_proc)
        if generated_path.is_file() or ca_path.is_file():
            break

    if generated_path.is_file() and generated_path != ca_path:
        try:
            shutil.copy2(generated_path, ca_path)
        except OSError as exc:
            append_log(log_path, f"Failed to copy generated CA cert to {ca_path}: {exc}")

    if ca_path.is_file():
        append_log(log_path, f"CA cert available at {ca_path}.")
        return True

    append_log(log_path, f"CA bootstrap failed. Expected cert at {ca_path}.")
    return False


def install_ca_macos(ca_path: Path, log_path: Path) -> bool:
    """Install CA cert into the macOS system keychain (requires sudo)."""
    if not ca_path.is_file():
        append_log(log_path, f"CA cert not found at {ca_path}")
        return False

    cmd = [
        "sudo",
        "security",
        "add-trusted-cert",
        "-d",
        "-r",
        "trustRoot",
        "-k",
        "/Library/Keychains/System.keychain",
        str(ca_path),
    ]
    append_log(log_path, f"Installing CA to macOS keychain: {fmt_cmd(cmd)}")
    try:
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode == 0:
            append_log(log_path, "CA cert installed to macOS system keychain.")
            return True
        append_log(
            log_path,
            f"CA install failed (exit={result.returncode}): {result.stderr.strip()}",
        )
    except OSError as exc:
        append_log(log_path, f"CA install failed: {exc}")
    return False


def install_ca_linux(ca_path: Path, log_path: Path) -> bool:
    """Install CA cert into the Linux system trust store."""
    if not ca_path.is_file():
        append_log(log_path, f"CA cert not found at {ca_path}")
        return False

    # Try Debian/Ubuntu style
    dest_dir = Path("/usr/local/share/ca-certificates")
    update_cmd = "update-ca-certificates"
    if not dest_dir.exists():
        # Try RHEL/Fedora style
        dest_dir = Path("/etc/pki/ca-trust/source/anchors")
        update_cmd = "update-ca-trust"

    if not dest_dir.exists():
        append_log(log_path, "No known CA trust directory found on this system.")
        return False

    dest = dest_dir / "mitmproxy-ca-cert.crt"
    try:
        result = subprocess.run(
            ["sudo", "cp", str(ca_path), str(dest)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            append_log(log_path, f"Failed to copy CA cert: {result.stderr.strip()}")
            return False
        result = subprocess.run(
            ["sudo", update_cmd],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            append_log(log_path, f"CA cert installed via {update_cmd}.")
            return True
        append_log(log_path, f"{update_cmd} failed: {result.stderr.strip()}")
    except OSError as exc:
        append_log(log_path, f"CA install failed: {exc}")
    return False

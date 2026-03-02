from __future__ import annotations

import os
import sys

from ai_cli import proxy


def test_pin_mitmdump_binary_creates_pinned_wrapper(monkeypatch, tmp_path) -> None:
    pinned_dir = tmp_path / "bin"
    pinned_bin = pinned_dir / "mitmdump"
    monkeypatch.setattr(proxy, "PINNED_MITM_DIR", pinned_dir)
    monkeypatch.setattr(proxy, "PINNED_MITMDUMP", pinned_bin)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    selected = proxy._pin_mitmdump_binary("/tmp/stable/mitmdump")

    assert selected == str(pinned_bin)
    assert pinned_bin.is_file()
    assert os.access(pinned_bin, os.X_OK)
    assert os.environ["PATH"].split(os.pathsep)[0] == str(pinned_dir)
    assert os.environ["MITM_BIN"] == str(pinned_bin)
    assert os.environ[proxy.PINNED_MITM_ENV] == str(pinned_bin)


def test_pin_mitmdump_binary_does_not_write_self_recursive_wrapper(
    monkeypatch, tmp_path
) -> None:
    pinned_dir = tmp_path / "bin"
    pinned_bin = pinned_dir / "mitmdump"
    monkeypatch.setattr(proxy, "PINNED_MITM_DIR", pinned_dir)
    monkeypatch.setattr(proxy, "PINNED_MITMDUMP", pinned_bin)

    pinned_dir.mkdir(parents=True, exist_ok=True)
    pinned_bin.write_text(
        "#!/usr/bin/env bash\nexec /opt/stable/mitmdump \"$@\"\n",
        encoding="utf-8",
    )
    pinned_bin.chmod(0o755)

    selected = proxy._pin_mitmdump_binary(str(pinned_bin))

    assert selected == str(pinned_bin)
    content = pinned_bin.read_text(encoding="utf-8")
    assert "exec /opt/stable/mitmdump " in content
    assert f"exec {pinned_bin} " not in content


def test_apply_pinned_mitmdump_path_updates_child_env(monkeypatch) -> None:
    monkeypatch.setenv(proxy.PINNED_MITM_ENV, "/opt/stable-mitm/mitmdump")
    env = {"PATH": "/usr/local/bin:/usr/bin"}

    updated = proxy.apply_pinned_mitmdump_path(env)

    assert updated["PATH"].split(os.pathsep)[0] == "/opt/stable-mitm"
    assert updated["MITM_BIN"].endswith("/opt/stable-mitm/mitmdump")
    assert updated[proxy.PINNED_MITM_ENV].endswith("/opt/stable-mitm/mitmdump")


def test_is_user_site_hidden_error_matches_expected_message() -> None:
    output = (
        "ERROR: Can not perform a '--user' install. "
        "User site-packages are not visible in this virtualenv."
    )
    assert proxy._is_user_site_hidden_error(output) is True
    assert proxy._is_user_site_hidden_error("some unrelated pip error") is False


def test_ensure_mitmdump_retries_without_user_install(monkeypatch, tmp_path) -> None:
    log_path = tmp_path / "install.log"
    calls: list[list[str]] = []
    resolve_calls = {"count": 0}

    def fake_resolve() -> str:
        resolve_calls["count"] += 1
        if resolve_calls["count"] == 1:
            raise FileNotFoundError("mitmdump missing")
        return "/opt/mitm/bin/mitmdump"

    def fake_run_install(cmd: list[str]) -> tuple[bool, str]:
        calls.append(cmd)
        if "--user" in cmd:
            return (
                False,
                "exit=1\nERROR: Can not perform a '--user' install. "
                "User site-packages are not visible in this virtualenv.",
            )
        return True, "ok"

    monkeypatch.setattr(proxy, "resolve_mitmdump", fake_resolve)
    monkeypatch.setattr(proxy, "_run_install_command", fake_run_install)
    monkeypatch.setattr(proxy, "_pin_mitmdump_binary", lambda binary, log_path=None: binary)
    monkeypatch.setattr(proxy.shutil, "which", lambda _: None)
    monkeypatch.setattr(proxy.sys, "platform", "linux")

    selected = proxy.ensure_mitmdump(log_path)

    assert selected == "/opt/mitm/bin/mitmdump"
    assert calls == [
        [sys.executable, "-m", "pip", "install", "--user", "mitmproxy"],
        [sys.executable, "-m", "pip", "install", "mitmproxy"],
    ]


def test_start_proxy_raises_when_process_exits_early(monkeypatch, tmp_path) -> None:
    class FakeProc:
        returncode = 42

        def poll(self) -> int:
            return self.returncode

    monkeypatch.setattr(proxy.time, "sleep", lambda _: None)
    monkeypatch.setattr(proxy.subprocess, "Popen", lambda *args, **kwargs: FakeProc())
    monkeypatch.setattr(proxy, "tail_file", lambda *_args, **_kwargs: "startup failed")

    logs: list[str] = []
    monkeypatch.setattr(proxy, "append_log", lambda _path, msg: logs.append(msg))

    log_path = tmp_path / "wrapper.log"
    mitm_log_path = tmp_path / "mitm.log"

    try:
        proxy.start_proxy(["mitmdump"], log_path, mitm_log_path)
    except RuntimeError as exc:
        assert "mitmdump exited with code 42" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError for early mitmdump exit.")

    assert any("mitmdump exited early." in line for line in logs)


def test_resolve_mitmdump_recovers_from_stale_pinned_override(
    monkeypatch, tmp_path
) -> None:
    pinned_dir = tmp_path / "bin"
    pinned_bin = pinned_dir / "mitmdump"
    good_bin = "/opt/homebrew/bin/mitmdump"
    monkeypatch.setattr(proxy, "PINNED_MITM_DIR", pinned_dir)
    monkeypatch.setattr(proxy, "PINNED_MITMDUMP", pinned_bin)
    monkeypatch.setenv(proxy.PINNED_MITM_ENV, str(pinned_bin))
    monkeypatch.setenv("MITM_BIN", str(pinned_bin))
    monkeypatch.setattr(proxy.shutil, "which", lambda value: value)
    monkeypatch.setattr(
        proxy,
        "_iter_path_executables",
        lambda _name: [str(pinned_bin), good_bin],
    )
    monkeypatch.setattr(
        proxy,
        "_probe_mitmdump",
        lambda binary: (False, "bad") if binary == str(pinned_bin) else (True, "ok"),
    )

    selected = proxy.resolve_mitmdump()

    assert selected == good_bin
    assert os.environ.get("MITM_BIN") is None
    assert os.environ.get(proxy.PINNED_MITM_ENV) is None


def test_verify_proxy_flow_retries_then_passes(monkeypatch, tmp_path) -> None:
    class FakeResponse:
        status = 200

        def read(self) -> bytes:
            return b"ai-cli-proxy-health-123"

    class FakeHTTPConnection:
        calls = 0

        def __init__(self, *_args, **_kwargs) -> None:
            return

        def request(self, *_args, **_kwargs) -> None:
            type(self).calls += 1
            if type(self).calls == 1:
                raise ConnectionRefusedError(61, "Connection refused")

        def getresponse(self) -> FakeResponse:
            return FakeResponse()

        def close(self) -> None:
            return

    logs: list[str] = []
    monkeypatch.setattr(proxy, "append_log", lambda _path, msg: logs.append(msg))
    monkeypatch.setattr(proxy.time, "time_ns", lambda: 123)
    monkeypatch.setattr(proxy.http.client, "HTTPConnection", FakeHTTPConnection)
    monkeypatch.setattr(proxy.time, "sleep", lambda _seconds: None)

    ok = proxy.verify_proxy_flow(
        "127.0.0.1",
        12345,
        tmp_path / "wrapper.log",
        startup_timeout_seconds=1.0,
        retry_interval_seconds=0.01,
    )

    assert ok is True
    assert FakeHTTPConnection.calls == 2
    assert any("attempt=2" in line for line in logs)

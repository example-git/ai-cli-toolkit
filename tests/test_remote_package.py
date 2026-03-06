from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from ai_cli.remote import RemoteSpec
from ai_cli.remote_package import (
    PackageFileEntry,
    build_package_manifest,
    ensure_remote_ai_mux_asset,
    local_ai_mux_asset_path,
    push_package,
    reattach_remote_session,
    render_remote_ai_mux_config,
)


def test_build_package_manifest_resolves_absolute_remote_home(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "ai_cli.remote_package.resolve_remote_home",
        lambda _spec: "/home/alice",
    )

    package = build_package_manifest(
        "claude",
        RemoteSpec(user="alice", host="server", path="/repo"),
    )

    assert package.real_home == "/home/alice"
    assert package.session_dir.startswith("/home/alice/.ai-cli/remote-sessions/")
    assert "/~/" not in package.session_dir
    assert "~" not in package.session_dir


def test_push_package_uses_portable_rsync_chmod(
    monkeypatch, tmp_path: Path
) -> None:
    local_file = tmp_path / "config.toml"
    local_file.write_text("x = 1\n", encoding="utf-8")

    package = SimpleNamespace(
        entries=[PackageFileEntry(local_path=local_file, remote_rel_path=".codex/config.toml")],
        session_dir="/home/alice/.ai-cli/remote-sessions/codex-abc123",
    )
    remote_spec = RemoteSpec(user="alice", host="server", path="/repo")

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("ai_cli.remote_package.shutil.which", lambda _name: "/usr/bin/rsync")
    monkeypatch.setattr("ai_cli.remote_package.subprocess.run", _run)

    push_package(package, remote_spec)

    assert len(calls) == 3
    assert calls[0][-1] == "mkdir -p /home/alice/.ai-cli/remote-sessions/codex-abc123"
    assert "--chmod=Du=rwx,Dgo=,Fu=rw,Fgo=" in calls[1]
    assert "chmod 700 /home/alice/.ai-cli/remote-sessions/codex-abc123/.ai-cli/bin/ai-prompt-editor" in calls[2][-1]


def test_build_package_manifest_includes_codex_startup_files(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "ai_cli.remote_package.resolve_remote_home",
        lambda _spec: "/home/alice",
    )

    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".codex" / "config.toml").write_text("model='gpt'\n", encoding="utf-8")
    (tmp_path / ".codex" / ".codex-global-state.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".codex" / "policy").mkdir()
    (tmp_path / ".codex" / "policy" / "default.codexpolicy").write_text("allow = true\n", encoding="utf-8")

    package = build_package_manifest(
        "codex",
        RemoteSpec(user="alice", host="server", path="/repo"),
    )
    rels = {entry.remote_rel_path for entry in package.entries}

    assert ".codex/auth.json" in rels
    assert ".codex/config.toml" in rels
    assert ".codex/.codex-global-state.json" in rels
    assert ".codex/policy/default.codexpolicy" in rels


def test_build_package_manifest_includes_claude_gemini_and_copilot_files(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "ai_cli.remote_package.resolve_remote_home",
        lambda _spec: "/home/alice",
    )

    (tmp_path / ".claude" / "plugins").mkdir(parents=True)
    (tmp_path / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".claude" / "plugins" / "config.json").write_text("{}", encoding="utf-8")

    (tmp_path / ".gemini" / "antigravity").mkdir(parents=True)
    (tmp_path / ".gemini" / "oauth_creds.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".gemini" / "antigravity" / "mcp_config.json").write_text("{}", encoding="utf-8")

    (tmp_path / ".copilot" / "agents").mkdir(parents=True)
    (tmp_path / ".copilot" / "config.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".copilot" / "agents" / "dev-instructions.agent.md").write_text("x", encoding="utf-8")
    (tmp_path / ".config" / "github-copilot").mkdir(parents=True)
    (tmp_path / ".config" / "github-copilot" / "apps.json").write_text("{}", encoding="utf-8")

    spec = RemoteSpec(user="alice", host="server", path="/repo")

    claude_rels = {
        entry.remote_rel_path
        for entry in build_package_manifest("claude", spec).entries
    }
    gemini_rels = {
        entry.remote_rel_path
        for entry in build_package_manifest("gemini", spec).entries
    }
    copilot_rels = {
        entry.remote_rel_path
        for entry in build_package_manifest("copilot", spec).entries
    }

    assert ".claude/settings.json" in claude_rels
    assert ".claude/plugins/config.json" in claude_rels
    assert ".gemini/oauth_creds.json" in gemini_rels
    assert ".gemini/antigravity/mcp_config.json" in gemini_rels
    assert ".copilot/config.json" in copilot_rels
    assert ".copilot/agents/dev-instructions.agent.md" in copilot_rels
    assert ".config/github-copilot/apps.json" in copilot_rels


def test_build_package_manifest_generates_shell_bootstrap_and_tmux_config(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "ai_cli.remote_package.resolve_remote_home",
        lambda _spec: "/home/alice",
    )

    tmux_dir = tmp_path / ".config" / "ai-cli"
    tmux_dir.mkdir(parents=True)
    (tmux_dir / "tmux.conf").write_text("set -g mouse on\n", encoding="utf-8")

    package = build_package_manifest(
        "codex",
        RemoteSpec(user="alice", host="server", path="/repo"),
    )
    contents = {
        entry.remote_rel_path: entry.content
        for entry in package.entries
        if entry.content is not None
    }
    rels = {entry.remote_rel_path for entry in package.entries}

    assert ".profile" in rels
    assert ".bashrc" in rels
    assert ".bash_env" in rels
    assert ".zshenv" in rels
    assert ".zshrc" in rels
    assert ".shrc" in rels
    assert ".kshrc" in rels
    assert ".tmux.conf" in rels
    assert ".config/ai-cli/tmux.conf" in rels
    assert ".ai-cli/bin/ai-prompt-editor" in rels
    project_prompt_rels = [rel for rel in rels if rel.startswith(".ai-cli/project-prompts/")]
    assert any(rel.endswith("/instructions.txt") for rel in project_prompt_rels)
    assert any(rel.endswith("/meta.json") for rel in project_prompt_rels)
    assert "ai_rules" in contents[".ai-cli/shell-common.sh"]
    assert "shell-commands.log" in contents[".ai-cli/shell-common.sh"]
    assert 'AI_CLI_BASE_PROMPT_FILE="$HOME/.ai-cli/base_instructions.txt"' in contents[".ai-cli/shell-common.sh"]
    assert 'export AI_CLI_PROJECT_PROMPT_FILE="$HOME/.ai-cli/project-prompts/' in contents[".ai-cli/shell-common.sh"]
    assert 'AI_CLI_PROMPT_EDITOR_LAUNCHER="$HOME/.ai-cli/bin/ai-prompt-editor"' in contents[".ai-cli/shell-common.sh"]
    assert "trap '_ai_cli_bash_debug_hook' DEBUG" in contents[".bash_env"]
    assert "add-zsh-hook preexec _ai_cli_zsh_preexec" in contents[".zshrc"]
    assert "set -g mouse on" in contents[".tmux.conf"]
    assert "set -g xterm-keys on" in contents[".tmux.conf"]
    # extended-keys/extkeys deliberately removed (broke key passthrough on remote)
    assert "extended-keys" not in contents[".tmux.conf"]
    assert "extkeys" not in contents[".tmux.conf"]
    assert "bind -n F5 if-shell -F" in contents[".tmux.conf"]
    assert "edit-global" in contents[".tmux.conf"]
    assert "ai-prompt-editor" in contents[".tmux.conf"]
    assert "run-shell -b" in contents[".tmux.conf"]
    assert "bind -n F6 if-shell -F" in contents[".tmux.conf"]
    assert "edit-base" in contents[".tmux.conf"]
    assert "bind -n F7 if-shell -F" in contents[".tmux.conf"]
    assert "edit-tool" in contents[".tmux.conf"]
    assert "bind -n F8 if-shell -F" in contents[".tmux.conf"]
    assert "edit-project" in contents[".tmux.conf"]
    assert "bind -n M-Left previous-window" in contents[".tmux.conf"]
    assert "bind -n F5 select-window -t :3" not in contents[".tmux.conf"]
    assert "source-file ~/.config/ai-cli/tmux.conf" in contents[".tmux.conf"]


def test_build_package_manifest_includes_ai_mux_binary(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(
        "ai_cli.remote_package.resolve_remote_home",
        lambda _spec: "/home/alice",
    )
    asset = tmp_path / "ai-mux-linux-x86_64"
    asset.write_text("binary", encoding="utf-8")

    package = build_package_manifest(
        "codex",
        RemoteSpec(user="alice", host="server", path="/repo"),
        ai_mux_binary=asset,
    )

    rels = {entry.remote_rel_path for entry in package.entries}
    assert ".ai-cli/bin/ai-mux" in rels


def test_render_remote_ai_mux_config() -> None:
    payload = json.loads(
        render_remote_ai_mux_config(
            tool_name="codex",
            session_name="codex-abc123",
            command=["/usr/bin/codex", "--version"],
            cwd="/repo",
            env={"HOME": "/home/alice/.ai-cli/remote-sessions/codex-abc123"},
        )
    )

    assert payload["session_name"] == "codex-abc123"
    assert payload["tabs"][0]["label"] == "codex"
    assert payload["tabs"][0]["cmd"] == ["/usr/bin/codex", "--version"]
    assert payload["tabs"][0]["cwd"] == "/repo"


def test_ensure_remote_ai_mux_asset_rebuilds_even_when_cached(monkeypatch, tmp_path: Path) -> None:
    asset = tmp_path / "ai-mux-linux-x86_64"
    asset.write_bytes(b"cached")
    asset.chmod(0o755)
    monkeypatch.setattr(
        "ai_cli.remote_package.resolve_remote_target",
        lambda _spec: ("linux", "x86_64"),
    )
    monkeypatch.setattr(
        "ai_cli.remote_package.local_ai_mux_asset_path",
        lambda _system, _machine: asset,
    )
    monkeypatch.setattr("ai_cli.remote_package.shutil.which", lambda _name: "/usr/bin/rsync")

    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        if Path(cmd[0]).name == "rsync" and str(asset) in cmd:
            asset.write_bytes(b"fresh")
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    monkeypatch.setattr("ai_cli.remote_package.subprocess.run", _run)

    resolved = ensure_remote_ai_mux_asset(RemoteSpec(user="alice", host="server", path="/repo"))

    assert resolved == asset
    assert asset.read_bytes() == b"fresh"
    assert any("cargo build --release" in cmd[-1] for cmd in calls if cmd and Path(cmd[0]).name == "ssh")


def test_reattach_remote_session_sources_packaged_tmux_conf(monkeypatch) -> None:
    calls: list[list[str]] = []

    def _run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr("ai_cli.remote_package.subprocess.run", _run)

    package = SimpleNamespace(
        tmux_socket="ai-cli-codex",
        session_name="codex-abc123",
        session_dir="/home/alice/.ai-cli/remote-sessions/codex-abc123",
    )
    spec = RemoteSpec(user="alice", host="server", path="/repo")

    rc = reattach_remote_session(package, spec)

    assert rc == 0
    remote_cmd = calls[0][-1]
    assert "tmux -L ai-cli-codex source-file /home/alice/.ai-cli/remote-sessions/codex-abc123/.tmux.conf" in remote_cmd
    assert "tmux -L ai-cli-codex attach -t codex-abc123" in remote_cmd

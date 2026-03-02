# Getting Started

## What This Is

`ai-cli` is a wrapper that runs `claude`, `codex`, `copilot`, or `gemini` through one consistent command and proxy/instruction layer.

## Install

```bash
bash install.sh
```

Or for local development:

```bash
python3 -m pip install --user -e .
```

## First Run

```bash
ai-cli status
ai-cli codex .
```

You can replace `codex` with `claude`, `copilot`, or `gemini`.

## Useful Next Commands

```bash
ai-cli history --list
ai-cli traffic --limit 50
ai-cli prompt-edit global
ai-cli prompt-edit tool codex
```

## Where Config Lives

- Main config: `~/.ai-cli/config.json`
- Global instructions: `~/.ai-cli/system_instructions.txt`
- Per-tool instructions: `~/.ai-cli/instructions/<tool>.txt`

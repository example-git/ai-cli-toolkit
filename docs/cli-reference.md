# CLI Reference

## Primary Usage

```bash
ai-cli <tool> [DIR] [args...]
```

`<tool>` is one of: `claude`, `codex`, `copilot`, `gemini`.

If `[DIR]` is an existing directory, the wrapped tool launches in that directory.

## Management Commands

```bash
ai-cli menu
ai-cli status
ai-cli system [tool]
ai-cli system prompt [model]
ai-cli prompt-edit <global|tool> [tool]
ai-cli cleanup [--list] [--all | --select 1,2,3] [-y]
ai-cli history [options]
ai-cli session [options]     # alias of history
ai-cli traffic [options]
ai-cli update [tool|--all|--list]
ai-cli completions generate [--shell bash|zsh|all]
```

## Prompt Editing

- Global prompt file:
  - `ai-cli prompt-edit global`
- Tool prompt file:
  - `ai-cli prompt-edit tool codex`
- Legacy equivalent:
  - `ai-cli system` / `ai-cli system <tool>`

## History Examples

```bash
ai-cli history --list
ai-cli history --all --tail 50
ai-cli history --agent codex --tail 30
ai-cli history --all --grep "keyword"
```

## Traffic Examples

```bash
ai-cli traffic --limit 100
ai-cli traffic --caller codex --provider openai
ai-cli traffic --search "responses" --api
ai-cli traffic --detail 42
```

## Completion Generation

```bash
ai-cli completions generate --shell zsh
ai-cli completions generate --shell bash
ai-cli completions generate --shell all
```

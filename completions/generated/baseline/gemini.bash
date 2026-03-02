{
  "session_id": "6f97a66a-8c26-4815-8cce-8f9cd828c57f",
  "response": "The `ai-cli` tool includes a manual bash completion script and a generator that uses `shtab` for more comprehensive completions.\n\n### Current Bash Completion\nThe following script is located at `completions/ai-cli.bash`. It provides completion for the main subcommands and several flags:\n\n```bash\n#!/usr/bin/env bash\n# Bash completion for ai-cli and alias entrypoints.\n\n_ai_cli_completion() {\n  local cur prev cmd\n  COMPREPLY=()\n  cur=\"${COMP_WORDS[COMP_CWORD]}\"\n  prev=\"${COMP_WORDS[COMP_CWORD-1]}\"\n  cmd=\"$(basename \"${COMP_WORDS[0]}\")\"\n\n  local tools=\"claude codex copilot gemini\"\n  local subcommands=\"claude codex copilot gemini menu status system edit-signal session update help\"\n  local session_flags=\"--agent --all --list --grep --tail --tools --raw\"\n  local update_flags=\"--all --dry-run --list\"\n\n  # Alias entrypoints forward all args directly to the wrapped tool.\n  case \"$cmd\" in\n    claude|codex|copilot|gemini)\n      return 0\n      ;;\n  esac\n\n  if [[ $COMP_CWORD -eq 1 ]]; then\n    COMPREPLY=( $(compgen -W \"$subcommands\" -- \"$cur\") )\n    return 0\n  fi\n\n  local subcommand=\"${COMP_WORDS[1]}\"\n  case \"$subcommand\" in\n    claude|codex|copilot|gemini)\n      return 0\n      ;;\n    system)\n      COMPREPLY=( $(compgen -W \"$tools\" -- \"$cur\") )\n      ;;\n    session)\n      case \"$prev\" in\n        --agent)\n          COMPREPLY=( $(compgen -W \"all $tools\" -- \"$cur\") )\n          return 0\n          ;;\n        --tail)\n          COMPREPLY=()\n          return 0\n          ;;\n      esac\n      if [[ \"$cur\" == --* ]]; then\n        COMPREPLY=( $(compgen -W \"$session_flags\" -- \"$cur\") )\n      else\n        COMPREPLY=( $(compgen -f -- \"$cur\") )\n      fi\n      ;;\n    update)\n      if [[ \"$cur\" == --* ]]; then\n        COMPREPLY=( $(compgen -W \"$update_flags\" -- \"$cur\") )\n      else\n        COMPREPLY=( $(compgen -W \"$tools\" -- \"$cur\") )\n      fi\n      ;;\n    menu|status|edit-signal|help)\n      COMPREPLY=()\n      ;;\n    *)\n      COMPREPLY=( $(compgen -W \"$subcommands\" -- \"$cur\") )\n      ;;\n  esac\n\n  return 0\n}\n\ncomplete -F _ai_cli_completion ai-cli\ncomplete -F _ai_cli_completion claude\ncomplete -F _ai_cli_completion codex\ncomplete -F _ai_cli_completion copilot\ncomplete -F _ai_cli_completion gemini\n```\n\n### Installation\nTo install this completion, you can run the included `install.sh` script:\n```bash\n./install.sh\n```\nOr manually source it in your `.bashrc`:\n```bash\necho 'source /Volumes/SanDisk/ai-cli/completions/ai-cli.bash' >> ~/.bashrc\n```\n\n### Generating Comprehensive Completions\nThe project also includes a generator in `ai_cli/completion_gen.py` that can produce a more detailed script (including the `completions` subcommand itself and flags for wrapped tools). To generate an updated script, ensure `shtab` is installed and run:\n```bash\nai-cli completions generate --shell bash\n```\nThis will update `completions/ai-cli.bash` with a script that uses dynamic dispatch for both native and wrapped tool completions.",
  "stats": {
    "models": {
      "gemini-2.5-flash-lite": {
        "api": {
          "totalRequests": 1,
          "totalErrors": 0,
          "totalLatencyMs": 2311
        },
        "tokens": {
          "input": 1628,
          "prompt": 1628,
          "candidates": 69,
          "total": 2064,
          "cached": 0,
          "thoughts": 367,
          "tool": 0
        }
      },
      "gemini-3-flash-preview": {
        "api": {
          "totalRequests": 30,
          "totalErrors": 3,
          "totalLatencyMs": 106247
        },
        "tokens": {
          "input": 97832,
          "prompt": 455819,
          "candidates": 3265,
          "total": 466869,
          "cached": 357987,
          "thoughts": 7785,
          "tool": 0
        }
      },
      "gemini-2.5-flash": {
        "api": {
          "totalRequests": 6,
          "totalErrors": 0,
          "totalLatencyMs": 57181
        },
        "tokens": {
          "input": 7830,
          "prompt": 7830,
          "candidates": 2953,
          "total": 17252,
          "cached": 0,
          "thoughts": 6469,
          "tool": 0
        }
      }
    },
    "tools": {
      "totalCalls": 26,
      "totalSuccess": 24,
      "totalFail": 2,
      "totalDurationMs": 79482,
      "totalDecisions": {
        "accept": 0,
        "reject": 0,
        "modify": 0,
        "auto_accept": 24
      },
      "byName": {
        "list_directory": {
          "count": 2,
          "success": 2,
          "fail": 0,
          "durationMs": 33,
          "decisions": {
            "accept": 0,
            "reject": 0,
            "modify": 0,
            "auto_accept": 2
          }
        },
        "read_file": {
          "count": 16,
          "success": 16,
          "fail": 0,
          "durationMs": 37,
          "decisions": {
            "accept": 0,
            "reject": 0,
            "modify": 0,
            "auto_accept": 16
          }
        },
        "run_shell_command": {
          "count": 1,
          "success": 0,
          "fail": 1,
          "durationMs": 0,
          "decisions": {
            "accept": 0,
            "reject": 0,
            "modify": 0,
            "auto_accept": 0
          }
        },
        "glob": {
          "count": 1,
          "success": 1,
          "fail": 0,
          "durationMs": 15,
          "decisions": {
            "accept": 0,
            "reject": 0,
            "modify": 0,
            "auto_accept": 1
          }
        },
        "search_file_content": {
          "count": 2,
          "success": 2,
          "fail": 0,
          "durationMs": 163,
          "decisions": {
            "accept": 0,
            "reject": 0,
            "modify": 0,
            "auto_accept": 2
          }
        },
        "delegate_to_agent": {
          "count": 3,
          "success": 3,
          "fail": 0,
          "durationMs": 79234,
          "decisions": {
            "accept": 0,
            "reject": 0,
            "modify": 0,
            "auto_accept": 3
          }
        },
        "write_file": {
          "count": 1,
          "success": 0,
          "fail": 1,
          "durationMs": 0,
          "decisions": {
            "accept": 0,
            "reject": 0,
            "modify": 0,
            "auto_accept": 0
          }
        }
      }
    },
    "files": {
      "totalLinesAdded": 0,
      "totalLinesRemoved": 0
    }
  }
}Session cleanup disabled: Either maxAge or maxCount must be specified
Loaded cached credentials.
Loading extension: botwrangler-hikari
Loading extension: code-review
Loading extension: criticalthink
Loading extension: gemini-cli-jules
Loading extension: github
Loading extension: gstreamer-master
Error executing tool run_shell_command: Tool "run_shell_command" not found in registry. Tools must use the exact names that are registered. Did you mean one of: "search_file_content", "read_file", "save_memory"?
[LocalAgentExecutor] Blocked call: Unauthorized tool call: 'get_internal_docs' is not available to this agent.
[LocalAgentExecutor] Blocked call: Unauthorized tool call: 'get_internal_docs' is not available to this agent.
Error executing tool write_file: Tool "write_file" not found in registry. Tools must use the exact names that are registered. Did you mean one of: "read_file", "activate_skill", "glob"?
Attempt 1 failed: You have exhausted your capacity on this model. Your quota will reset after 0s.. Retrying after 648.543955ms...
Attempt 1 failed: You have exhausted your capacity on this model. Your quota will reset after 0s.. Retrying after 400.505882ms...
Attempt 1 failed: You have exhausted your capacity on this model. Your quota will reset after 0s.. Retrying after 483.56147ms...

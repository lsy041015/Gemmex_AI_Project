# Gemmex

Gemmex is a terminal-based autonomous coding agent built around Google AI Studio models. It references the workflow of Claude Code and GPT Codex while aiming to provide a free, lightweight tool for developers who want an interactive coding agent without a paid subscription.

The project focuses on practical coding workflows: reading and editing files, running commands, inspecting Git changes, executing generated code in a sandbox, and improving answers through an automated retry loop.

## Goals

- Provide a TUI coding agent that runs directly in the terminal.
- Use Google AI Studio API keys with intelligent rotation to extend available usage.
- Support coding workflows similar to Claude Code and GPT Codex.
- Build toward at least 80% of the benchmark performance of Claude Code and GPT Codex on comparable coding tasks.
- Add skill-based improvement loops for repeated coding and evaluation workflows.
- Keep the tool accessible for users who want a free local-first coding assistant.

## Current Features

- Textual-based terminal UI
- Multi-line prompt input
- File system tools:
  - `read_file`
  - `write_file`
  - `edit_file`
  - `list_dir`
  - `find_files`
  - `search_in_files`
- Shell execution tool with basic dangerous-command blocking
- Git tools:
  - `git_status`
  - `git_diff`
- API key rotation for Google AI Studio keys
- Rate-limit detection and temporary key blocking
- Session save and load
- Tool execution log panel
- Sandboxed code execution in `/tmp/gemma_sandbox`
- Automatic code-block execution
- Runtime error detection
- Auto-retry loop for failed generated code
- Missing Python package detection and install confirmation

## Project Status

Gemmex is currently in an active MVP stage.

Implemented core agent features are usable, but the project is not yet at full Claude Code / GPT Codex parity. The main remaining work is benchmark automation, stronger long-running reliability, 그리고 deeper integration of skill-based workflows.

Recent local benchmark results:

- Internal component performance test: passed, with most operations in FAST/OK range.
- Coding benchmark: 8/9 passed, 89% score on the included Python algorithm test set.

These results are promising but should not be treated as a final comparison against Claude Code or GPT Codex. A shared benchmark harness for all three tools is still needed.

## Installation

```bash
cd /home/wego/coder_ws
python3 -m pip install textual rich google-genai
```

## API Key Setup

Gemmex requires at least one Google AI Studio API key.

You can provide keys using environment variables:

```bash
export GEMMA_API_KEY="your-api-key"
```

Or multiple keys:

```bash
export GEMMA_API_KEYS="key-1,key-2,key-3"
```

The project also supports config files through `config/key_loader.py`, including:

- `config/settings.env`
- `config/api_keys/gemma_api_key.txt`
- `config/api_keys/gemma_api_keys.txt`

## Usage

Run the TUI agent:

```bash
cd /home/wego/coder_ws
python3 Gemmex.py
```

Example prompts:

```text
main.py 파일을 읽고 버그를 찾아줘
```

```text
/home/wego/project/app.py를 읽고 리팩토링 계획을 세워줘
```

```text
테스트가 실패하는 원인을 찾고 수정해줘
```

## Built-in Commands

Inside the TUI:

- `/help` - show command list
- `/new` - start a new chat
- `/save` - save the current session
- `/sessions` - list recent sessions
- `/load <file>` - load a saved session
- `/find <keyword>` - search current conversation
- `/model` - show available models
- `/model next` - switch to the next model
- `/tool <number>` - show a tool log entry
- `/tooldetail` - toggle detailed tool logs
- `/auto` - enable automatic code execution
- `/noauto` - disable automatic code execution
- `/cancel` - request cancellation
- `/quit` - exit
- `!<command>` - run a shell command directly

## Safety Model

Gemmex can read files, write files, edit files, run shell commands, 그리고 inspect Git state. To reduce accidental damage:

- Write/edit/run operations require confirmation.
- Dangerous command patterns such as destructive `rm` variants are blocked.
- Generated code is executed in a temporary sandbox directory.
- Existing files are backed up before edits.

This is not a complete security sandbox. Use Gemmex in repositories and environments where you are comfortable allowing a coding agent to operate.

## Benchmarking

Run the included local component benchmark:

```bash
python3 perf_test.py
```

Run the included coding benchmark:

```bash
python3 coding_test.py
```

The current benchmark suite is useful for regression testing, but the project still needs a standardized comparison harness for:

- Gemmex
- Claude Code
- GPT Codex

Planned benchmark dimensions:

- Algorithmic coding tasks
- Multi-file repository edits
- Debugging tasks
- Test repair tasks
- Tool-use correctness
- Long-running agent reliability
- Cost and API-limit behavior

## Roadmap

- Integrate skill loading and triggering directly into `Gemmex.py`
- Add a repeatable benchmark harness against Claude Code and GPT Codex
- Improve API error handling for 500/timeout/network failures
- Add persistent API key health tracking
- Add richer project indexing and context selection
- Improve final response stability after tool calls in the TUI
- Add structured benchmark reports
- Package the project for easier installation

## Philosophy

Gemmex is built for developers who want the workflow of modern coding agents without being locked into a paid subscription. The goal is not to clone any single product, but to combine a terminal-first interface, practical tool use, free API access patterns, 그리고 iterative skill improvement into a usable open coding assistant.

## 라이선스

License information has not been finalized yet.

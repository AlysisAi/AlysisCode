# Quickstart

This guide gets Alysis Code installed, configured, and running against a local workspace.

## Install

### Windows PowerShell

Paste into Windows PowerShell 5.1 or PowerShell 7:

```powershell
irm https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.ps1 | iex
```

The installer provisions uv and Python 3.12 if needed, installs the CLI in an isolated
environment, and makes `alysis` available in the current terminal. Other open terminals
may need a restart. It needs no administrator access or existing Python/pipx.
Read [install.ps1](../scripts/install.ps1) before running it if you prefer.

Native Windows needs a running Docker Desktop backend for sandboxed commands.
WSL can use Linux Bubblewrap or Docker integration. Keep their installations separate.

### Linux, macOS, and WSL

For WSL, first run `wsl --install` in PowerShell, restart if requested, then open
Ubuntu and create your Linux user. Run this command **inside Ubuntu**, not PowerShell:

```sh
curl -LsSf https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.sh | sh
```

This installer also provisions uv and Python. Follow its PATH instructions or open a
new terminal before running `alysis`. On WSL, keep projects under `~/projects`;
accessing projects under `/mnt/c` is slower.

Run either installer again to upgrade. Optional environment variables:

- `ALYSIS_VERSION=0.15.2` pins a release.
- `ALYSIS_PYTHON=3.12` selects the Python version.
- `ALYSIS_NO_MODIFY_PATH=1` skips persistent PATH changes. The PowerShell installer
  still updates the current process so you can use the CLI immediately.

Native Windows x64 releases include the signed native sandbox and select it by
default. Neither Docker nor the Desktop app is required. Install a release
containing this change with `uv tool install --python 3.12 --link-mode copy
alysis-code`, or use pipx or the standalone executable. The copy option avoids
shared package-cache hardlinks in the contained Python worker. See
[Windows CLI installation and limits](windows-cli-sandbox.md), then run
`alysis sandbox doctor --smoke` in the installed environment.

The native sandbox runs PowerShell commands. Explicit Full access uses the
Windows host runner with `cmd.exe` semantics. WSL uses Linux Bubblewrap or a
configured Docker integration; keep Windows and Linux environments separate.
Provider login and clipboard adapters use the environment where Alysis runs.

### Existing pipx users and manual installation

Python 3.11 or newer is required for manual installation. If pipx already manages
Alysis Code, use `pipx upgrade alysis-code` or `alysis update`. The installers
report conflicting executables instead of overwriting a different installation.

On native Windows with Python installed:

```powershell
py -m pip install --user pipx
py -m pipx ensurepath
py -m pipx install alysis-code
```

Use `python` instead of `py` if that is how your Python installation is launched.
On Ubuntu/Debian/WSL:

```sh
sudo apt update && sudo apt install -y pipx
pipx ensurepath
pipx install alysis-code
```

On macOS, install pipx with `brew install pipx`, then run `pipx ensurepath`
and `pipx install alysis-code`. Open a new terminal after `ensurepath`.
If your default Python is older than 3.11, select a newer interpreter:

```bash
pipx install --python python3.12 alysis-code
```

Virtual-environment installs also work. Activate the environment before running:

```bash
python -m pip install alysis-code
```

## Connect your Alysis account

```sh
alysis --version
alysis login
```

Approve the short code in your browser to link this machine to your account and credits.
Version 0.15.2 restores hosted login without any cloud environment variables.
If 0.15.1 reports that hosted services are not configured, upgrade with `alysis update`
(or `pipx upgrade alysis-code`), then retry.

You can instead configure your own provider in setup; an Alysis account is optional for that path.

## First Run

Start Alysis Code from the project you want to inspect or edit:

```bash
cd /path/to/project
alysis
```

On a fresh install, setup starts with **Connection Method** and asks:
**How would you like to connect Alysis Code to AI models?**

- **Use an API key** continues through provider, API key, default model,
  optional router model, workspace, and sandbox setup. Leave the router model
  unset to inherit the default, or choose a smaller/cheaper model for
  lightweight request routing.
- **Use an AI subscription** opens a second screen of supported provider
  connections, then asks for the workspace and skips API-key setup. The adapter
  owns provider sign-in; Alysis Code keeps its native agent loop, tools, and TUI.
  After sign-in, choose the model and reasoning effort in `/config`.

Setup does not ask for main-agent, task, or subagent step budgets. Execution is
autonomous by default and continues until completion; explicit step or
wall-clock limits are optional advanced safeguards.

The Alysis Code-hosted account option also remains under **Use an API key** and
uses its own sign-in instead of requiring a third-party provider key.

Re-run setup anytime:

```bash
alysis setup
```

After choosing **Use an AI subscription**, the built-in connection currently
supports ChatGPT subscription sign-in. Connect and select it directly with:

```bash
alysis auth login openai-codex
```

Complete sign-in in the browser. Alysis Code stores refreshable credentials in its encrypted
provider vault and keeps tokens out of `config.json`. Choose the account model
and its supported reasoning effort together in `/config → Default Model`. Inside
the TUI, `/login` presents both the Alysis Code account and AI subscription choices.

When the selected subscription is disconnected, `alysis` still opens the
native TUI. Its landing page shows one model-access prompt: use `/login` to choose
Alysis Code or an AI subscription, or `/config` for API-key setup. Account
management is also available under `/config → Model Access`. Model prompts stay blocked until
the connection succeeds. Alysis Code then returns to the TUI and opens Default
Model for the paired model/reasoning selection. One-shot `alysis run` calls
fail fast while the subscription is unavailable.

For manual configuration:

```bash
export ALYSIS_API_KEY="YOUR_KEY"
alysis config set base_url "https://api.openai.com/v1"
alysis config set model "gpt-4.1-mini"
```

To avoid storing a key, pass it for one command:

```bash
alysis run --api-key-stdin "Explain this repository."
```

To switch providers with a different environment variable:

```bash
export OTHER_API_KEY="YOUR_KEY"
alysis run --api-key-env OTHER_API_KEY --base-url "https://example.com/v1" --model "your-model" "Hello"
```

Those manual examples configure API-key model access. See
[AI subscription connections](account-runtimes.md) for subscription setup,
account status/logout commands, execution-mode mappings, and extension limits.

## Run And Chat

Use `run` for one-shot work:

```bash
alysis run --mode readonly "Explain this repository and identify the main entrypoints."
alysis run --mode review "Fix the failing test and show me the diff."
```

`alysis run` is a bounded one-shot flow. It includes guardrails for execution-style prompts, but
it is still best for focused tasks that can be completed from one instruction. For exploratory or
multi-step work, prefer `alysis chat` or Forge.

Use `chat` for an interactive session:

```bash
alysis chat
```

Useful chat commands:

- `/help`: show commands
- `/status`: show mode, workspace, and active model
- `/pwd`: show workspace root, focus directory, and active workdir
- `/permissions`: inspect or select execution permissions for the next message
- `/persona architect`: plan and design, with Markdown-only writes
- `/config`: open the inline configuration menu, including router model and limits
- `/forge`: start the plan-driven workflow for larger tasks

The same Alysis Code slash-command and tool surface remains available with a
subscription-backed profile. `/model` cannot change a subscription model by
itself; use `/config → Default Model` so model and reasoning effort remain a
compatible persisted pair.

## Workspace Binding

`alysis run` and `alysis chat` bind a workspace before the session starts. The requested path is
the current directory or `--path`. Inside a Git repository, Alysis Code binds to the repository root
and keeps the starting subdirectory as the focus directory.

Missing paths require `--create-path`. Broad paths such as `~` require an explicit override, and `/`
is blocked as a workspace root.

In chat, relative file/search/shell paths default to the active workdir. You can move within the
bound workspace with natural-language requests, `/cd`, or tool calls. Alysis Code does not rebind to a
different workspace mid-session.

## Sandbox Setup

Alysis Code can run shell and verification commands through Docker or Bubblewrap. For the simplest
first run on macOS or Windows, install Docker Desktop first, then run:

```bash
alysis sandbox setup
alysis sandbox doctor --smoke
alysis sandbox pull
```

See [Shell sandbox](shell_sandbox.md) for backend selection, production image pinning, and troubleshooting.

## Images And Tools

For multimodal-compatible models or providers:

```bash
alysis run --image ./screenshot.png "Describe this screenshot."
```

Inspect the built-in tool surface:

```bash
alysis tools
```

Web search is model-selected and model-independent once a backend is configured. For example,
DeepSeek can call the shared Tavily tool even though DeepSeek does not expose a provider-hosted
search API:

```bash
alysis config set web_search_policy auto
alysis config set web_search_mode external
alysis config set web_search_adapter tavily
export ALYSIS_WEB_SEARCH_API_KEY=<tavily-key>
alysis tools
```

`web_search_policy=auto` exposes the tool and lets the active model decide when current or
source-backed evidence is needed. `off` hides the tool. Use `/config` to change access and backend
mode interactively. `TAVILY_API_KEY` remains supported as a vendor-specific alias. See
[Web Search](web-search.md) for the provider-hosted coverage matrix and external fallback setup.

Inspect custom tools discovered for the current workspace:

```bash
alysis tool list --path .
```

## Updates

Alysis Code checks for newer releases in the background during interactive terminal launches,
at most once per configured interval, then shows cached notices in home/status surfaces.
Piped and managed invocations do not start automatic update checks. It never installs updates
silently. When a newer
release is known, interactive launches also show a one-time popup before the setup and workspace
screens offering to update now, remind you later (24-hour snooze), or skip that version; disable it
with `alysis config set update_prompt_enabled false` or `ALYSIS_UPDATE_PROMPT_ENABLED=0`. To
check PyPI for the latest package immediately:

```bash
alysis update check
```

To apply an available update, run:

```bash
alysis update
```

The command detects common `pipx`, `uv`, virtualenv, and pip installs, shows the exact upgrade
command, and asks before running it. Source or editable installs are left manual.

## Next Steps

- [Credentials](credentials.md): API key precedence and persisted credentials.
- [AI subscription connections](account-runtimes.md): use provider sign-in with Alysis Code's native agent and paired `/config` model/effort selection.
- [Permissions](../README.md#permissions--personas): readonly, review, auto, and fullaccess.
- [Forge](forge.md): plan, execute, verify, and review larger tasks.
- [MCP](mcp.md): connect external MCP servers.
- [Skills](skills.md): install and use reusable instruction bundles.

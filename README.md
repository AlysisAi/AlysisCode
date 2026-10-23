<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/banner-dark.svg">
    <img src="docs/assets/banner.svg" alt="Alysis Code — Everyone's Coding Agent" width="640">
  </picture>
</p>

<p align="center">
  An open-source coding agent for your terminal.<br>
  Bring your own model. Turn plans into reviewed, PR-ready code.
</p>

<p align="center">
  <a href="https://alysiscode.com/">Website</a> ·
  <a href="https://alysiscode.com/docs">Documentation</a> ·
  <a href="docs/quickstart.md">Quickstart</a> ·
  <a href="docs/CHANGELOG.md">Changelog</a>
</p>

<p align="center">
  <a href="https://github.com/AlysisAi/AlysisCode/actions/workflows/ci.yml"><img src="https://github.com/AlysisAi/AlysisCode/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://pypi.org/project/alysis-code/"><img src="https://img.shields.io/pypi/v/alysis-code.svg" alt="PyPI version"></a>
  <a href="https://pypi.org/project/alysis-code/"><img src="https://img.shields.io/pypi/pyversions/alysis-code.svg" alt="Python versions"></a>
  <a href="https://github.com/AlysisAi/AlysisCode/blob/main/LICENSE"><img src="https://img.shields.io/pypi/l/alysis-code.svg" alt="License"></a>
</p>

<p align="center">
  <a href="https://x.com/alysiscode">Follow on X</a> ·
  <a href="https://github.com/sponsors/AlysisAi">Sponsor the project</a>
</p>

---

### Alysis Code App

Alysis Code App is a desktop agent for coding and broader everyday work. It is a separate, closed-source product. This repository contains the Apache-2.0 CLI; the desktop application's source is not included here.

## Get Started

Alysis Code requires **Python 3.11+** and runs on macOS, Linux, and Windows.

**1 — Install**

On Windows, paste this into **PowerShell**:

```powershell
irm https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.ps1 | iex
```

On Linux, macOS, or inside a **WSL/Ubuntu terminal**:

```sh
curl -LsSf https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.sh | sh
```

The installers provision uv and Python 3.12 when needed. No existing Python or pipx is required.
PowerShell can use `alysis` immediately; after the shell installer, follow its PATH instructions
or open a new terminal. If you already use pipx, `pipx install alysis-code` also works;
run `pipx ensurepath` and restart your terminal if the command is missing.
Already installed? Run `alysis update` to get the latest release.

**2 — Choose how to connect to a model**

Pick one path. You can switch later with `/config` or by re-running `alysis-code setup`.

*Path A — Your own API key or a local model (no Alysis account needed)*

```bash
cd your-project
alysis-code
```

The first run opens a guided setup wizard. Choose **Use an API key**, connect a provider
(OpenAI, Anthropic, DeepSeek, Gemini, OpenRouter, and more), and pick your default model.
To run fully offline, point Alysis Code at a local endpoint (Ollama, LM Studio, vLLM) from the
`/config` picker, which lists every preset. Prefer to configure it by hand?

```bash
export ALYSIS_API_KEY="YOUR_KEY"
alysis config set base_url "https://api.openai.com/v1"
alysis config set model "gpt-4.1-mini"
```

Have a ChatGPT subscription instead? Choose **Use an AI subscription** in the wizard, or run
`alysis auth login openai-codex`.

*Path B — Alysis account*

```sh
alysis login
```

Approve the code in your browser to use your Alysis credits. No provider key and no cloud
environment variables are needed.

**3 — Start building**

```bash
cd your-project
alysis-code chat                            # interactive coding session
alysis-code run "Fix the failing tests"     # one-shot task
```

(`alysis` works too, as a short alias.) The `/config` picker lists every provider preset.
Full walkthrough: [Quickstart guide](docs/quickstart.md).

---

## Why Alysis Code

- **Easy to use** — a full coding agent behind a clean, convenient terminal interface that anyone can
  pick up and play with in minutes: slash commands, interactive pickers, and live status built in.
- **Built for every developer** — from solo scripts to team servers and CI, the same agent fits every
  workflow.
- **[Forge Mode](https://alysiscode.com/docs/concepts/tasks)** — plan-driven execution for larger work:
  plan, dispatch parallel workers, verify each task, ship PR-ready code.
- **[Orchestrated subagents](https://alysiscode.com/docs/concepts/agents)** — isolated workers run in
  parallel, verification is pinned to their exact candidates, and changes apply only after host-side
  conflict checks.
- **Free built-in web search** — model-independent `web_search` that works with any provider, at no
  extra cost.
- **[Bring your own model](https://alysiscode.com/docs/config/models)** — OpenAI, Anthropic, DeepSeek,
  Qwen, Gemini, Mistral, Xiaomi MiMo, OpenRouter, xAI, and more — plus local endpoints (Ollama,
  LM Studio, vLLM) for fully offline use.
- **[Deeply extensible](https://alysiscode.com/docs/concepts/plugins)** — skills, custom tools, MCP
  servers, hooks, subagents, and plugins, bundled in one declarative manifest.
- **[Sandboxed by default](https://alysiscode.com/docs/safety/sandbox-modes)** — Docker or Bubblewrap
  isolation with an always-on denylist that refuses `rm -rf /`, `curl | sh`, and `sudo` — even in
  `fullaccess`.

Shell and verification commands default to strict sandboxing. Verification can run outside that
sandbox only when explicitly configured with `verify_sandbox.mode="off"` or
`ALYSIS_VERIFY_SANDBOX_MODE=off`.

## How it compares

<details>
<summary>Compare models, integrations, and sandboxing</summary>

A quick, good-faith snapshot based on each project's public README and docs (October 2026).
"—" means we couldn't confirm it from their docs; corrections welcome via an issue.

| | Alysis Code | [Claude Code](https://github.com/anthropics/claude-code) | [Aider](https://github.com/Aider-AI/aider) | [Codex CLI](https://github.com/openai/codex) | [Gemini CLI](https://github.com/google-gemini/gemini-cli) |
| --- | --- | --- | --- | --- | --- |
| License | Apache-2.0 | Proprietary | Apache-2.0 | Apache-2.0 | Apache-2.0 |
| Models | Any supported provider | Claude | Almost any LLM | OpenAI (ChatGPT sign-in or API key), plus local models | Gemini |
| Local models | Yes (Ollama, LM Studio, vLLM) | — | Yes | Yes (Ollama, LM Studio) | — |
| MCP servers | Yes | Yes | — | Yes | Yes |
| Sandboxed shell | Yes, on by default (Docker / Bubblewrap) | Yes (sandboxed Bash tool) | — | Yes | Yes |
| Editor integration | VS Code (pre-release), JetBrains (preview) | Yes | Yes | Yes (VS Code, Cursor, Windsurf) | Yes (VS Code companion, ACP integrations) |

References: [Codex CLI local-provider options](https://learn.chatgpt.com/docs/developer-commands?surface=cli) ·
[Gemini CLI editor integrations](https://github.com/google-gemini/gemini-cli/blob/main/docs/ide-integration/index.md).

</details>

## Permissions & Personas

Two layers control what the agent may do — one enforces, one focuses.

**Permissions** are the enforcement layer. Choose one per command with `--mode`, select the next
message's level in chat with `/permissions` or **Shift+Tab**, or set a default with
`alysis-code config set default_mode <mode>`. A selection takes effect when the next user message
starts; it does not change a turn already running:

| Mode | Behavior |
| --- | --- |
| `readonly` | Inspection plus top-level read-only delegation — no file writes, shell, MCP, candidate apply/discard, or nested delegation. |
| `review` | The safe default — previews and asks before every write and shell command. |
| `auto` | Applies routine edits with fewer prompts; dangerous operations stay blocked. |
| `fullaccess` | No mode-level prompts for trusted workspaces — the denylist and audit log stay active. |

**Personas** are working postures layered on top. Switch with `/persona`, or cycle with **Tab** in
the TUI:

| Persona | Posture |
| --- | --- |
| `code` | Implementation — the default, full coding workflow. |
| `architect` | Planning and design — may write markdown documents only. |
| `ask` | Read-only questions and codebase exploration. |
| `debug` | Reproduce the failure first, then fix minimally and rerun the reproduction. |

A persona is a convention, never a permission: it can keep or lower your execution mode, but can
never raise it. Define your own in `.alysis_personas/*.md`.
[Learn more](https://alysiscode.com/docs/concepts/personas)

Use `/persona architect` for planning in chat, or `/forge` for a plan-driven execution workflow.
The former chat `/plan` commands have been removed; Forge retains its plan views and editor.
Persona changes made during a running turn apply after that turn finishes.

## Project Links

- [Website](https://alysiscode.com/)
- [Documentation](https://alysiscode.com/docs) — also mirrored in [docs/](docs/README.md)
- [Changelog](docs/CHANGELOG.md)
- [Contributing](.github/CONTRIBUTING.md) — local setup and PR expectations
- [Security policy](.github/SECURITY.md) — report vulnerabilities privately, never through public issues

## License

Apache-2.0. You're free to use, modify, and distribute this code, including commercially, as long as
you keep the attribution and license notices. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
The shared editor sidebar includes MIT-licensed adaptations from Kilo Code/OpenCode; retain its
[third-party notices](extensions/shared-ui/THIRD_PARTY_NOTICES.txt).

## FAQ

<details>
<summary><b>Where did Alysis Code come from?</b></summary>

Alysis Code is not a fork. The agent runtime, Forge orchestration, subagent system, terminal
interface, and sandbox layer were designed and built from zero by the Alysis AI team.
</details>

<details>
<summary><b>Is Alysis Code really open source?</b></summary>

Yes. The entire CLI is Apache-2.0 licensed. Bring your own API key from any supported provider, or
run fully offline against a local endpoint — nothing in the agent requires a paid account.
</details>

More questions? See the [full FAQ](https://alysiscode.com/docs/faq).

---

**Join the community** — [X](https://x.com/alysiscode) | [GitHub Issues](https://github.com/AlysisAi/AlysisCode/issues)


## Editor integrations

[VS Code](extensions/vscode-alysis/README.md) uses the shared Alysis sidebar and Python IDE bridge.
[JetBrains](extensions/jetbrains-alysis/README.md) is a development preview with a smaller feature set.
The VS Code Marketplace pre-release is pending production signing and acceptance; see the
[IDE release status](docs/vscode-marketplace-launch.md) before installing component packages.

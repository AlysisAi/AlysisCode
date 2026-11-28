<h1 align="center">Alysis AI</h1>

<p align="center"><strong>AI for code, work, and everyday tasks.</strong></p>

<p align="center">Build software with Alysis Code, create documents with Alysis Work, and delegate ongoing tasks to Alysis Owls — all in one desktop app.</p>

<p align="center">
  <a href="https://pypi.org/project/alysis-code/"><img alt="PyPI version" src="https://img.shields.io/pypi/v/alysis-code?style=flat-square"></a>
  <a href="https://pypi.org/project/alysis-code/"><img alt="CLI downloads per month" src="https://img.shields.io/pypi/dm/alysis-code?style=flat-square&amp;label=CLI%20downloads%2Fmonth"></a>
  <a href="https://github.com/AlysisAi/AlysisCode/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/AlysisAi/AlysisCode/ci.yml?style=flat-square&amp;label=ci"></a>
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/pypi/l/alysis-code?style=flat-square"></a>
  <a href="https://github.com/AlysisAi/AlysisCode"><img alt="GitHub stars" src="https://img.shields.io/github/stars/AlysisAi/AlysisCode?style=flat-square"></a>
  <a href="https://discord.gg/psEVWZgzFS"><img alt="Discord members" src="https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Fdiscord.com%2Fapi%2Fv10%2Finvites%2FpsEVWZgzFS%3Fwith_counts%3Dtrue&amp;query=%24.approximate_member_count&amp;label=discord&amp;suffix=%20members&amp;style=flat-square&amp;logo=discord&amp;logoColor=white&amp;color=5865F2"></a>
</p>

<p align="center">
  <strong><a href="https://alysiscode.com/#desktop-download">Download for macOS &amp; Windows</a></strong> ·
  <a href="https://alysiscode.com/docs">Documentation</a> ·
  <a href="https://discord.gg/psEVWZgzFS">Community</a>
</p>

<p align="center">macOS on Apple silicon · Windows x64 · No Python or CLI setup required</p>

<p align="center">
  <a href="docs/assets/alysis-app-0.1.62.png">
    <img src="docs/assets/alysis-app-0.1.62.png" alt="Alysis desktop app — a fresh Work workspace with Code and Owls navigation" width="1000">
  </a>
</p>

## Three ways to get things done

| Product | What you can do |
| --- | --- |
| **Alysis Code** | Build, debug, and review software. Work with your repositories, inspect changes, run commands in the built-in terminal, and roll back to a checkpoint. |
| **Alysis Work** | Turn notes, files, and research into Word documents, spreadsheets, presentations, and PDFs. Preview the result, select what needs changing, and ask for a revision. |
| **Alysis Owls** | Delegate ongoing tasks to personal assistants with persistent memory, routines, and a private local or cloud computer. Keep context as your work continues. |

Owls ask for approval before actions that reach other people, make payments, publish, or delete.

## Your tools, your choice

- **Choose your models.** Use an Alysis account, bring your own API keys, or connect local models.
- **Search the web.** Built-in web search works with any model, without a separate search API key.
- **Work across apps.** Built-in computer use lets Alysis operate the apps you allow.
- **Stay in control.** Choose permissions for each task, with shell commands sandboxed by default. [How permissions work](https://alysiscode.com/docs/safety/approval-prompts).
  Verification commands also default to strict sandboxing. Strict mode refuses execution when no backend is available. [Shell sandbox](docs/shell_sandbox.md)

## Get started

1. [Download and install the app](https://alysiscode.com/#desktop-download).
2. Connect your preferred model in **Models & keys**.
3. Open a repository in Code, bring a file into Work, or set up an Owl.

[Desktop guide](https://alysiscode.com/docs/desktop) · [Your first task](https://alysiscode.com/docs/desktop/first-task) · [Release notes](https://github.com/AlysisAi/AlysisCode/releases)

## Also available in your terminal

The open-source Alysis engine and CLI live in this repository. The CLI supports macOS, Linux, and Windows.

<p align="center">
  <img src="docs/assets/demo.gif" alt="Alysis Code CLI welcome screen followed by a short conversation" width="900">
</p>

<details>
<summary>Install the CLI</summary>

```bash
# macOS, Linux and WSL
curl -LsSf https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.sh | sh
```

```powershell
# Windows PowerShell
irm https://raw.githubusercontent.com/AlysisAi/AlysisCode/main/scripts/install.ps1 | iex
```

The installers manage Python for you. Run `alysis` to get started and `alysis update` to upgrade.

Already using Python 3.11+? Install with `uv tool install alysis-code` or `pipx install alysis-code`.

[CLI documentation](https://alysiscode.com/docs) · [VS Code](extensions/vscode-alysis/README.md) · [JetBrains preview](extensions/jetbrains-alysis/README.md)

</details>

## Community and contributions

[Join Discord](https://discord.gg/psEVWZgzFS), [report a bug](https://github.com/AlysisAi/AlysisCode/issues), or read the [contributing guide](.github/CONTRIBUTING.md). Report vulnerabilities privately through the [security policy](.github/SECURITY.md).

## License

The engine and CLI in this repository are licensed under Apache-2.0. See [LICENSE](LICENSE), [NOTICE](NOTICE), and the editor sidebar's [third-party notices](extensions/shared-ui/THIRD_PARTY_NOTICES.txt).

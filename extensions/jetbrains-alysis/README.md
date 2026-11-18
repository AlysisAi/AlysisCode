# Alysis Code for JetBrains — development preview

This IntelliJ Platform plugin renders the shared Alysis sidebar and connects to the existing Python
agent using protocol v1 over stdio. It supports chat, streaming, permission modes, approval/denial,
cancellation and new conversations. VS Code and Cursor retain their existing controllers and features.

## Build and test

Use Python, Node.js, the repository's Python dependencies, the VS Code extension's npm dev dependencies,
and a local IntelliJ Platform installation containing its JDK 17 compiler. No Gradle download or global
IDE configuration changes are required. From the repository root on Windows:

```powershell
.\.venv\Scripts\python.exe extensions/jetbrains-alysis/build.py --ide-home 'C:\Program Files\JetBrains\CLion 2022.3.2'
.\.venv\Scripts\python.exe extensions/jetbrains-alysis/test.py --ide-home 'C:\Program Files\JetBrains\CLion 2022.3.2'
node --test extensions/shared-ui/test/*.test.cjs
```

The generated installable ZIP is `build/alysis-code-jetbrains-0.1.0-preview.zip`. The build includes
the shared sidebar and its third-party notices. This was compiled against platform 223 with Java 17.
The manifest uses platform APIs only; other IntelliJ-based IDEs and newer platform builds still need
compatibility verification. A JCEF-enabled JetBrains Runtime is required.

The automated smoke test uses the actual sidebar DOM and JavaScript, JetBrains JS adapter, production
Java subprocess transport, and real Python `StdioBridge`. A deterministic agent supplies responses
and approval requests, with separate disposable project/config/data directories and no provider calls.
It checks rendered output, composer acknowledgement, allow/deny, cancellation, new task and shutdown.
It runs in JSDOM; it does **not** verify native JCEF rendering, IntelliJ project trust/file dialogs,
or the native session-ownership dispatcher inside a running IDE.

## Try the preview

1. In a disposable JetBrains test profile, use **Settings → Plugins → Install Plugin from Disk** and
   choose the ZIP, then restart as prompted. It has not been installed in your existing profile.
2. Open a trusted local project and the **Alysis Code** tool window.
3. Select **Choose CLI**, choose your separately installed/configured Alysis executable, and accept
   the native development-runtime prompt. The host connects after this explicit selection; it never
   starts the CLI merely because the tool window opens. **Settings → Local agent** also exposes
   **Choose CLI** and **Connect**.
4. Send a task. Use the permissions control, approve or deny requested actions, and use **Stop**
   to request cancellation. Start a new conversation with the **New task** button.

The executable must be an absolute, executable file outside the current project. On Windows select
the installed `alysis.exe`, not a `.cmd`, `.bat` or `.ps1` wrapper. Provider credentials remain in
the CLI configuration/environment; the webview neither reads nor stores them. Changing the chosen
CLI affects future connections across projects; existing connections keep their current process.

## Limits and next work

This is an explicitly selected development runtime, without managed-runtime verification/installation.
JetBrains browser panels, history/resume, image/file attachments, editor selection, model
and persona management, host Tasks/Debug actions and slash commands are not implemented. Their UI
controls are hidden and unsupported native requests are rejected. CLI tools remain governed by the
agent's selected permissions. Conversation state is in memory and does not survive project/IDE
shutdown. A disconnected conversation cannot be resumed; its displayed text may remain in the sidebar.

Before release: dogfood the plugin inside actual JetBrains IDEs, run Plugin Verifier across the
supported versions, validate native trust/ownership boundaries and JCEF light/dark rendering, add
managed-runtime delivery, and extend the native adapters for the remaining features. Publishing and
ACP support are separate work. See `docs/multi_ide.md` in the repository root for the architecture
and verification record.

# Alysis in editors

One Python engine owns execution, permissions, tools and sessions. Each editor integration owns
project trust, choosing the executable, editor operations and the transport to the sidebar.

- **Protocol.** Editors talk to the engine over the versioned JSON-lines stdio protocol
  ([ide_protocol.md](ide_protocol.md)).
- **One sidebar.** HTML, CSS and browser JavaScript live once in `extensions/shared-ui` and are
  staged into each editor's package; lint rejects stale copies
  ([shared UI](../extensions/shared-ui/README.md)).
- **VS Code family.** The VS Code extension (`extensions/vscode-alysis`) also runs in editors built
  on VS Code. A compatibility claim for such an editor needs its real Extension Host suite to pass.
- **JetBrains.** The IntelliJ Platform plugin (`extensions/jetbrains-alysis`) renders the same
  sidebar in JCEF. Project trust and process authority stay in the native shell, never in browser
  messages. It is a development preview; see its README for what it supports and what remains.

Unsupported actions are hidden or disabled in the UI and rejected by the host.

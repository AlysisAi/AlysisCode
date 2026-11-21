# Shared Alysis sidebar

`startView.html`, `startView.js` and `startView.css` are the canonical UI sources for both editor
families. VS Code's `npm run sync:ui` stages identical copies in its `media/` directory; lint
rejects stale copies. Edit the files here, not the staged copies.

An editor can inject `window.alysisHost` before the renderer starts:

```ts
interface AlysisHost {
  postMessage(message: unknown): void;
  getState(): unknown;
  setState(state: unknown): void;
}
```

The host sends state and action results through `window` message events. The fallback transport
uses `acquireVsCodeApi`, preserving the VS Code message contract. Host implementations
must validate requests and enforce project trust, ownership and permissions independently of UI
state. Template fields are host-owned, escaped values; user/agent content uses DOM text nodes.

VS Code and editors built on it use their native view title and toolbar. The inline navigation starts hidden
and is shown only for portable hosts that inject `window.alysisHost`.

`portable-chat.js` implements the initial non-VS-Code chat controller. Its injected RPC interface
uses the existing Alysis protocol, with no `vscode` imports. JetBrains supplies process lifecycle
and native operations. It intentionally exposes only its supported chat controls.

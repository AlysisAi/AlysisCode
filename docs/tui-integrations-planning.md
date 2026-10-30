# Integration inspectors and planning

Inspectors show the selected integration's state and available actions. **I**
toggles configuration details and tools, **F1** opens help, and **Tab** focuses
the scrollable details. Errors remain visible in the default view.

In the full-screen TUI, `/mcp` shows configuration, connection state and available
tools. `/extensions` shows installed user/project records, effective precedence,
trust and configuration status. Inspection and refresh do not create connections;
credentials are redacted. Follow the displayed external commands to configure or
authenticate a service. `/plugins` is a separate command.

Use the displayed Enable, Disable or Restart keys for supported MCP operations.
Extension Enable/Disable applies to a new session. Make changes while the session
is idle. Closing the inspector cancels unstarted work; an operation already
applying finishes and reports its outcome. The composer draft is preserved.

In normal chat, `/plan` opens planning choices. Architect keeps the current
conversation and permissions. Forge opens its planner with the assistant off;
entry does not start a model turn or execute a plan. Creating its artifacts needs
explicit approval, including in auto/fullaccess. Readonly and custom write scopes
cannot enter Forge through this chooser.

`/planning` opens the chooser from chat or Forge. Choose Return to restore the
prior persona and permission ceiling while respecting newer restrictions and
retaining the conversation and Forge plan. Inside Forge, `/plan` still opens its
tasks, Markdown and edit views. Escape cancels without changing the draft or mode.
Use `/help` to find the available commands and shortcuts.

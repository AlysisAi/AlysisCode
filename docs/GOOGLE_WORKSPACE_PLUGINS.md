# Google Workspace plugins

Alysis bundles separate plugin descriptors for Drive, Docs, Sheets, Slides,
Calendar, Gmail, Chat and Contacts. A compatible desktop host supplies each
service's tool catalog through `google_mcp` and handles calls through
`mcp.google.call` on the IDE host-action channel. Credentials stay in the desktop
app; the Python engine does not receive OAuth tokens.

The host supplies the service protocol version and plugin/server identity.
`session.create` returns manifest receipts and tool names so the host can check
the connection. The engine checks the trusted installed manifest when binding
the service and before and after each call. Results are treated as untrusted
content, and read-only sessions exclude write tools.

Using these plugins requires the desktop's Google adapter, an OAuth client and
access to the relevant Google services. A bundled descriptor alone does not
establish account access. Only tools supported by the adapter and negotiated
catalog are available; the Python engine cannot change the provider endpoint or
select another account.

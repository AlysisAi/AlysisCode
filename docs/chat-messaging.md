# Communication between chats

The engine can exchange messages between existing Code/Work/terminal sessions.
Each recipient uses its own model, workspace, conversation and execution mode.

## Enable and authorize

Use the same engine configuration and sessions directory for the participating
chats. Messaging is disabled by default:

```sh
alysis config set chat_messaging_enabled true
alysis sessions messaging list
alysis sessions messaging link CHAT_A_ID CHAT_B_ID
```

Start or reopen the participating chats with this engine after enabling it. The
link is a user-authorized, bidirectional communication channel, including
follow-up questions. It remains until explicitly revoked. Linking across
projects is supported. A link authorizes communication; it does not grant file,
shell, browser, connector or approval permissions.

Then ask an agent, for example: “Coordinate with chat CHAT_B_ID about the API;
ask it what request format the frontend should use.” The agent discovers chats
and calls the messaging tools. Session IDs are the stable addresses. Discovery
returns the workspace, optional engine title, link state and online/saved state.

To revoke a link or stop one exchange:

```sh
alysis sessions messaging unlink CHAT_A_ID CHAT_B_ID
alysis sessions messaging close CHAT_A_ID EXCHANGE_ID
```

Revocation cancels queued messages and signals running peer turns to stop.
Stopping a user turn also closes that chat's open exchanges. A newly admitted
human message interrupts an automatically activated peer turn and takes priority.

## Receiving and saved chats

Open engine sessions receive messages automatically. Busy sessions ingest peer
data between complete tool batches. An idle recipient starts a peer turn. The
desktop engine bridge schedules that turn through its existing durable job
queue, retaining its normal progress, cancellation and execution ownership.

The default full-screen CLI displays live sends and receives as separate
**Sent to** / **Received from** blocks, with literal wrapped text and the stable
peer session ID. Delivery receipts update the existing block without splitting
a streaming answer or adding repeated status lines. The blocks remain visible
when tool tracing is off; full tracing retains raw tool diagnostics. Restart an
already-running CLI to load an updated renderer.

For recipients whose engine processes have exited, run the engine receiver:

```sh
alysis sessions messaging serve
```

The receiver activates saved recipients only when messages are queued. It loads
their own conversation, saved model, provider profile, permission mode, file
restrictions and retained project/memory scope. It uses the existing credential
resolver; credentials are not copied into the mailbox. Missing profiles, changed
provider endpoints and unavailable workspaces produce a failed delivery rather
than selecting a different provider. If a saved host memory scope is unavailable,
memory is disabled for the resumed peer turn. Extra project folders are never
inferred. The receiver releases a chat after it drains, allowing an interactive
engine to acquire it again. An already-running receiver turn must finish or stop
before a different engine process can take ownership of that same chat.

Ctrl+C stops the receiver and its owned sessions, retaining data. Queued messages
stay in the mailbox. A machine that is off cannot execute a recipient; delivery
continues when an eligible engine receiver runs again.

## Agent tools

| Tool | Behavior |
| --- | --- |
| `list_chats` | Show same-project chats and explicitly linked chats in other projects; no transcript access. |
| `send_chat_message` | Send to a linked recipient. Use `reply_to` for a reply/follow-up and reuse the idempotency key only for an exact retry. |
| `read_chat_messages` | Read this chat's inbox after a sequence cursor. |
| `wait_for_chat_reply` | Return current delivery status/replies immediately. If pending, finish the turn to yield; a reply wakes the chat. |
| `close_chat_exchange` | Close an exchange and cancel pending delivery. |

For a new request, `request_reply` defaults to true. A recipient replies only by
explicitly calling `send_chat_message`; its final text stays in its own chat.
This preserves a decision to decline communication and records every outgoing
reply in the model's tool history. Replies default to false: processing an
answer does not automatically generate another answer. An agent can explicitly ask a follow-up using `reply_to` and
`request_reply: true`. A busy recipient sees the request in its active task and
uses `send_chat_message` to answer it; delivery status alone never means it has
answered. Peer-triggered turns may only send within their current exchange.

## Persistence and authority

SQLite state lives in `<sessions_dir>/.chat-messaging/`, outside project source.
The directory is owner-private; messages use the engine's existing secret
redaction. The host binds the sender ID. Model arguments cannot impersonate
another chat or create links. Other local owners are filtered. This is local
same-user coordination, not an authentication boundary against hostile programs
running as the same OS user.

Peer content enters model history through a `read_chat_messages` tool result,
with sender and exchange provenance. It never becomes a logged human message
or a system instruction. Neither full histories nor memories are automatically
copied between chats. Standard transcript replay retains the tool call/result
pair and the engine records separate sent/received events.

Each logical chat has one OS-lock owner, across terminal processes and desktop
bridges. Restoring history into a new engine log aliases that log to the original
chat address. Duplicate sends are deduplicated by sender/idempotency key.
States distinguish queued, processing, delivered, failed and cancelled.
`alysis sessions messaging status CHAT_ID MESSAGE_ID` returns the actual state
and replies. Delivery is not a claim of task success.

A crash after execution begins leaves an indeterminate failed message; the
receiver never automatically repeats that turn. A user may inspect it and send
a new request. Each recipient accepts up to 100 outstanding messages. Exchanges
allow up to 32 messages; automatically activated peer
turns allow at most 12 steps, respecting a smaller existing session cap. These
are execution limits, with no language/provider-name routing or canned replies.

## Scope

Messaging is available to ordinary local interactive sessions and saved
sessions resumed by the receiver. The regression suite uses scripted model
clients to exercise engine sessions, the bridge, and the terminal renderer:

```sh
pytest -q tests/test_chat_messaging.py tests/test_tui_peer_messages.py
```

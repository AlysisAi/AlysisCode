# Changelog

## 0.18.0 - 2026-11-21

- Alysis Code no longer hands its turns to an outside command-line agent: the built-in delegated
  runtime that ran another company's CLI in place of Alysis Code's own agent is removed. The
  delegated-runtime interface stays for installed plugins only. A saved setup that still selects
  the retired runtime moves to the same subscription through Alysis Code's own agent (also for a
  custom executable or an install that asked to keep it), and the retired settings are dropped.
- The agent's identity rule is general: it never claims to be another company's model or
  assistant, or to be made by the company behind the configured model or provider.
- Feedback reports with logs include the chat's recent failure records (`terminal_error`, `error`,
  `warning`, `llm_call_retry`, `turn_outcome`; type, time and payload only, redacted, at most 30
  records and 20,000 bytes), also for a chat that is not open, read from its saved log.
- A heavy web page no longer retires a Cloud owl's computer when it is only read: the host admits
  the largest page reply at worst-case escaping; a read that is still too big says to read the page
  in parts with `browser_source`, and a step that changes something and gets no complete receipt is
  reported as not confirmed and never replayed. The browser view falls back to the page's text when
  its snapshot is too big.
- Deleting a Cloud owl works while its computer is awake: work that is running answers `409`
  ("Stop this Owl's work before deleting it."), the owl's desktop and browser sessions end and its
  computer is recovered before the delete, and a computer still settling answers `409` to try again.
- An owl's reply says how its routines are now: every step reads the routines the turn saved,
  changed or suggested (a routines note), a routine that changes while the owl writes is restated
  after the latest tool results and the response is written again (at most twice), and the tools
  never call a routine on unless the note says so.
- Owl, browser and desktop tool schemas no longer carry labels that repeat each name (owl tools
  about 9% smaller), and `check_back` and `list_files` describe their inputs once.
- Workspace skills and settings reach the desktop app and editors without loading a chat session;
  skill bundles are inspected in one pass, and MCP and HTTP clients load only when used.
- Windows file work in the contained file worker starts faster (bundled Git files are verified
  concurrently, only the tool installations the worker needs are admitted, version and capability
  probes skip Git admission) and a worker that times out reports the job it was running.
- Editor streams publish events in sequence, tell the client when a provider attempt restarts, close
  when a reply only repeats itself and end at the Responses stream's terminal event; ordinary chat
  keeps a redacted diagnostic when it salvages a provider failure.
- Provider profiles survive concurrent settings writes from the desktop app; approvals and file
  results keep primary workspace paths portable; a browser start page that cannot be opened is
  refused before its approval card, which shows the managed browser's origin.
- Saved speaker notes are checked before delivery, later requests reuse saved artifact ranges, an
  unresolved verification failure is explained from recorded evidence, bounded workspace listings
  no longer resolve ignored files, and fixed interpreter file operations on Linux use the system PATH.
- Cloud hosts metered by a separate service start without a billing connection.
- Owls answer questions about the data in their Library with one read-only SQL query
  (`query_tables`, offered with files on): delimited text (CSV, TSV, other delimiters sniffed
  from the content), a JSON list of objects or an Excel workbook (a table per sheet) becomes a
  table in a private in-memory SQLite database for that call, with simple table and column names
  listed beside the original headers. Only a SELECT runs (no ATTACH, PRAGMA or writes), for at
  most 20 seconds and up to the task's Stop; at most 500 rows and about 40,000 characters are
  shown and the rest counted. It counts as reading for routines and restarts.

- A routine can think with its own model (capability `routine_models`, schema 51): one its owl's
  place offers, set with `PUT /v1/bots/{id}/schedules/{id}/model` or given by the owl in
  `create_schedule`/`edit_schedule`/`propose_schedule` (only an offered id); each run uses the
  model its routine had when queued, by the owl's fallback rules, and a move keeps it where the
  other place offers it. The owl list's `now_doing` also carries the browser step's `variant`.
- An owl's finished, checked reply is no longer turned into a failure by what happens when its
  computer is released: a computer that refuses to save its browser storage (typed now, with
  the real error type in `computer_operation_rejected`), or storage that fails that save, keeps
  the owl's last saved browser storage, still saves the computer's files and retires it
  (`browser_state_not_saved`); any other release failure after the reply leaves the handles to
  the reaper and says so (`computer_cleanup_pending`, `after_reply`). A lasting browser profile
  keeps Chromium's disk caches outside the saved storage, and a frame that leaves the page while
  it is read (Chromium's -32602) is shown as `- frame [unavailable]` instead of failing the
  observation or a navigation; newer owl computer images.
- An owl can think with any model its connection's catalog offers the person, not only the
  connection's default: on this computer each saved connection also lists, as
  `<connection>/<model>`, the account's models on the account gateway (the plan's catalog, open
  where the gateway serves them; others listed closed with why) or the reviewed models of the
  provider it was made from, each with its declared image input, context window and reasoning.
  It thinks through the same connection with only the model changed. A catalog model no longer
  open to the person gives way to its connection's own model with a `model_fallback` notice, and
  a moved owl keeps its model where the other place offers it (`owl_move_model`).
- Owls can make and change pictures: `create_image` and `edit_image` (capability `owl_images`) use
  the engine's own image generation, are offered only with the owl's files on and a model
  connection that declares image generation (`image_generation` in `GET /v1/models`), count as the
  task's own calls, say plainly when the image service refuses for a usage limit or credits, and
  save each result as a new Library file or version with a file card; Stop mid-request keeps
  nothing.
- Word, Excel, PowerPoint and PDF files the engine makes (and owls' `create_document`) can hold
  charts: column, bar, line, pie and scatter, from one chart element in every format. Office
  files get native, editable charts (Word's with its own data workbook); a PDF draws a ```chart
  block as a vector chart in the document's fonts. Inspection lists a file's charts. An Excel
  sheet with charts prints with no chart cut by a page, also where the workbook's font is
  missing and the program printing it draws the columns wider (LibreOffice without Calibri).
- On a newer owl computer, an acting browser step (click, typing, a choice, a key, a check, a drag,
  an upload, answering a dialog) says what the website itself reported while it ran and while the
  page was observed after it: `page_reported` lists the site's own document, XHR and fetch requests
  that failed or answered 400 or above (method, path without query with ids as `:id`, status or
  Chromium's error name; at most five, the rest counted) and counts uncaught script errors. Never
  queries, bodies, headers, cookies, console text or another site's requests. The reply checker gets
  these steps as `browser_page_reports`, kept when a result moves to storage or into a progress
  record, and holds a reply that says such a send worked against them. Declared as `page_reported`
  (browser `page_report`); older images report nothing.
- Owls come back to a chat later by themselves instead of waiting inside a turn: `check_back`
  (capability `check_back`, schema 47) starts one new turn in the same chat at a time, or sooner when
  a page shows or loses a text, a Library file arrives or the owl's other work ends, checked by the
  service without a model; the chat lists waiting ones with Cancel (`GET /v1/chats/{id}/check-backs`).
- A turn cut off by an app restart (update, quit, crash or sleep) starts again once, told so,
  when nothing it did reached outside the owl's own records; one that acted, or may have, is
  interrupted as before and its failure names the step (`not_rerun`, `step`; `restarts`,
  schema 48; capability `restart_reruns`).
- A routine's run that checked and found nothing new stays quiet: the owl ends it with
  `nothing_to_report` (offered only in routine runs; refused after a step that changed
  something), so it posts no reply and sends no notification, and the routine's history says it
  checked, with the time (`nothing_new_at`, capability `quiet_routine_runs`).
- A reply the model's output limit cut off is no longer published or acted on, with any
  provider: an owl's chat turn, computer work, research helpers and peer replies record the call
  as incomplete (`output_cut_off`, naming only the tools it was writing), run none of its calls,
  tell the model to continue shorter and in parts, and give up with the usual plain reason after
  two; the next call may use more of the model's catalogued output ceiling, once per turn.
  Coding work gets such a reply as cut off, and the reply checker treats a final reply that only
  announces work still to do as an omission.
- An owl stuck repeating itself stops instead of using up the allowance (`bots/repeat_guard.py`,
  chat turns and work on its computer): a call (tool plus canonical arguments) that failed the
  same way 3 times is not run again (`{"error", "executed": false}` says why and what to do
  instead) until another step works; the same successful result 3 times gets a `repeat_notice`;
  about 10 identical attempts (only back-to-back ones for a successful result) end the turn as a
  `model` failure. Routine runs, peer exchanges and work off the reply loop use 2/2/6. A result
  with an image is a fresh look and never counts.
- A call to a tool the current step does not offer (often one from an earlier turn) is told so,
  with the tools offered now, instead of "Invalid tool arguments."
  (`bots/tool_errors.ToolNotOffered`, `executed: false`); real validation errors keep their
  `validation_errors`.
- A response is discarded for a memory change only when what it may rely on changed (approved
  memories' id, status and content, and notes the person wrote), at most twice per turn or
  computer session (`MEMORY_DISCARDS`). Memory index scopes and notes written by the owl's own
  running work no longer cost a charged call; they reach the next step.
- A rate limit no longer ends a chat or background turn when the provider says to wait 20-120
  seconds: the turn waits (Stop ends the wait), up to 240 seconds of such waits per turn or
  computer session (`PROVIDER_ASKED_WAIT_SECONDS`, `PROVIDER_ASKED_TURN_SECONDS`). The wait is a
  `provider_retry` step with `provider_asked` and a `message`; longer stated waits fail as before.
- An optional tool argument a model leaves as `null` or an empty string takes its default when
  that value would fail its check (`models.tool_input`: owl, browser and computer tools); an
  empty value a field accepts is kept. A reply that is a tool call written as text
  (`<tool_call>…</tool_call>`, `<function=name>…</function>`, or only a JSON object naming an
  offered tool) is made as the real call when it ends the reply and names a tool offered now
  (`bots/text_tool_calls.py`, chat and computer work); other such markup is never published,
  the model is asked once for a real call, then the turn ends as a `model` failure.
- Refusals say plainly that nothing was done and how to send it again: a value longer than a
  tool takes names the field and its limit and says to shorten or split it
  (`tool_errors.invalid_arguments`, with `limits` and `executed: false`; computer work now gets
  the same structured result instead of the raw validation text); a `computer_run` that timed
  out or printed too much carries a `note` that it may have partly run and to use
  `computer_launch` or a file; a file over 50 MiB is a `LibraryCapacityExceeded` ("Nothing was
  saved", so computer work can split it); peer exchanges over their text allowance say nothing
  was sent.
- Long owl tasks no longer fail when their requests outgrow what the model's route admits: chat
  turns, background and routine work, work on the owl's computer, coding work and research helpers
  also measure each request with the model client's own wire measure (`request_capacity_ratio`, the
  hosted gateway's byte bound; 0 elsewhere) and make room at 90% of it as they do for tokens, and a
  call refused as too long (`context_length_exceeded`, `hosted_request_too_large`) makes room down
  to 70% of the refused request and is sent again, at most twice a turn, recorded as a
  `context_window` step with reason `request_too_long`; a third refusal fails as before.
- On a newer owl computer, owls have fuller hands: `computer_run` waits up to 300 seconds for a
  command (in short requests, so Stop ends it at once; the computer stops it at its limit itself),
  `computer_launch` keeps a process's latest 256 KB of output for `computer_process` to read from
  a byte offset (`output_from`), `computer_pointer` double-clicks, right-clicks, hovers and drags
  at pixels or at an element's ref, `other_windows` gives every open window a ref for
  `computer_focus_window`, and `computer_read_clipboard` returns the clipboard's text as untrusted
  data (never what the person typed while in control). Each is offered only while the running
  computer declares it (`long_commands`, `process_output`, `pointer`, `windows`, `clipboard`);
  an older image keeps today's tools, bounds and requests.
- Owls' browsers go back, forward and reload (`browser_history`, reading; a page that came from
  sending a form is never reloaded, so no form is sent twice), right-, middle- (links only),
  double- and triple-click (`browser_click`/`browser_visual_click` `button`, `clicks`; the
  browser's own menu stays closed), choose several options of a list (`browser_select_many`),
  show one element or the whole page as an image to image models (`browser_screenshot` `node`,
  `full_page`, for looking only), and answer a file chooser a click opens with a Library file
  (`browser_choose_file`, approved like an upload). Offered only by owl computer images that
  declare them (`history`, `click_options`, `select_options`, `page_images`, `file_chooser`).
- Owls with many connected apps no longer send every app tool with each request: when a turn's
  list passes 96 tools, its schemas a quarter of the model's declared window, or (on a route with
  its own admission bound) the instructions and tools half of that bound, the apps' tools are
  reached through `tool_details`/`use_tool` with the same approvals and receipts, and, if that is
  still too much, every tool but the everyday ones; each turn records its mode in a
  `tools_offered` event.
- A message waiting for a working owl can be sent now (`PATCH /v1/tasks/{id}/queued` action
  `steer`, capability `queued_message_steer`): the running turn reads it as the person's own words
  at its next model round and holds a reply that was about to go out; schema 49.
- Owls remember what the person says in passing: once an owl's own chat has been quiet for two
  minutes after a checked turn, one bounded model call without tools (on the owl's model, within
  the allowance) reads only the person's own words since the last read and keeps at most five
  durable facts, each only if its quote is in the person's message and it isn't saved already;
  an update replaces the memory it names. They are kept with Undo under the reply they quote
  (`captured: true`, capability `memory_capture`, schema 50), never from replies, tool results,
  web pages, routines or rooms, and a new message, archiving, deleting or memory off cancels a
  read. Once a day exact duplicate memories are merged through the same replace path, with Undo.
- An owl's working line and steps can follow its task as they happen: `POST /v1/tasks/{id}/events`
  (capability `live_task_events`) answers as soon as the task has new events, or empty after a
  wait of up to 10 seconds, for the task's owner only. Progress only: the reply never streams, and
  the reply checker's verdicts and suggested next messages come with their data withheld.
- Owls can install the Python packages a job needs on their own computer: `pip install` in its
  shell reaches the declared package index (`bots/package_indexes.json`, PyPI) through a guard
  that admits only those hosts over TLS, and nothing else; packages stay inside the owl's
  computer and installs are approved like any other command there. Computers declare it in
  their command receipts (`package_indexes`), only on an image with the guard and pip (the
  Cloud image adds pip); older images keep a fully offline shell. The npm registry stays out:
  it accepts uploads on the host it serves installs from.
- On a newer owl computer, owls read a form inside another site's frame in that site's own
  process, so a card number typed in a payment frame is paying and a password in a sign-in frame
  is signing in, in any language (an older computer, or a frame over the read's limits, still
  waits for the person every time). A step can confirm the question it opens itself (Delete,
  then Are you sure?) when the request means yes: its review and approval card show that it also
  says yes, and it accepts only that one question, from the site it acts on, never one the page
  asked before the step (that stops the step with nothing sent). A dialog from another site's
  frame is checked and reviewed as that site's and answered only in that frame. A pending
  approval carries the `effect` that made it wait (schema 46).
- Owls read the windows of their computer as text: on a computer image with the accessibility
  bus, observing also returns the focused window's elements, one line each (role, name, states
  such as checked or disabled, a field's text; a password shows only `[private field]`), with
  refs that stay with an element while it lives and a clear "Stopped at…" line when a window is
  too big. `computer_element` activates, fills, selects or ticks one by its ref through the
  application's accessibility interface, else by clicking its centre, and sends nothing (observe
  again) when it closed or changed. A model without image input observes (the element view
  alone) and presses keys there. Offered only while the running computer declares it, so older
  images keep today's tools; approvals are unchanged. The computer images add `at-spi2-core`,
  `python3-gi` and `libreoffice-gtk3`.
- Owls can look into several separate questions at once: `research_helpers` sends up to three
  read-only helpers in parallel, each with one question (and links to start from). A helper only
  searches and reads public pages with the owl's own model at work strength, within six model
  calls, ten web steps and three minutes, and reports facts with the link each came from and
  what it could not find, in its question's language; a link counts as opened only if the helper
  read it, and a progress record keeps exactly those. Helper calls are the task's own calls and
  always leave two for the owl's answer; Stop or a used-up allowance stops them and the owl gets
  a plainly partial result. Offered with web lookup to a model that calls tools; the task's
  activity shows each helper, what it looked into and how it ended.
- Owls keep playbooks: after a run that worked and whose reply passed the reply checker, an owl
  may keep a short procedure for that job (when to use it, steps, pitfalls met, one check that
  proves it is done, and the tools the run used, from its receipts) with `save_playbook`; later
  turns and routines see an index and read one with `read_playbook`, and a better run saves a new
  version. Nothing is kept from a failed, stopped or unchecked run. At most 20 per owl, three
  earlier versions each; Undo restores the version before. `/v1/bots/{id}/playbooks` lists,
  reads, undoes and deletes them (capability `owl_playbooks`, schema 45); they move with the owl
  and are erased with it.
- Owls use websites for the person's own request without a card per site: reading, scrolling,
  searching, filtering, paging and filling in fields go ahead on any site the request needs.
  Every input still passes the action review, and a step that sends, posts, pays, books, signs
  in, deletes, uploads or submits personal details waits for the person's approval (password,
  payment-card, file and filled contact fields are read from the page's HTML; the reviewer
  names any other effect, and asks when it can't). Routine runs keep their declared sites, Do
  it cards for a site work as before, and without the action reviewer input still needs a card.
  Filling, key chords, ticking boxes, dragging and answering a page's dialog follow the same
  rule (the reviewer reads the dialog's text); a field inside another site's frame is judged by
  that frame's own form, and when the owl's computer can't read it the step waits for the person.
- Owls fill form fields and check what they hold, check and uncheck boxes, press shortcuts,
  hover to open menus, drag and drop, wait for a page to show something, answer a page's pop-up
  question, and reach fields and buttons inside embedded frames (such as a payment form), on an
  owl computer that has these new browser abilities; older owl computers keep today's tools. A
  page's pop-up question no longer stalls the owl: it reads the question and answers it as
  website input, under the same permission and review as a click.
- Owls think at work strength in every turn, when they work on their computer and in coding
  work: the model connection's own reasoning effort when it sets one (a connection set to
  "auto" sets none), otherwise medium for a model that declares reasoning in the model
  registry, otherwise none. The connection's own setting is never changed. Coding work
  survives a temporary model provider error: the controller answers `503` with `retry_after`
  and the worker sends the step again (at most three times in a row, each attempt charged to
  the job's allowance); a worker from before this change behaves as before.
- Owls make, change and check Word, Excel, PowerPoint and PDF files without their computer:
  `describe_document_format`, `create_document`, `inspect_document`, `edit_document` and
  `check_document`, built on the engine's own office operations in a per-task scratch folder.
  A file reaches the owl's Library only when no check failed; a check that can't run on this
  computer is reported as not checked, never as passed. Offered when files are on and the engine
  has the format's libraries; the private computer stays for other files and applications.
- Owls read web pages as a page view: one line per element from the browser's accessibility
  tree (role, name, state, value, link address), about 20,000 characters, with refs that stay
  usable while the element is on the page and are checked against the page before every action.
  `browser_source` reads on in 20,000-character parts. The DOM view remains as a fallback, and
  an observation says when and why it is used.
- A long owl turn whose own steps fill the context compacts its finished steps into a progress
  record and carries on instead of stopping: a summary from the engine's compactor, the steps'
  receipts (links opened, file and card ids, outcomes) copied by the service, and the exact steps
  kept for `read_tool_result`. The summary call retries temporary provider errors, and the reply
  checker counts a progress record's receipts as evidence.
- Owls' standing guidance is short named sections that say each rule once. Each turn is offered
  only the tools it can use; a model whose declared context window is under 100,000 tokens gets
  a short list plus `tool_details` and `use_tool`, which run a hidden tool through the same
  checks. Owl model calls use the engine's prompt cache, keyed to each owl.
- PDFs that Work and owls make can now contain Chinese, Japanese and Korean text, which wraps
  inside the page instead of failing; these characters print in regular weight, even in bold
  text and headings.
- Owls now use the engine's built-in skills (research, code review, debugging and more) while
  they work on their computer. Every session offers a skill only when it has each tool the skill
  names, so an owl is never pointed at a tool it doesn't have; the document and file skills built
  on the desktop app's own tools stay in Work and Code. A skill lists the tools it needs with
  `uses_tools` in its SKILL.md.
- Each owl in `GET /v1/bots` carries `next_routine_at`: when its next routine run is due, from
  its routines that are on and the retries of failed runs (capability `next_routine_at`). While
  a plan or usage limit holds a routine, its first run after the limit frees up counts. Paused
  routines and archived owls never count, and a time already past stays until the run starts.
  The desktop app keeps the computer awake shortly before it, so routines on this computer
  aren't missed because it went to sleep.
- An owl can offer to make a need the person shows repeats into a routine (capability
  `routine_suggestions`, schema 44). `propose_schedule` now saves its suggestion as one card
  under the reply (task `routine_suggestion`: what it does, when, and what it uses) as well as a
  draft in Routines; nothing runs until the person answers it with
  `PATCH /v1/bots/{id}/routine-suggestions/{id}`: `turn_on` (with a `request_id`, so sending it
  again turns on one routine; the person's own name, job or timing; and `allow`, exactly what the
  card showed when the routine uses the owl's computer, browser or websites) or `decline`, which
  removes the draft and remembers the decline by a Unicode-normalized, case-folded fingerprint of
  its job and timing so the owl never offers it again. The owl offers one only while answering the
  person's own message (never in routine runs, file events, peer exchanges, first hellos or
  another answer), one waits at a time (a newer one replaces it), and a routine it already has is
  refused. Open suggestions stay out of Needs you. Suggestions move with the owl (record kind
  `suggestion`, sent only to a place that lists the capability) and are erased with it.
- Rooms: one conversation with two to six of the person's owls in one place (capability
  `owl_rooms`, schema 44). `/v1/rooms` makes, lists, renames, changes and deletes rooms (deleting
  a room never deletes an owl); `POST /v1/rooms/{id}/messages` writes in one. The owls
  @mentioned answer, in the order mentioned (names in any script, matched case- and
  accent-insensitively), else the owl picked, else the one that answered last; each answers as a
  normal turn of its own, one after another, reading the room's recent messages labelled with who
  said them, where other owls' words grant nothing. Owls never start each other's turns. A room is
  read and marked read like a chat (`/v1/chats/{id}/page`, `/read`); its turns carry their
  questions, approvals and suggestions, and Needs you names the room. An owl that is deleted or
  moves leaves its rooms; an owl's own chat, history and moves leave rooms out.
- An owl offers choices with its question only when each choice fully answers that question, and
  none when the answer is open (a place, a name, a date, an amount), so the app shows a field to
  type in instead of choices that answer something else. Both the reply guidance and the
  `ask_clarification` tool say so.
- Pin an owl to the top of the list, archive it, or duplicate it (capability `owl_organize`,
  schema 43). Owls carry `pinned_at` and `archived_at`, set with `PATCH /v1/bots/{id}/organize`
  without touching the owl's setup. An archived owl keeps everything, but its routines and file
  events rest and Needs you leaves it out; work the person starts for it brings it back, and its
  routines go on from their next time without replaying what they missed. `POST
  /v1/bots/{id}/duplicate` makes a new owl with the same setup, look, apps and approval mode
  (and, if asked, its approved memories), never its chats, files, routines or sign-ins; a
  retried duplicate with the same `request_id` makes one owl. Archived owls count toward the
  30-owl limit.
- An owl can move between the person's places, this computer and Cloud, keeping its setup and
  look, approval mode and rules, the apps it may use, its chats (messages, replies, steps,
  attached files, reactions, messages changed or cancelled before they started, and what was
  read), memories, working notes with their history, routines and
  file events, and every version of its Library files (capability `owl_move`, schema 43). The
  desktop app reads it page by page (`GET /v1/bots/{id}/move`, `POST …/move/export`), the other
  place takes the pages into a hidden copy (`POST /v1/owl-moves`, `…/pages`), files travel
  through the ordinary resumable uploads, and `…/complete` checks every file by size and SHA-256
  before showing the copy. The original is deleted through the ordinary delete path
  (`POST …/move/finish`) only if it didn't work or change meanwhile. Routines and file events
  arrive off (`pause_code` `moved`) and ask again for what they may use, so nothing runs in both
  places. A retried move with the same request id never makes a second copy; every part is
  validated as untrusted input, and a move nobody finishes is cleared after a day.
- A person can ask an owl for another answer to their latest message from its reply
  (`POST /v1/tasks/{id}/regenerate`, capability `regenerate_reply`). The new answer is a turn of
  its own that reads the person's message and files again, with the answer it replaces as
  reference data; the chat keeps both, and later turns read only the newest finished answer. Only
  the latest finished reply to the person's own message can be answered again, never while the
  owl works, and the same `request_id` returns the turn already asked for.
- Conversations and their pages carry `read_at`, when the person last read them, so the app can
  show where the messages that arrived since begin.
- A message the person sends while their owl works can be changed or cancelled until the owl
  starts it (`PATCH /v1/tasks/{id}/queued`, capability `queued_message_edits`). Each such
  message's task carries `queued_message` (`revision`, `editable`, `edited`, `withdrawn`), and
  every task carries `started_at`. A change keeps the message's files; a cancelled message
  leaves the conversation and the owl's context. A change and the owl starting the message never
  both happen: the answer's `outcome` is `edited`, `cancelled`, `started` or `changed`, and the
  same change again gives the same answer (schema 43).
- Thumbs up or down on an owl's reply, with an optional short note
  (`PUT/DELETE /v1/bots/{id}/messages/{id}/reaction`, capability `reply_reactions`); the reply's
  task carries `reactions`. Later turns of that owl read the newest reactions in their task
  state within a fixed size, labelled as the person's feedback and never as instructions.
- The Liberation Sans fonts used for PDFs now ship with their own license, the Liberation
  Fonts license (GNU GPL v2 with Red Hat's font exception), instead of the newer fonts'
  SIL Open Font License text.
- The sandbox image's Go toolchain is 1.26.11, which fixes CVE-2026-97054 (an HTTP/1 server
  using `MaxBytesReader` could crash when its handler reads the body while writing headers).

## 0.17.3 - 2026-11-18

- Add optional anonymous activity counts for the CLI, off by default: `ALYSIS_USAGE_METRICS=1`
  counts the days a run or chat task is started, with a random installation identifier, and
  `DO_NOT_TRACK=1` turns it off. No prompts, paths, host names, account details or credentials
  are sent, engines started by the desktop app are not counted, and a request never holds up a
  task.
- Fix the picomatch ReDoS advisory (GHSA-fc5q-39xc-vjpf) in the sandbox image's npm and the VS
  Code extension's build: both use picomatch 4.0.7 rebuilt with the upstream parser fix
  (`4.0.8-alysis.1`), with its matcher and hostile-input checks run in the image build.
- On Windows, contained file reads, writes and edits run in fixed workers started directly inside
  the MXC sandbox, whatever folders commands may reach, and each worker loads only the code it
  needs.
- The desktop app reads the chat commands from the engine's capabilities at startup
  (`chat_commands`), and its capability and version probes no longer load the terminal interface.
- About me: what the person wrote about themselves, shared by all their owls. The app writes
  it to each place (`PUT /v1/settings/about-me`, capability `about_me`); every turn reads it,
  labelled as the person's own words and apart from each owl's memories, within a fixed token
  budget that leaves out the last facts when it is long. Owls may suggest adding a fact the
  person stated in a chat; nothing is added unless the person accepts it.
- Each owl uses only the desktop app's plugins the person leaves on for it (`GET/PUT
  /v1/bots/{id}/plugins`, capability `owl_apps`). New owls and owls from before have every
  connected plugin on. A plugin switched off for an owl isn't offered to it in any turn, and a
  call already chosen is refused before anything is sent, with a coded refusal (`app_off`).
- An owl can ask for an app its request needs that is off for it or not connected
  (`need_app`, by the app's id): the task's `app_needs` carry the card, Needs you lists it, and
  turning it on or connecting it (`PATCH /v1/bots/{id}/app-needs/{id}`) carries the request on
  as a turn with no new message. The desktop app's catalog may name every app owls could use
  (`apps`).
- Routines can use the owl's offline computer, its online browser and named websites. The owl
  declares what a routine needs (`needs`: `offline_computer`, `online_browser`, `sites`) when it
  creates, suggests or edits it; the person allows exactly that once for the routine
  (`PUT /v1/bots/{id}/schedules/{id}/access`, optionally turning it on and running it now), and
  each run uses what was allowed when it was queued, without a card. A run that asks for more
  pauses its routine (`pause_code` `access`, with the request in `access.requested`) and shows in
  Needs you. A changed job or needs asks again, `DELETE …/access` revokes from the next run, and
  deleting a routine or its owl deletes what was allowed. Website actions that need review still
  ask, and peer reports and activations still get no computer (capability `routine_access`,
  schema 42).
- An owl keeps the look the person picked for its face: its colour and the background behind it
  (`color`, `background`, capability `owl_looks`). Set when it is made or later, a look is a label
  like the name and changes at once even while the owl works. A change that leaves a part out
  keeps it, so older apps renaming an owl don't reset it; null goes back to the app's default.
  Owls and Needs you items (`bot_color`, `bot_background`) carry it.
- Each owl can think with a model of its own (`model`, capability `owl_models`, schema 43), picked
  from the models its place offers (`GET /v1/models`, with each model's declared image input and
  context window): on this computer the person's saved model connections, in the Cloud the
  service's own model and the others it offers on the same endpoint (`Settings.models`,
  `ALYSIS_BOTS_MODELS`). Null is the place's own model. Every turn of the owl (chat, routine runs,
  first hellos, carrying on, background work, its answers in exchanges with other owls, a coding
  worker's calls) thinks with it, with the context and reply allowances its capacities give. When
  the place no longer offers it, the turn fails with failure kind `model` and says so, a routine
  pauses (`setting`), and nothing thinks with another model instead. Like a look, it changes at
  once and a change that leaves it out keeps it; picking a model the place doesn't offer is refused.
- Owls know the person's local date, weekday, time and time zone on every turn,
  including routine runs, first hellos and exchanges between owls, so "today",
  "tomorrow" and "9 AM" mean the person's own. The app sends its computer's time
  zone (`PUT /v1/settings/time-zone`, capability `time_zone`); a new zone applies
  from the next turn. Without one, owls say they are working in UTC.
- Daily and weekly routines an owl sets up use the person's time zone unless they
  name another; an edit keeps the routine's own zone.
- Say why an owl's turn stopped: a failed or interrupted task carries `failure` with a closed
  `kind` (`sign_in`, `model`, `plan_limit`, `computer`, `network`, `service`, `task_limit`,
  `unknown`), decided where the turn fails from structured data, never from error wording,
  plus the model service's status and code when it answered (capability `task_failure_kinds`).
- Creating an owl takes an optional `request_id`: sending the same create again returns the
  owl already made instead of a second one, and the same id with other details is refused
  (capability `bot_create_request_id`).
- A conversation's tasks list the actions a person approved or declined (`decided_actions`),
  without the review screenshot, and a plugin step records its effect label.
- Read an owl's conversation a page at a time (`POST /v1/chats/{id}/page`, capability
  `conversation_pages`): the latest messages with their tasks, earlier pages on request, and
  polls that return only what changed, so long chats open and refresh quickly.
- List everything waiting for the person across their owls in one read
  (`GET /v1/needs-you`, capability `needs_you`): questions, approvals, suggestions, help on an
  owl's computer, routines that paused or wait to be turned on, and unseen failed turns.
- Search an owl's messages in any language: matching is Unicode-normalised and case-folded,
  and when nothing matches exactly it also ignores accents, full- and half-width forms and
  spacing. The app's search shows the words around each match.
- Skipping an owl's question no longer stops its work: the owl carries the request on
  without the answer, picks sensible defaults and says which, unless the person has asked
  something newer since.
- An owl sets itself up from the person's own description of the job: its new
  `propose_setup` tool puts a one-line purpose, working instructions and an optional routine
  on one card under its reply (the task's `setup_proposal`, and a `proposal` with
  `setup: true` in Needs you). `PATCH /v1/bots/{id}/setup-proposals/{id}` applies it, with
  the person's own changes, or declines it (capability `owl_setup`). Apply changes the
  purpose and instructions through the owl's update path and saves the routine as a draft
  the person turns on, in one step; the same answer twice makes one change, a card made
  before the owl's setup last changed is refused, and a setup the person said Not now to is
  never proposed again. Only the person's own request in the chat can lead to one.
- See the sites an owl's browser is signed in to and sign it out of one site or of every
  site (`GET /v1/bots/{id}/sign-ins`, `POST /v1/bots/{id}/sign-ins/sign-out`, capability
  `owl_sign_ins`). The list comes from the browser's own saved storage (its cookies, site
  storage and visited pages), never from page text; signing out of a site removes that site's
  cookies and stored data and leaves every other site as it was. An owl computer image with
  this engine also signs its running browser out at once (cookies and all of the site's
  storage); with an older image, sign-out waits until the owl's computer closes and the answer
  says so (`reason: computer_in_use`).
- Sign-ins a person makes while only viewing an owl's computer are kept for its next browser;
  saving that storage used to fail when no task owned the computer.
- When an owl needs the person on its computer it says what for, as a closed `kind` of its own
  tool call (`human_check`, `code`, `sign_in`, `permission`, `other`). For a human check, a code,
  a sign-in or a permission prompt it first waits a few seconds and reloads the page once (a
  code page is not reloaded), and asks only if it is still blocked. The request and its Needs
  you item carry `help: {kind, site, site_name}` from the browser's page. The owl still never
  types passwords or codes and never solves human checks itself.
- Remove Forge: the `alysis forge` commands, the `/forge` chat mode and its plan views,
  parallel swarm runs, the Forge host protocol, the IDE bridge `forge.*` and `diff.*`
  methods, and the server's Forge jobs. Use chat with the Architect persona to plan, and
  subagents for parallel exploration. Plans and logs that Forge already wrote stay on disk.
- Settings that name a runtime only Forge used (`forge_exec`, `swarm_worker`,
  `conflict_auto_resolve`) are skipped. An MCP server, hook or custom tool scoped only to
  those runtimes no longer runs anywhere; a custom tool scoped only to them is reported as
  invalid.
- Stop offering the merge-conflict review and resolve role models, which only Forge used.

## 0.17.1 - 2026-11-16

- Add the Pro plan to `alysis login`, the terminal interface and the hosted model
  catalog, with Pro's hosted models.
- Show "Between tools" as a model's thinking setting when up-front thinking is off but
  the model still thinks between tool calls.
- Add `alysis limit-reset`: list the weekly resets the account was given, or use one
  (`--use <id>`, asks first unless `--yes`). A weekly reset refills the weekly and the
  5-hour limits at once; credits don't change. The desktop app runs it with `--json`.
- Give a Cloud owl one desktop for Google Chrome and its applications, usable from the
  live view.
- Give owl desktops a compact dock with readable icons, and stream the live view over a
  private, read-only channel.
- Keep track of browser windows a person opened before an owl's first navigation.
- Keep a computer's controls when it resumes from an older owl computer image.
- Build Mac owl computer images that the desktop app downloads on first use, and record
  Google Chrome's notices in Windows images the same way as in Mac images.
- Apply CLI dependency overrides the same way whatever order they are initialized in.

## 0.17.0 - 2026-11-12

- Consolidate Alysis engine-interface documentation under `docs/` and link its
  packaged message schema and conformance fixtures; retain compatibility identifiers.
- Add Alysis Owls (`alysis bots`): assistants with their own memory that work on a
  private computer on this PC or in the cloud, run routines, and stop for approval
  before anything that reaches other people, pays, posts or deletes.
- Let Work and owls use connected plugins. Reads run directly; a change inside your
  own account follows the approval mode; changes that reach other people or can't
  be undone ask every time. An agent can show a plugin's Connect card and uses a
  connected plugin before that service's website.
- Name each command step in the work trace in the agent's own words, in the
  conversation's language, and mark which step titles the model wrote.
- Keep long tasks going past the model's input limit: compact the oldest finished
  steps of the current request, and learn the real limit from a provider's
  "too long" refusal.
- Continue a task when a reply is cut off by the model's output limit, instead of
  failing it.
- Keep a reply the model already wrote when its next response comes back empty.
- Let desktop hosts offer two or three suggested next steps under an answer.
- Size Word table columns to their content.
- Offer image input on every hosted model, with disclosed assistance where a model
  cannot see images itself.
- Restore engine features that the 0.16.3 public baseline left out, while keeping
  every 0.16.1-0.16.3 fix.
- Stage, commit and branch without a separate approval in "Approve for me", on macOS
  and in the Windows sandbox. The operating system keeps a command from changing what
  a later Git would run (configuration, hooks, a rebase); a push still asks a person.
- Let commands in the Windows sandbox follow the approval mode, run the programs on
  your PATH, and use Git in the project they run in.
- Decide a forced folder delete (`rm -rf`, `Remove-Item -Recurse -Force`) through the
  approval mode every time instead of refusing it. A delete of a drive, a home folder
  or a system folder stays refused, and `fs_delete` says when it was given a folder.
- End a finished task without sending the model back to work when its result was a
  commit in a new repository, removed files, or no file change at all.
- Compact chats and runs the same way: whole tool exchanges, oldest first, sized to
  what must be freed, with the request being worked on and the recent steps kept.
- Take a task's requirements and file roles from the model's declaration and from
  facts, in any language, instead of from English phrases in the request.
- Begin every request of an owl the same way, so a provider can cache the
  conversation; what belongs to one task follows it.
- Let the model choose how long a command may run (`timeout_s`, up to 600 seconds).

## 0.16.3 - 2026-11-08

- Install the required `jsonschema` dependency for ordinary CLI installations,
  fixing the first-request crash reported in #46.
- Give the ordinary terminal chat's slash-command menu a dark background and
  green selection, matching the full-screen interface (#44).
- Fix native Windows sandbox startup and cancellation by separating the engine's
  cancellation signal from the helper's process-containment job. Wait for child
  termination and permission cleanup before reporting completion.
- Verify existing protections on system-owned runtime parents such as Program
  Files without attempting to change their permissions; retain explicit deletion
  protection on writable parents.
- Validate release wheels in a fresh virtual environment with runtime dependencies
  only, including a complete first agent turn with a local model-response fixture.

## 0.16.2 - 2026-11-07

- Align the VS Code extension's tested CLI version and documentation with the
  Python package version, fixing the release compatibility check.
- Refresh the sandbox's frozen OS packages and rebuilt npm runtime to address
  newly reported high-severity dependency findings, keeping vulnerability scans
  and authenticated, hash-pinned inputs required.
- Include the linked-chat messaging, structured guardrail denials and editor
  bridge improvements listed under 0.16.1. Version 0.16.1 was tagged but was not
  published to PyPI; its source tag remains unchanged.

## 0.16.1 - 2026-11-04

- Add typed VS Code bridge calls for skill imports and updates, skill and hook
  documents, reviewed plugin descriptors, and completed job output paths.
- Keep host interpreter examples out of native document capability responses.
- Let explicitly linked chats exchange messages and replies across local projects
  while keeping each chat's model, workspace, and permissions. The terminal
  displays sent and received messages with delivery status. See
  [chat messaging](chat-messaging.md) to opt in.
- Record machine-readable reasons for policy, approval, and hook denials in
  engine-owned tool-result metadata. The codes remain available in session logs
  when large tool outputs are stored separately.
- Run public source checks once before the Python test matrix and retain
  test timing reports for each Python version.

## 0.16.0 - 2026-11-01

- Update provider models, reasoning controls, and context limits across CLI
  settings and requests. Keep NVIDIA tool catalogs within request limits and
  preserve conversation history when switching to Reflection.
- Show Beta and Alpha model availability and shared credit usage from the
  account catalog.
- Require separate approvals for GitHub credentials and write actions,
  including continued shell commands and scoped services.
- Improve cancellation, process ownership, retry recovery, and completion reporting.
- Expand desktop bridge, document artifact, and native Windows sandbox contracts.
- Refresh pinned dependencies and installed-package qualification checks.
- Preserve Git rename and copy paths containing ` b/` directory components
  when diff headers require adjacent metadata to disambiguate the paths.
- Recognize Alibaba Model Studio endpoints by owned hostname boundaries rather
  than applying Qwen provider behavior to unrelated custom hosts.

## 0.15.2 - 2026-10-21

- Restore the default hosted API URL so a fresh installation can run
  `alysis login`, read account status, and prepare online feedback without
  environment configuration. Custom deployment overrides remain supported;
  project keys and server credentials are not bundled.
- Add a Windows PowerShell installer that provisions uv and Python, detects
  conflicting installations, and updates PATH for the current and future terminals.
- Clarify PowerShell, WSL, pipx prerequisites, and the account sign-in steps.
- Require a complete simulated device login from the installed release wheel,
  with no cloud environment variables, before publishing Python packages.
- Refresh the sandbox's pinned certificate, curl, and Go security updates on
  both supported architectures, with native image scans and smoke checks.
- Require patched Certifi and Pillow releases and refresh their locked versions
  so new and upgraded installations receive the current security fixes.

## 0.15.1 - 2026-10-15

- Keep the release qualification's simulated stalled provider connected until
  the test completes. Its previous five-second disconnect could report a
  provider failure before the managed deadline on Windows. Deadline and
  process-cleanup assertions remain enforced.
- Include all changes listed under 0.15.0 below. The 0.15.0 tag is preserved,
  but its release qualification failed before package publication.

## 0.15.0 - 2026-10-15

- Expand the terminal workspace with inline diffs, file mentions, prompt history,
  session navigation, integration inspection, planning, review, tool traces,
  structured questions, and clearer subagent progress.
- Add IDE command and skill discovery, explicit skill invocation, mid-turn
  steering, and opt-in reasoning summary events.
- Improve ChatGPT subscription registration, account selection, credential
  refresh, saved profiles, endpoint migration, and reviewed model discovery.
- Add GPT-6.1 Sol presets and model-specific reasoning controls. Preserve
  reasoning choices across settings reloads and separate safe reasoning summaries
  from final answers.
- Recover more reliably from output limits, interrupted streams, provider retry
  exhaustion, and cancellation. Improve caching and provider diagnostics.
- Add approval review and safer command classification, with fixes for Windows
  command execution, macOS image paste, sandbox fallback, and local listeners.
- Clarify hosted-account billing and capacity errors without mislabeling them as
  rejected API keys; direct model configuration guidance to `/config`.
- Remove `/objective` and explicit task-relation state. State the current goal in
  the conversation; steer an active turn when requirements change.
- Stamp builds consistently and persist an installation identifier across client
  sessions.
- Add optional PDF, Word, spreadsheet, and presentation tools through the
  `documents` extra, with path-scoped input and output acceptance checks.
- Add host integration contracts for browser and computer actions, GitHub,
  Google services, open panels, task budgets, and Forge verification.
- Route `forge plan` and `forge run` through the shared host planner and approval
  flow. Machine planning accepts an explicit goal and intent.
- Add account usage/status, reviewed feedback delivery, corporate CA support,
  and safer managed-session persistence on Windows.
- Add `profile check`, subagent settings, bundled Alysis documentation guidance,
  and persona restoration when IDE sessions start or resume.
- Fix Windows `.git/info/exclude` line endings and VS Code provider setup,
  startup readiness, and package inventory checks.
- Align public links and release provenance checks with `AlysisAi/AlysisCode`.
- Keep hosted deployment URLs and project keys out of public source and packages.
  Hosted login, account status, and online feedback require runtime configuration
  through `ALYSIS_CLOUD_API_URL`; see [Reference](reference.md). Other configured
  providers remain available.
- Preserve focused setup actions during streamed sidebar updates and avoid
  repeatedly searching the transcript for static controls.
- Keep background update checks bound to the data directory that scheduled them.
- Repair sandbox dependency locks and server packaging, and update the bundled
  Go toolchain to the 1.26.9 security patch.
- Retire the `sylliptor` command and legacy branding aliases. Use `alysis` or
  `alysis-code`, canonical `ALYSIS_` environment variables, `.alysis` project
  data, and `alysis-plugin.toml` manifests. Back up and migrate legacy locations
  explicitly before upgrading; this release does not move them automatically.


## 0.14.9 — Claude Haiku 5.5 on Alysis Free — 2026-10-07

- Add Claude Haiku 5.5 to the Alysis Free model picker and direct Anthropic
  profiles, with native Messages routing, a 1M context window, prompt caching,
  and adaptive thinking. Choose low, medium, high, xhigh, max, or off; Auto uses
  the model's medium default. Pricing increases 5x above 100K total prompt tokens.
- Replace the retired Sonnet Free campaign entry with Haiku 5.5. Existing
  account credits and saved model preferences are retained.

## 0.14.8 — production VSIX verification

- Run final candidate evidence validation inside the frozen Python environment,
  where its cryptographic verification dependencies are installed.
- Update the VS Code test utility to 3.1.0 so current macOS releases resolve
  their executable from the application metadata, while older versions remain supported.
- Exercise both minimum and current Stable VS Code in extension CI before tagging.
- Retry interrupted or incomplete pinned Python downloads up to three times,
  requiring the original size and SHA-256 checks to pass before extraction.

Version 0.14.7 passed main CI and PyPI publication. Its six native runtimes and
signed manifest passed, but later VSIX verification exposed these harness issues.
Existing release tags and published distributions remain unchanged. Marketplace
promotion still requires the documented acceptance and signoff gates.

## 0.14.7 — VSIX dependency audit and sidebar rendering

- Update the VS Code extension's locked source-map-js dependency to 1.2.2,
  resolving GHSA-68fv-2mgg-jv7q and restoring the release audit gate.
- Avoid repainting unchanged permission controls and settings filters during
  conversation updates, reducing sidebar render cost on Intel macOS.
- Run extension CI on Intel macOS as well as Apple Silicon before tagging releases.
- Synchronize the queued-command overflow test with actual input receipt so
  loaded CI runners exercise the intended full-inbox behavior reliably.

This replacement preserves the published 0.14.6 source tag and distributions.
The VS Code 0.3.0 beta still requires candidate installation, acceptance, and
promotion gates before Marketplace publication.

## 0.14.6 — final VSIX attestation and startup validation

- Give each final VSIX dependency report an RFC 4122 document ID accepted by
  GitHub's attestation parser. Derive it from the exact package bytes so promotion
  can reproduce the signed report without changing its identity.
- Wait for startup runtime validation before reporting installed runtime evidence,
  so a fresh installation does not report the bundled CLI as temporarily unavailable.

All six 0.14.5 native runtimes and their assembled manifest passed signing, but
final VSIX attestation failed before installation testing. This replacement
preserves the existing tags; Marketplace pre-release 0.3.0 still requires the
documented installation, acceptance, and promotion gates.

## 0.14.5 — native managed-runtime packaging

- Pin the six bundled runtimes to checksum-verified CPython 3.12.15 distributions,
  including their observed OpenSSL and Expat versions in each dependency SBOM.
- Build Linux executables with shared-library-capable Python on the existing
  glibc 2.28 baseline, and build tested, static OpenSSL 3.5.9 for Windows ARM64
  and Intel macOS cryptography.
- Sign embedded macOS libraries during packaging and verify notarization using
  Apple's documented check for standalone code. Preserve Windows API paths in Git Bash.
- Add an unsigned native build and executable smoke check for all six targets.

This source version prepares a replacement candidate after the protected `v0.14.4`
native build failed. Marketplace pre-release 0.3.0 remains subject to the documented
signing, installation, acceptance, and promotion gates.

## 0.14.4 — IDE integrations and release tooling

- Add the VS Code 0.3.0 integration with a shared sidebar, provider management, bounded editor
  context, reviewed changes, recovery checkpoints, and native host actions.
- Add shared UI transport and a JetBrains development preview using the same Python IDE bridge.
- Isolate provider credentials, handle physical workspace paths, and keep unit tests portable.
- Add six-target managed-runtime packaging, Azure OIDC Windows signing verification, Apple
  notarization/provenance contracts, and protected evidence and Marketplace promotion workflows.
- Update pinned sandbox build inputs; fresh image scans remain required.

This is a CLI/source release. Production signed VSIX packages and Marketplace publication remain
pending the [documented signing and acceptance gates](vscode-marketplace-launch.md).

Notable user-facing changes to Alysis Code are recorded here. This project follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html) while the public API remains pre-1.0.

## [Unreleased]

## [0.14.3] - 2026-09-29

### Added

- Add Claude Sonnet 5.5 to the Alysis account model picker, using native Messages
  transport, streaming, signed reasoning and tool continuation, and the shared
  hosted credit allowance.

### Changed

- Default hosted accounts to automatic prompt caching. Migrate legacy Manual
  settings only when their other cache settings match the old defaults; preserve
  Off, customized settings, and subsequent explicit choices.
- Use five-minute cache controls for hosted Sonnet and provider-managed caching
  for hosted DeepSeek, GLM, and Luna. See the
  [caching guide](hosted-prompt-caching.md) for migration details and controls.

### Fixed

- Resolve cache previews and diagnostics using each model's actual protocol,
  preserve hosted credit-limit error details, and report Sonnet reasoning usage
  without counting it twice.
- Honor the configured HTTP MCP startup timeout for initialization acknowledgements.
  Report MCP startup errors once and exit cleanly without retrying through classic chat.

## [0.14.2] - 2026-09-28

### Fixed

- Show only supported reasoning efforts for hosted Luna and clear incompatible
  settings inherited from another model. Map saved or environment-provided
  `minimal` and `ultra` settings to `low` and `max`, preventing invalid-effort
  errors during the free-credit campaign.
- Preserve the hosted gateway's capacity retry guidance and account request-limit
  messages so users can distinguish temporary capacity from exhausted credits.
- Cancel working-tip link clicks when a fast mouse drag arrives before the next
  screen repaint.

## [0.14.1] - 2026-09-25

### Added

- Add GPT-6 Luna to the Alysis Code account model picker, with hosted Responses
  routing, configurable reasoning, and stateless function-tool continuation.
  Luna uses the account's shared credit allowance alongside the Flash models.

### Fixed

- Rebuild the correct chat and compactor transports when switching between hosted
  Luna and Flash models during a conversation, while preserving the session on a
  failed switch.
- Keep hosted web search as a function tool and show the gateway's capacity,
  request, and credit-limit reasons instead of a generic rate-limit message.

## [0.14.0] - 2026-09-24

### Added

- Add GPT-6 Sol and GPT-6 Luna to the OpenAI API and ChatGPT subscription model
  choices, with pricing, context limits, and reasoning settings. Subscription
  availability follows the live account catalog.
- Add Claude Opus 5.5 to Anthropic and OpenRouter, Grok 4.7 to xAI and OpenRouter,
  Mistral's hosted GLM 5.3, and current OpenRouter model routes.
- Show DeepSeek V4.1 Flash and GLM 5.3 Flash as hosted account model choices.
- Add `/objective` commands to inspect, replace, and amend the current task across
  retries and resumed sessions.
- Allow a configurable set of public package registries for `web_fetch` while
  retaining URL and network safety checks.

### Changed

- Show clearer model names in setup and move the request timeout control to
  Advanced settings.
- Improve subagent scheduling, task evidence, and verification reporting.

### Fixed

- Suppress unexecuted tool-call markup in replies, retry once with structured
  tool calls, and report a failure if the model repeats the malformed output.
- Require AnyIO 4.14.2 or newer in the HTTP runtime dependency set.
- Keep hosted account credentials on the selected gateway when an unsupported
  model is chosen, and preserve retired Xiaomi selections without silently
  redirecting them to a different provider's model.
- Omit unsupported temperature settings for GPT-6 reasoning requests and reject
  attempts to disable reasoning for always-thinking OpenRouter models.
- Improve session recovery and deadline handling for long running work.
- Verify durable service listener ownership on macOS without requiring root access.

## [0.13.8] - 2026-09-16

### Changed

- Remove legacy chat Plan Mode, its draft/approve flow, and its prompts and toolbar
  state. Use the Architect persona for planning in chat; Forge retains its plan views
  and editor.
- Replace `/mode` with `/permissions`. Permission selections and Shift+Tab apply to
  the next user message, with the pending choice visible beside the active permissions.
- Apply persona changes and approved persona-switch proposals only after the active
  turn finishes. Preserve pending permission selections when resuming a conversation.
- Show pending steering messages and queued follow-ups. Shift+Left recalls the newest
  queued message for editing. Interruptions preserve undelivered work and wait for the
  current worker to finish before another turn starts.
- Read large Git diffs in complete pages with a content identifier that detects
  changes between pages. The agent can select a path or inspect staged changes.
- Allow a background-process wait of up to 900 seconds, bounded by the remaining
  run budget and returning early when its requested condition is met.
- Explain how to correct rejected verification commands, including how to use the
  session's configured verification contract.

### Fixed

- Keep plain assistant replies in conversation history so follow-up requests retain
  the previous answer even when the provider returns no extra metadata.
- Redact credential fields inside nested JSON, tool arguments, feedback text, and
  support bundles. Omit data that exceeds the safe redaction depth and non-UTF-8
  diagnostic artifacts that cannot be sanitized reliably.
- Clean up incomplete support bundles after cancellation or I/O errors, support long
  Windows export paths, and open report drafts without disrupting terminal output.
- Inspect Git changes without running configured diff programs, text converters,
  filesystem monitors, or content filters. Worktree reads that require content
  filters or submodule helpers return guidance for an approved shell command;
  staged diffs and unaffected scoped paths remain available.
- Preserve hosted-account explanations for rolling usage limits, concurrent requests,
  reserved credits, and temporary routing or capacity problems.

## [0.13.7] - 2026-09-11

### Added

- Rotating usage tips above the chat input while the agent works, with clickable links.
  Tips hide during dialogs and stay out of conversation history.

### Changed

- The context footer shows remaining conversation space after startup prompts and tools.
  Provider measurements and calibrated estimates inform the gauge, while capacity warnings
  continue to use the full request budget.
- Ctrl+J is the multiline input shortcut; Enter submits. The Alt+Enter alias is removed.
- Updated public documentation and contributor guidance.

### Fixed

- Setup continuation hints stay visible above the footer on small terminals and long screens.
- Benchmark wheel builds preserve local build metadata and existing build artifacts, including
  when a build fails or is interrupted.

## [0.13.6.2] - 2026-09-08

### Fixed

- **Alysis Code account setup and configuration use subscription controls.**
  Setup identifies the Alysis Code endpoint without the retired MiMo label.
  Hosted profiles show Alysis Code subscription access and account sign-in,
  reconnect, and disconnect controls instead of an API-key editor.
- **Hosted model changes no longer ask for a base URL.** The managed endpoint
  stays in place through model selection and back navigation. Custom and BYOK
  providers keep their endpoint settings.
- **Switching to an Alysis Code account keeps the correct connection.** Signing
  in refreshes the full active profile before configuration is saved, preventing
  the previous provider's URL from overwriting the hosted endpoint. Login also
  switches back to the native agent from a selected delegated runtime. Account
  status reflects the Alysis login, including after logout when an unrelated
  provider key is still saved.

## [0.13.6.1] - 2026-09-08

### Fixed

- **V4.1 Flash beta on Alysis Code Pro.** The Pro model picker now includes
  `deepseek-v4.1-flash-expires-on-0910` and `deepseek-v4-flash-vision-exp`
  with descriptions. Newly discovered models are captioned "available on your
  Alysis Code Pro plan" instead of the retired MiMo "trial" wording.

## [0.13.6] - 2026-09-08

### Added

- **DeepSeek V4.1 Flash internal beta.** `deepseek-v4.1-flash-expires-on-0910`
  (announced 2026-09-08) is now on the DeepSeek preset menu. It is a
  new-architecture model with native multimodal input, served on the unchanged
  `https://api.deepseek.com` base URL under a temporary id that stops resolving
  on 2026-09-10; DeepSeek bills it at the `deepseek-v4-flash` rate and caps
  accounts at 20 concurrent requests. Registry metadata assumes the V4 Flash
  shape (1M context, 384K output, vision, thinking `low|high|max`) until
  DeepSeek publishes the GA id and limits. `deepseek-v4-pro` stays the default.

### Changed

- **DeepSeek footer labels follow vendor spelling.** Names now appear as
  `DeepSeek-V4-Pro` and `DeepSeek-V4.1-Flash`; dated beta ids omit their
  `-expires-on-<date>` suffix from the displayed label.

## [0.13.5] - 2026-09-07

### Added

- Ship eight bundled workflow skills: `address-pr-comments`, `code-review`,
  `commit`, `debug`, `fix-ci`, `release-notes`, `security-review`, and `skill-creator`.
- Select a relevant skill automatically with a bounded, reasoning-off model call;
  read its instructions before other task tools, with ordinary routing as fallback.
- Invoke skills with `$<name> [task]` at an idle chat prompt, with live completion
  and one-turn attachment. `/skill` remains supported.
- Add `bundled_skills_enabled` configuration, project and user override precedence,
  and model-in-loop bundled workflow evaluations with launch-readiness checks.

### Fixed

- Harden skill instruction boundaries, selection ordering, and per-turn cleanup.
- Preserve resolved skill settings in child agents and restore slash completion.
- Recognize silent and inline verification results and preserve authoritative
  verification evidence when supplemental checks run.
- Bound stalled OpenAI-compatible and Responses streams, including traffic that
  contains no meaningful progress.

## [0.13.4] - 2026-09-05

Model-catalog and TUI-access refresh.

### Added

- Added GPT-6 Astra as the default OpenAI model, with aliases, pricing, context-window,
  reasoning, OpenRouter, and ChatGPT-subscription metadata.
- Added newly available first-party and gateway routes for Claude Fable 5.1, Gemini 3.8
  Flash, Qwen 3.8, GLM-5.3, Kimi K3, Doubao Seed 2.1, DeepSeek V4 snapshots, Grok 4.6,
  and related hosted variants.

### Changed

- Refreshed every hosted preset's model picker, aliases, validation model, retirement
  mappings, pricing, context limits, and reasoning contracts against provider catalogs.
- Updated TUI model labels to preserve vendor naming and made profile pickers show each
  profile's model, host, active state, retired-model state, and duplicates.
- Migrated the Perplexity preset from the retiring Sonar Chat Completions endpoint to the
  Agent API, including hosted web search and saved-profile migration.
- Replaced the OpenAI economy and validation model with GPT-5.6 Luna and kept
  GPT-5.3 Codex limited to the Responses preset.

### Fixed

- OpenAI Chat Completions now uses `max_completion_tokens` for GPT-5 and newer models.
- Native Gemini tool and response schemas are projected onto Gemini's supported OpenAPI
  subset before requests are sent.

## [0.13.3] - 2026-08-28

### Fixed

- Restored the Ubuntu Python 3.11 and 3.12 CI jobs after the repository cleanup by aligning Ruff
  formatting, updating moved sandbox contract paths, preserving the security-documentation
  contract, making immediate shell-output coverage independent of thread scheduling, and allowing
  harmless floating-point clock precision in deadline coverage.

## [0.13.2] - 2026-08-28

Public launch and repository-layout refresh.

### Added

- A theme-adaptive README banner with dedicated light and dark artwork.
- `alysis-code` as the canonical executable, while retaining `alysis` as the short alias.

### Changed

- Simplified the public README around installation, core capabilities, modes, and documentation.
- Moved governance files under `.github/`, release and changelog documents under `docs/`, and
  sandbox and benchmark sources under `scripts/` for a cleaner repository root.
- Legacy Sylliptor environment and command compatibility now forwards silently while retaining
  diagnostic logging for support.

### Fixed

- Device-login approval URLs now remain pinned to the configured Alysis site instead of trusting
  a backend-provided origin.

## [0.13.1] - 2026-08-27

### Fixed

- Kept terminal input, selection, scrolling, paste hints, and theme behavior consistent across
  chat and configuration screens.
- Made intentional clean stops exit successfully while preserving non-zero exits for genuine
  failures and unknown stop reasons.
- Added bounded retries for connections interrupted during a response, with consistent deadline
  and diagnostic reporting.

## [0.13.0] - 2026-08-26

### Changed

- Renamed Sylliptor to Alysis Code. The package is now `alysis-code`, the command is `alysis`, and
  the Python module is `alysis_code`.
- Preserved the deprecated `sylliptor` command and migration support for existing configuration,
  credentials, plugins, and project directories. See the [migration guide](migration-alysis-code.md).
- Improved run-budget handling, deadline-aware shell waits, persistent services, edit diagnostics,
  sampling controls, and build provenance.

### Security

- Added redaction at log and telemetry write boundaries for credential-shaped values.

## [0.12.0] - 2026-08-18

### Added

- Added Code, Architect, Ask, and Debug personas.
- Added provider-neutral Terminal-Bench and Harbor adapters.
- Added deterministic release builds, dependency auditing, SBOM generation, and provenance
  attestations.

### Changed

- Improved evidence-based finalization, blocker reporting, provider authentication, model
  discovery, context management, and verification repair.
- Strengthened boundaries around credentials, protected paths, filesystem access, and sensitive
  tool output.

## [0.11.2] - 2026-07-27

### Added

- Added route arbitration, turn-contract enforcement, regression baselines, and evidence-backed
  completion checks.
- Added tracked process cleanup, workspace provisioning, incomplete-subagent containment, and new
  Moonshot/Kimi model presets.
- Added a master switch for web tools and search backends.

### Changed

- Refreshed the ChatGPT subscription model catalog and provider-reported final-answer handling.

## [0.11.1] - 2026-07-20

### Changed

- Redesigned the interactive configuration screen with grouped settings, clearer summaries, and
  improved provider and model selection.
- Added provider-specific reasoning controls and clearer per-role model overrides.

### Fixed

- Applied configuration changes made before the first message without restarting the terminal UI.
- Corrected input-budget calculations for shared-window models.

## [0.11.0] - 2026-07-18

### Added

- Added specialist subagent roles and capability-aware delegation.
- Added live subagent progress and result attribution to the terminal UI.
- Added an opt-in visual designer for image generation.

### Security

- Constrained child tools, execution modes, deadlines, and result handling so delegated output
  cannot change parent authority or permissions.

## [0.10.0] - 2026-07-13

### Added

- Added ChatGPT Codex subscription login, encrypted refreshable credentials, and live model
  discovery.
- Added DDGS web search as a keyless fallback and expanded hosted search support.
- Added repository mapping, focused test discovery, cached update prompts, and safer workspace
  previews.

### Changed

- Improved provider-aware prompt caching, usage accounting, terminal navigation, Forge planning,
  and OpenAI Responses streaming recovery.

## [0.9.8] - 2026-06-29

### Added

- Added stronger completion contracts and verification evidence for one-shot and managed tasks.
- Added shell waiting, durable service tools, and stricter web-fetch provenance.

### Changed

- Improved Forge verification, runtime deadlines, provider adapters, usage tracking, and recovery
  from unknown tool calls.

## Earlier releases

Release notes for versions before 0.9.8 remain available in the
[GitHub Releases archive](https://github.com/AlysisAi/AlysisCode/releases).

[Unreleased]: https://github.com/AlysisAi/AlysisCode/compare/v0.14.1...HEAD
[0.14.1]: https://github.com/AlysisAi/AlysisCode/compare/v0.14.0...v0.14.1
[0.14.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.8...v0.14.0
[0.13.8]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.7...v0.13.8
[0.13.7]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.6.2...v0.13.7
[0.13.6.2]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.6.1...v0.13.6.2
[0.13.6.1]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.6...v0.13.6.1
[0.13.6]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.5...v0.13.6
[0.13.5]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.4...v0.13.5
[0.13.4]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.3...v0.13.4
[0.13.3]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.2...v0.13.3
[0.13.2]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.1...v0.13.2
[0.13.1]: https://github.com/AlysisAi/AlysisCode/compare/v0.13.0...v0.13.1
[0.13.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.12.0...v0.13.0
[0.12.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.11.2...v0.12.0
[0.11.2]: https://github.com/AlysisAi/AlysisCode/compare/v0.11.1...v0.11.2
[0.11.1]: https://github.com/AlysisAi/AlysisCode/compare/v0.11.0...v0.11.1
[0.11.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.10.0...v0.11.0
[0.10.0]: https://github.com/AlysisAi/AlysisCode/compare/v0.9.8...v0.10.0
[0.9.8]: https://github.com/AlysisAi/AlysisCode/releases/tag/v0.9.8

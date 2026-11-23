# Owl scenarios: the free test tier

Owl turns replayed by a scripted model, so a change that breaks how owls behave is caught with
no model calls and nothing sent over the network.

- A **scenario** (`scenarios/*.json`) is pure data: the owl, what the person says and does, what
  the "model" answers on each call, fake results for anything that would reach outside (the web,
  a website, a Cloud computer), and checks.
- The **runner** (`run.py`) builds a fresh store in a temporary folder and drives the real owls
  runtime in-process (`BotRuntime`, wired as the owls service wires it: reply checker and action
  reviewer on). Every model call goes to the scripted model; tools, approvals, routines,
  check-backs, steering and the browser host code are the engine's own.
- **Checks** (`checks.py`) are pure functions on the evidence frozen after the run: tool calls
  in order with their arguments, replies published or withheld, cards and questions, events,
  routines, the model's requests, counts.

The tier runs in CI with the rest of the tests (`tests/test_owls_scenarios_*.py`); the whole set
takes a few seconds. `scripts/qa/owls_bench/` is the other end: real models over HTTP, at a cost.

## Run it

From the engine repository root, with the engine importable:

```sh
PYTHONPATH=src python3 -m scripts.qa.owls_scenarios.run                     # all
PYTHONPATH=src python3 -m scripts.qa.owls_scenarios.run --scenario ID       # one
PYTHONPATH=src python3 -m scripts.qa.owls_scenarios.run --evidence .agent-notes/scenarios
uv run pytest -q tests/test_owls_scenarios_tier.py                          # as CI does
```

It prints a short markdown report (one line per scenario, then each failed check with its
reason) and exits 1 when any scenario fails. `--evidence` writes what each run saw, as JSON, for
writing checks.

## The scenarios

| Scenario | What it pins |
|---|---|
| `routine-turned-on-while-writing-el` | A routine turned on while the owl writes is described as on, never as a draft |
| `cut-off-call-runs-nothing-el` | A reply cut off while writing a call runs none of it and shows none of it |
| `cut-offs-past-the-bound-zh` | Three cut-offs in a row end the turn plainly, nothing run or published |
| `same-failing-call-refused-es` | The same failing call is refused after 3 failures; ~10 attempts end the turn |
| `steer-during-tool-batch-el` | A message sent now is read at the next model round, after the whole tool batch |
| `website-payment-waits-el` | A step on a page with payment fields waits for the person (text-only model) |
| `website-sign-in-waits-zh` | Signing in waits for the person (model with image input) |
| `cloud-page-too-large-keeps-computer-it` | A Cloud page read too large to receive keeps the owl's computer |
| `check-back-with-condition-de` | A check-back with a condition ends the turn, is kept, and comes back once it holds |
| `quiet-routine-run-zh` | A routine run that found nothing new ends with no reply and nothing unread |

Each one fails when the guard it pins is reverted (checked by hand in a scratch copy of the
engine when the tier was written).

## Add a scenario

1. Copy the scenario closest to what you need and give it a new `id` (the file name, in
   kebab-case, ending with its language). Write `pins` in plain words: the behaviour, and where
   it came from (a changelog entry, a bug, a doc section).
2. `owl`, `settings` (`time_zone`), `library`, `memories` and `routines` set up what the owl
   already has. `model` declares the scripted model's capabilities (`id`, `image_input`,
   `context_window`, `max_output_tokens`); decisions in the engine come from these, never from
   the id.
3. `script` holds the replies per channel, in order (see `model.py`):

   ```json
   "script": {
     "owl": [
       {"call": [{"tool": "save_file", "arguments": {"name": "λίστα.txt", "content": "Νερό"}}]},
       {"cut_off": {"wire": "messages", "say": "…", "call": [{"tool": "save_file", "arguments": {}}]}},
       {"error": {"message": "LLM error 429: rate limited", "status": 429, "retry_after": 30}},
       {"call": [{"tool": "browser_click", "arguments": {
         "source_id": {"$result": "source_id"}, "node": {"$page_ref": "- button \"Πληρωμή\""}}}]},
       {"say": "Έτοιμο."}
     ],
     "reply_checker": "ready",
     "action_reviewer": [{"decision": "allow", "effect": "none"}]
   }
   ```

   The channel of a call is the engine module that made it. The reply checker answers "ready"
   unless you script its verdicts; every other channel must be scripted. A call with nothing
   left to answer it, a scripted reply never used, or a `during` action that never came makes
   the scenario fail: when the engine's behaviour moves, the script says so.
4. `fakes` stand in for the outside: `web` (`searches` by query, `pages` by address),
   `browser` (`pages`: each page's structure, as its accessibility roles, names and HTML
   attributes; see `fakes.PageBuilder`) and `cloud_computer` (a `browser` as above, plus
   `reply_too_large` rules for receipts the host cannot read). Without a fake, the web is off
   and no computer exists.
5. `steps` run in order: `send` (with an `id` to name the turn), `run` (every turn that can
   start runs to its end), `steer`, `turn_on_routine`, `pause_routine`, `cancel`,
   `run_routine`, `advance_clock`, `tick_check_backs`, `add_file`, `mark_read`. A `run` step
   takes `during` actions, each at `owl_call` N (while the model writes its N-th reply of that
   run) or `tool` N (just before the N-th tool of that run starts).
6. `checks` (see `checks.py`): `turn`, `reply`, `tools`, `tool_not_run`, `card`, `event`,
   `count`, `request`, `routine`, `path`. Give the important ones a `why`: it is printed when
   the check fails.
7. Run it, then **revert the fix it pins in a scratch copy and run it again: it must fail.** A
   scenario that passes either way pins nothing.

Rule #1 holds here too: a scenario is test data, so it may name a page, a word or a language,
but what it pins must be general. Prefer a non-English scenario, and a second one in another
script or with another model capability when the behaviour depends on either.

## Turn a real chat into a scenario

1. Export the chat from the owls API (the person's own service; never another person's data):
   `GET /v1/bots/{id}`, `GET /v1/chats/{id}` and `GET /v1/tasks/{id}` for each of its turns, in
   one JSON file as `{"owl": ..., "chat": ..., "tasks": [...]}` (one task, or a list of tasks,
   works too). Keep it outside the repository, for example in `.agent-notes/`.
2. Redact it:

   ```sh
   python3 -m scripts.qa.owls_scenarios.redact export.json --id my-new-case-el
   ```

   Ids, names, e-mail addresses, the hosts and paths of web addresses and all free text become
   stable placeholders (the same value always gets the same one). Each of the person's turns
   becomes a `send` and a `run`, its events become the scripted model's rounds (with the topic,
   file name and site the events kept), and its reply checks the checker's verdicts. A leak scan
   then reads the whole scenario for any original value, and for any address, e-mail or id that
   is not a placeholder; if one remains, **nothing is written** and it says where.
3. Finish it by hand: write `pins`, put realistic words back in the language of the original
   (never the original's own words), complete the calls' arguments (events keep only a topic, a
   file name or a site), add fakes for what it reads, and replace the starting checks (status,
   tools asked for, replied or not) with the ones that pin the bug. Then step 7 above.

## What is not covered

- The scripted model answers from its script, not from what it was shown (beyond `$result` and
  `$page_ref`). Prompt and tool-description changes are only seen as far as checks on the
  model's requests (`request`) look at them; the paid benchmark measures how real models react.
- Token counts use the engine's offline estimate (no tokenizer data is downloaded), so a
  scenario near a context limit can land differently from a computer that has the tokenizer.
- Websites are page structures, not real pages: no scripts, layout or network inside the page.
  Code that runs inside the owl's computer (guest desktop, managed browser) is not exercised;
  the Cloud computer fake answers the worker's protocol on the host side.
- Background, coding and peer work start only as far as a turn's own steps reach them.

# Owls benchmark

A stopwatch for owls. It gives an owl real tasks and scores each one end to end:

- **Did it deliver?** A file of the expected kind exists and opens (or, for website
  tasks, the answer is in the reply).
- **Is it right?** Deterministic checks on the files and the reply, then an optional
  model judge.
- **What did it cost?** Wall time, model calls, tokens and cost (when the provider
  reports them), approvals asked, questions asked and failures.

It can run the same tasks through another app's headless command line for comparison.

Everything uses the Python standard library (3.11 or later). Run the commands from the
engine repository root. The benchmark never imports the engine; it talks to the owls
service over HTTP.

## Contents

| Path | What it is |
|---|---|
| `tasks/*.json` | 24 tasks in 8 categories, 3 each, split into dev and held-out halves |
| `tasks/attachments/` | Small synthetic files the data tasks attach (CSV and XLSX, with non-ASCII text) |
| `judge/` | The judge's prompt files and `manifest.json`, the sha256 of each |
| `run.py` | Runs tasks against an owls service and/or a reference command, then scores and reports |
| `score.py` | Deterministic checks and the judge; re-scores an output folder |
| `report.py` | `report.json` and `report.md`; drift from a baseline report |
| `tasks.py` | The task schema check (`python3 -m scripts.qa.owls_bench.tasks`) |
| `documents.py`, `language.py` | Readers for xlsx, docx, pptx, PDF, CSV and text; the reply script check |
| `owls_client.py` | The owls HTTP client |
| `builders.py` | Minimal Office and PDF writers for the attachments and the offline tests |

The categories are `research_to_spreadsheet`, `comparison_to_document`, `deck`,
`website_task`, `price_check`, `data_file`, `long_job` (20 or more pages or sources) and
`repeat_job` (the same job twice in one owl; the second run is the one scored, and the
report compares its time with the first). Two thirds of the tasks are not in English
(Greek, Spanish, German, Chinese, French, Italian, Portuguese). No task needs an account,
a payment or a sign-in; website tasks say to stop before any of them and never to enter
personal data.

## Before you run

- Write results to a gitignored folder, for example `.agent-notes/owls-bench/<name>/`.
  Records hold transcripts, delivered files and website content: never commit them.
- Every owls run spends real model calls. The default is the dev split, 3 runs per task
  (12 tasks, 36 runs, plus the repeat jobs' second runs). For a first check use
  `--only <task-id> --runs 1`.
- Check the task files and the judge prompts:

  ```sh
  python3 -m scripts.qa.owls_bench.tasks
  python3 -m scripts.qa.owls_bench.judge --check
  ```

## Run against an owls service on a computer

The runner uses the owls service's HTTP API with the service's bearer token. Where the owls
run (this computer or the Cloud) is decided by the service you point it at: each place has
its own service. For each task and run the runner:

1. creates a new owl (`POST /v1/bots`, with `model` when you pass `--owls-model`) and its
   conversation (`POST /v1/bots/{id}/chats`);
2. puts the task's attachments in the owl's Library (`POST /v1/bots/{id}/files/binary`, or
   chunked `.../uploads` above 2 MiB) and sends the prompt with them
   (`POST /v1/chats/{id}/tasks` with `file_ids`);
3. reads the turn (`GET /v1/tasks/{id}`: status, events, model usage) until it ends. If it
   waits on a card or a question (`GET /v1/needs-you`), the runner records the card,
   never answers it, and cancels the turn (`POST /v1/tasks/{id}/cancel`). A turn that runs
   past the timeout is cancelled too;
4. downloads every Library file the turn saved (`POST /v1/bots/{id}/files/page` with
   `source_task_id`, then `.../download` or `.../chunks/{offset}`) and the conversation
   (`GET /v1/chats/{id}`).

Start a separate local owls service with its own data folder, so benchmark owls never mix
with your own. It reads one JSON line on standard input and stops when that input closes:

```sh
export ALYSIS_OWLS_BENCH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
DATA="$PWD/.agent-notes/owls-bench/service"
mkdir -p "$DATA"
{ printf '{"directory": "%s", "token": "%s"}\n' "$DATA" "$ALYSIS_OWLS_BENCH_TOKEN"; cat; } \
  | alysis bots serve
```

It prints `{"type": "ready", "url": "http://127.0.0.1:<port>", ...}`; keep it open in its
own terminal (Ctrl+D or Ctrl+C stops it). The bootstrap line also takes:

- `"profile": "<name>"`: the saved Alysis model connection the owls use (default: the
  active one). Other saved connections are offered per owl (`GET /v1/models`).
- `"computer_bundle": "<absolute folder>"`: an unpacked owl computer image (the folder
  with its `manifest.json`, built by `scripts/bots-local/build_windows_image.py` or
  `build_macos_image.py`). This gives each owl its private computer and browser, which
  the website tasks need (without it they measure how an owl copes with no browser).
  Each computer reserves about 3 GB of RAM;
  `"resources": {"cpu_budget": 4, "memory_mib": 8192}` sets the budget (default: half of
  this machine).

Then run the benchmark in another terminal:

```sh
python3 -m scripts.qa.owls_bench.run \
  --owls-url http://127.0.0.1:<port> \
  --out .agent-notes/owls-bench/2026-11-19-dev
```

Useful options: `--owls-model <id>` picks the owl's model from the ids the service lists
at `GET /v1/models` (the report records that model's declared `image_input` and
`context_window`); `--split dev|heldout|all`; `--only id,id`; `--runs N`;
`--timeout <seconds>` (overrides each task's `timeout_s`, default 1800);
`--delete-owls` removes each benchmark owl after its run; `--resume` continues an
interrupted benchmark in the same `--out`. The token can also be given with
`--owls-token`, but the environment variable keeps it out of your shell history and
process list. `ALYSIS_OWLS_BENCH_URL` can replace `--owls-url`.

## Run against the reference app's headless command

Give any app's non-interactive command as a template. The runner fills in:

| Placeholder | Value |
|---|---|
| `{prompt}` | the task text, exactly (required) |
| `{out}` | an empty folder for the files it should deliver |
| `{task_id}` | the task id |
| `{inputs}` | a folder holding copies of the task's attachments; it is also the working folder |

```sh
python3 -m scripts.qa.owls_bench.run \
  --reference-command 'refapp run --non-interactive --output-dir {out} {prompt}' \
  --reference-label 'Reference app 1.2' \
  --out .agent-notes/owls-bench/2026-11-19-reference
```

The template is split into words once, then each placeholder is replaced inside its own
word: the prompt always arrives as one argument and is never read by a shell, whatever it
contains. On Windows, or for paths with backslashes, give the command as a JSON list:
`--reference-command '["C:/Tools/refapp.exe", "run", "--output-dir", "{out}", "{prompt}"]'`.

The command's standard output is taken as its final reply, exit code 0 as finished, and
the files it writes to `{out}` as its deliverables. Its standard error is saved beside the
record. Your environment is passed through, so the app uses its own sign-in. Model calls,
tokens and cost show as "–" because the command does not report them.

Give both `--owls-url` and `--reference-command` to run the same tasks through both, one
after the other for each task and run, so time-sensitive data (prices, timetables) is as
close as possible. The report shows each side separately.

## The judge (optional)

Deterministic checks always run first. The judge then grades each run against the common
rubric items and its category's items in `judge/rubric.json`, from the task, the final
reply, the extracted text of the delivered files (bounded) and a summary of the receipts.
Use any OpenAI-compatible chat endpoint and model you choose:

```sh
export ALYSIS_OWLS_BENCH_JUDGE_URL=https://<endpoint>/v1
export ALYSIS_OWLS_BENCH_JUDGE_KEY=<key>
export ALYSIS_OWLS_BENCH_JUDGE_MODEL=<model id>
python3 -m scripts.qa.owls_bench.score --out .agent-notes/owls-bench/2026-11-19-dev
```

The same variables, or `--judge-url`, `--judge-key` and `--judge-model`, work on `run`.
`--judge-no-temperature` and `--judge-json-mode` adapt to endpoints that reject or support
those options. A failed judge call is recorded as an error, never as a score.

The scorer refuses to judge if any file in `judge/` differs from `judge/manifest.json`.
After a reviewed change to a judge prompt, record it with
`python3 -m scripts.qa.owls_bench.judge --write-manifest` and commit both. Reports carry
the prompt digest, and drift says so when two reports used different judge prompts.

## Reading the results

`<out>/report.md` and `<out>/report.json` group results by system, then by split (dev and
held-out are never pooled), then by category and task:

- **Finished**: the turn completed (owls) or the command exited 0 (reference).
- **Checks all pass**: runs where every deterministic check passed. **Checks passed**: the
  share of individual checks passed.
- **Judge**: mean rubric score from 0 to 100%.
- **Wall s, Model calls, Tokens, Cost**: medians over runs; tokens and cost only count runs
  whose source reported them.
- **Approvals, Questions, Failures**: totals (approval cards, questions the owl asked, failed
  tool steps and failed turns).

The deterministic checks, by id: `finished`, `deliverable_opens`, `reply_present`,
`min_rows` (rows under the header in the main table), `columns` (header width; what the
columns hold is left to the judge), `slides`, `min_pages`, `min_items` (list items or
table rows in a reply), `links_are_web` (every link is http or https), `min_sources`
(distinct pages, or distinct sites for tasks that compare shops), `totals_reconcile` (a
numeric column's total equals the sum above it, within half a unit of its last decimal
place; a `SUM` formula is evaluated), `value_N` (a value known from the attached data
appears), `chart`, `repeat_completed` and `reply_script`. The script check compares the
reply's dominant Unicode script with the expected language's script; when other languages
share that script (Spanish and English both use Latin), it says so and leaves the language
itself to the judge.

Each run folder, `<out>/runs/<owls|reference>/<task id>/run-<n>/`, holds `record.json`
(what happened), `score.json` (the checks and the judge), the delivered files under
`files/attempt-<k>/`, and the owl's task records and conversation or the command's output.

Compare with an earlier run:

```sh
python3 -m scripts.qa.owls_bench.report --out .agent-notes/owls-bench/2026-11-19-dev \
  --baseline .agent-notes/owls-bench/2026-11-12-dev/report.json
```

## Keeping the held-out half untouched

The held-out half tells you whether a change works beyond the tasks you looked at. It
only does that while nobody tunes on it:

- Look at dev results, transcripts and files to find and fix problems. Run held-out only
  to evaluate a finished change (`--split heldout`), and read only its report, not its
  transcripts.
- Never tune prompts, tool descriptions or product behaviour on held-out results. If you
  had to study a held-out run in detail, move that task to dev and write a new held-out
  task for the category, in another language and on another site.
- Bench tasks never go into product code. No task text, site, product name, expected
  value or attachment content may be copied into the engine, the app, prompts or tool
  descriptions. Fixes must be general: they must work for every site, language and model.
- Changing a task changes its hash. Reports record each task's hash, and drift lists the
  tasks that changed between two reports, so their numbers are not compared blindly.

## Tests

```sh
python3 -m pytest -q tests/test_owls_bench_*.py
```

The tests run offline, without a model: they build good and broken files in the test,
check the shipped task set, the judge manifest, report aggregation, the reference command
substitution, and the owls flow against an in-memory stand-in for the service.

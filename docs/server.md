# Server Mode

## Overview

`alysis server start` exposes an HTTP API for creating run workspaces and executing jobs in an
outer worker sandbox.

- Run workspace mount inside worker: `/workspace`
- Job artifact mount inside worker: `/alysis_job`
- Worker config/data paths inside sandbox:
  - `ALYSIS_CONFIG_DIR=/alysis_job/config`
  - `ALYSIS_DATA_DIR=/alysis_job/data`

## API Mode Support

Server endpoints expose modes as follows:

- `POST /v1/runs/{run_id}/jobs/run`: `readonly|review|auto|fullaccess`

`fullaccess` disables mode-level approval/guard prompts in the inner agent runtime, but jobs still
execute inside the configured outer worker sandbox backend (`bwrap`/`docker`) and its policy.

## Authentication And Locality Policy

Protected routes use `ALYSIS_SERVER_TOKEN` as follows:

- If `ALYSIS_SERVER_TOKEN` is set, requests must include
  `Authorization: Bearer <token>`.
  - Missing Bearer token: `401`
  - Wrong token: `403`
- If `ALYSIS_SERVER_TOKEN` is unset, protected routes only allow localhost clients
  (`127.0.0.1`, `::1`, `localhost`).
  - Non-localhost clients are rejected with `403`.

`/health` remains public.

## Start The Server

Server mode uses the same package runtime baseline as the CLI: Python 3.11 or newer.

Install server dependencies:

```bash
python -m pip install "alysis-code[server]"
```

Start:

```bash
alysis server start --host 127.0.0.1 --port 7070
```

Minimal authenticated API example:

```bash
export ALYSIS_SERVER_TOKEN="your-token"

RUN_ID=$(curl -sS -X POST \
  -H "Authorization: Bearer $ALYSIS_SERVER_TOKEN" \
  http://127.0.0.1:7070/v1/runs/empty | python -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')

curl -sS -X POST \
  -H "Authorization: Bearer $ALYSIS_SERVER_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"instruction":"Implement the task","mode":"fullaccess"}' \
  "http://127.0.0.1:7070/v1/runs/${RUN_ID}/jobs/run"
```

Optional data directory override:

```bash
alysis server start --data-dir /var/lib/alysis-server
```

`ALYSIS_SERVER_MAX_JOBS` controls the server worker-pool size. It bounds both:

- how many jobs may execute in worker subprocesses at the same time
- how many server worker threads are created to service queued jobs

## Upload Limits

`POST /v1/runs` stages uploaded ZIP archives to a temporary file before import.

- `ALYSIS_SERVER_MAX_UPLOAD_BYTES` is enforced while the multipart body is being read in chunks.
- Oversized uploads are rejected as soon as the configured limit is crossed; the server does not
  buffer the entire archive into a single Python `bytes` object first.
- The staged temporary ZIP is removed after the request completes.
- Existing ZIP validation still applies after staging succeeds, including bad-ZIP rejection, path
  sanitization, and the uncompressed-size guard during extraction.

## Job Status

A job's status comes from its worker process: exit code `0` is `succeeded`, any other exit
code is `failed`, and a cancelled job is `cancelled`. `GET /v1/jobs/{job_id}` reports
`status_source: "exit_code"` and the `exit_code` it was decided from.

## Job Queue And Cancellation

Jobs move through `queued -> running -> succeeded|failed|cancelled`.

- `start_job()` enqueues work onto a fixed worker pool instead of creating one new thread per job.
- When all worker slots are busy, additional jobs remain `queued` without spawning extra worker
  threads or subprocesses.
- Queued jobs cancelled via `POST /v1/jobs/{job_id}/cancel` become terminal `cancelled`
  immediately and are skipped by workers.
- Cancelling a running job requests process termination when possible; once the worker finishes
  teardown, the job becomes terminal `cancelled`.
- Job metadata stays under the per-job directory while queued/running, and `result.json` is
  written when the job reaches a terminal state.
- A cancelled job keeps `status_source: "exit_code"`: cancellation is decided by the server,
  not by anything the job reported.

## Worker Backends

`ALYSIS_SERVER_WORKER_BACKEND` selects the outer worker sandbox backend:

- `bwrap` on Linux by default
- `docker` on macOS/Windows by default

Worker sandbox mode default depends on backend when
`ALYSIS_SERVER_WORKER_SANDBOX_MODE` is unset:

- `strict` for `bwrap`
- `warn` for `docker`

Inside workers, nested tool sandbox defaults are hardened:

- `ALYSIS_SHELL_SANDBOX_BACKEND=bwrap`
- `ALYSIS_SHELL_SANDBOX_BWRAP_PROFILE=hardened`
- `ALYSIS_SHELL_SANDBOX_NETWORK=off`
- `ALYSIS_SHELL_SANDBOX_CLEAR_ENV=1`
- `ALYSIS_SHELL_SANDBOX_PROTECT_REPO_META=1`

## Model And Base URL Policy

Security defaults are server-operator first:

- Client `base_url` override is disabled by default
  (`ALYSIS_SERVER_ALLOW_CLIENT_BASE_URL=0`).
- If client `base_url` override is enabled, client-provided URLs must use
  `http://` or `https://`.
- Client model override is enabled by default
  (`ALYSIS_SERVER_ALLOW_CLIENT_MODEL=1`).
- Set fixed defaults with:
  - `ALYSIS_SERVER_MODEL`
  - `ALYSIS_SERVER_BASE_URL`

`ALYSIS_SERVER_BASE_URL` must be an `http://` or `https://` URL.

## Docker Worker Image

By default, docker server workers use `ghcr.io/alysisai/alysis-sandbox:server`.

Build the server worker image:

```bash
docker build --build-arg VARIANT=server -t alysis-sandbox:server -f scripts/sandbox/Dockerfile .
```

Override image tag used by server workers:

```bash
export ALYSIS_SERVER_DOCKER_IMAGE=alysis-sandbox:server
```

The image should include:

- `alysis-code` installed as a Python package
- `git`
- `ripgrep`
- `ca-certificates`
- `bubblewrap` (recommended; best-effort inner sandbox support)

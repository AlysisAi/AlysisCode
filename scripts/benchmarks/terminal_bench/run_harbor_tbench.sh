#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../../.." && pwd)"
cd "$REPO_ROOT"
export PYTHONPATH="$REPO_ROOT:$REPO_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"

usage() {
  cat <<'EOF_USAGE'
Usage: run_harbor_tbench.sh [options]

  --latest-main                 Label this campaign as running latest main. The
                                adapter then refuses to start unless the wheel
                                was built from the commit origin/main resolved
                                to at build time.
  --provenance-override REASON  Proceed even though the provenance check failed,
                                recording REASON verbatim in every artifact this
                                run produces. REASON is required and must not be
                                blank -- an override that records nothing about
                                why it was taken reproduces the failure the
                                check exists to prevent.
  --no-provenance-guard         Disable the check outright. Prefer
                                --provenance-override: this leaves no reason
                                behind.
  -h, --help                    Show this message.
EOF_USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --latest-main)
      export ALYSIS_TBENCH_LATEST_MAIN=1
      shift
      ;;
    --provenance-override)
      if [[ -z "${2:-}" || -z "${2// /}" ]]; then
        echo "--provenance-override requires a non-blank REASON." >&2
        exit 2
      fi
      export ALYSIS_PROVENANCE_OVERRIDE="$2"
      shift 2
      ;;
    --provenance-override=*)
      ALYSIS_PROVENANCE_OVERRIDE="${1#*=}"
      if [[ -z "${ALYSIS_PROVENANCE_OVERRIDE// /}" ]]; then
        echo "--provenance-override requires a non-blank REASON." >&2
        exit 2
      fi
      export ALYSIS_PROVENANCE_OVERRIDE
      shift
      ;;
    --no-provenance-guard)
      export ALYSIS_PROVENANCE_GUARD=0
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

load_stored_alysis_key() {
  local py_bin
  py_bin="$(command -v python3 || command -v python || true)"
  if [[ -z "$py_bin" ]]; then
    return 0
  fi

  PYTHONPATH="$REPO_ROOT/src:$REPO_ROOT" "$py_bin" - <<'PY' 2>/dev/null || true
from alysis_code.config import load_config, resolve_profile_api_key

try:
    key = resolve_profile_api_key(load_config(), "alysis").key or ""
except Exception:
    key = ""

if key:
    print(key)
PY
}

if [[ -z "${ALYSIS_API_KEY:-${OPENROUTER_API_KEY:-${DASHSCOPE_API_KEY:-${OPENAI_API_KEY:-}}}}" ]]; then
  stored_alysis_key="$(load_stored_alysis_key)"
  if [[ -n "$stored_alysis_key" ]]; then
    export ALYSIS_API_KEY="$stored_alysis_key"
  fi
fi

if [[ -z "${ALYSIS_API_KEY:-${OPENROUTER_API_KEY:-${DASHSCOPE_API_KEY:-${OPENAI_API_KEY:-}}}}" ]]; then
  echo "Set ALYSIS_API_KEY, OPENROUTER_API_KEY, DASHSCOPE_API_KEY, or OPENAI_API_KEY before running, or log in to the alysis profile first." >&2
  exit 2
fi

detect_concurrency() {
  if command -v nproc >/dev/null 2>&1; then
    nproc
  else
    echo 4
  fi
}

RUN_ID="${RUN_ID:-alysis-tbench21-$(date -u +%Y%m%d-%H%M%S)}"
TB_WORK_ROOT="${TB_WORK_ROOT:-$HOME/alysis-tbench}"
TB_DATASET="${TB_DATASET:-terminal-bench/terminal-bench-2-1}"
TB_N_ATTEMPTS="${TB_N_ATTEMPTS:-1}"
TB_N_CONCURRENT="${TB_N_CONCURRENT:-$(detect_concurrency)}"
TB_AGENT_TIMEOUT_SEC="${TB_AGENT_TIMEOUT_SEC:-7200}"
TB_TIMEOUT_MULTIPLIER="${TB_TIMEOUT_MULTIPLIER:-4}"
TB_AGENT_TIMEOUT_MULTIPLIER="${TB_AGENT_TIMEOUT_MULTIPLIER:-4}"
TB_VERIFIER_TIMEOUT_MULTIPLIER="${TB_VERIFIER_TIMEOUT_MULTIPLIER:-10}"

if [[ -z "${ALYSIS_BASE_URL:-}" ]]; then
  echo "Set ALYSIS_BASE_URL to an OpenAI-compatible endpoint before running." >&2
  exit 2
fi
if [[ -z "${ALYSIS_MODEL:-}" ]]; then
  echo "Set ALYSIS_MODEL to the model id to benchmark before running." >&2
  exit 2
fi
export ALYSIS_BASE_URL ALYSIS_MODEL
export ALYSIS_MAX_STEPS="${ALYSIS_MAX_STEPS:-1000}"
export ALYSIS_TEMPERATURE="${ALYSIS_TEMPERATURE:-0.2}"
export ALYSIS_LLM_TIMEOUT_S="${ALYSIS_LLM_TIMEOUT_S:-240}"
export ALYSIS_INSTALL_SPEC="${ALYSIS_INSTALL_SPEC:-alysis-code}"
export ALYSIS_SUBAGENTS="${ALYSIS_SUBAGENTS:-true}"
export ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC="${ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC:-120}"
export ALYSIS_TBENCH_COMMAND_TIMEOUT_SEC="${ALYSIS_TBENCH_COMMAND_TIMEOUT_SEC:-$TB_AGENT_TIMEOUT_SEC}"
export ALYSIS_TBENCH_WEB_SEARCH_MODE="${ALYSIS_TBENCH_WEB_SEARCH_MODE:-off}"

LOG_DIR="$TB_WORK_ROOT/logs"
NOTES_DIR="$TB_WORK_ROOT/notes"
JOBS_DIR="$TB_WORK_ROOT/artifacts/harbor-jobs"
mkdir -p "$LOG_DIR" "$NOTES_DIR" "$JOBS_DIR"

RUN_NOTES="$NOTES_DIR/$RUN_ID-run-notes.md"
LOG_FILE="$LOG_DIR/$RUN_ID.log"

cat >"$RUN_NOTES" <<EOF_NOTES
# Terminal-Bench 2.1 run

Run ID: $RUN_ID
Started: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
Repo: $REPO_ROOT
Dataset: $TB_DATASET
Agent: scripts.benchmarks.terminal_bench.harbor_agent:AlysisHarborAgent
Model: $ALYSIS_MODEL
Base URL: $ALYSIS_BASE_URL
Sandbox: Alysis Code shell/verify sandbox off; Harbor/Docker task isolation still active
Attempts: $TB_N_ATTEMPTS
Concurrency: $TB_N_CONCURRENT
Max steps: $ALYSIS_MAX_STEPS
Subagents: $ALYSIS_SUBAGENTS
Agent timeout seconds: $TB_AGENT_TIMEOUT_SEC
Latest-main claim: ${ALYSIS_TBENCH_LATEST_MAIN:-0}
Provenance guard: ${ALYSIS_PROVENANCE_GUARD:-1}
Provenance override: ${ALYSIS_PROVENANCE_OVERRIDE:-(none)}

## Live paths

- Log: $LOG_FILE
- Harbor jobs: $JOBS_DIR/$RUN_ID
- Notes: $RUN_NOTES
EOF_NOTES

if [[ -n "${HARBOR_BIN:-}" ]]; then
  read -r -a harbor_cmd <<<"$HARBOR_BIN"
elif command -v harbor >/dev/null 2>&1; then
  harbor_cmd=(harbor)
else
  harbor_cmd=(uvx harbor)
fi

cmd=(
  "${harbor_cmd[@]}" run
  -d "$TB_DATASET"
  --agent-import-path scripts.benchmarks.terminal_bench.harbor_agent:AlysisHarborAgent
  -m "$ALYSIS_MODEL"
  --jobs-dir "$JOBS_DIR"
  --job-name "$RUN_ID"
  --n-attempts "$TB_N_ATTEMPTS"
  --n-concurrent "$TB_N_CONCURRENT"
  --timeout-multiplier "$TB_TIMEOUT_MULTIPLIER"
  --agent-timeout-multiplier "$TB_AGENT_TIMEOUT_MULTIPLIER"
  --verifier-timeout-multiplier "$TB_VERIFIER_TIMEOUT_MULTIPLIER"
  --agent-kwarg "max_steps=$ALYSIS_MAX_STEPS"
  --agent-kwarg "temperature=$ALYSIS_TEMPERATURE"
  --agent-kwarg "llm_timeout_s=$ALYSIS_LLM_TIMEOUT_S"
  --agent-kwarg "command_timeout_sec=$ALYSIS_TBENCH_COMMAND_TIMEOUT_SEC"
  --agent-kwarg "shutdown_reserve_sec=$ALYSIS_MANAGED_HOST_SHUTDOWN_RESERVE_SEC"
  --agent-kwarg "base_url=$ALYSIS_BASE_URL"
  --agent-kwarg "install_spec=$ALYSIS_INSTALL_SPEC"
  --agent-kwarg "subagents=$ALYSIS_SUBAGENTS"
  --agent-kwarg "tbench_web_search_mode=$ALYSIS_TBENCH_WEB_SEARCH_MODE"
  --artifact /app/.alysis
  --agent-include-logs "**/*"
  --yes
)

if [[ -n "${TB_TASK:-}" ]]; then
  cmd+=(--task "$TB_TASK")
fi
if [[ -n "${TB_INCLUDE_TASK:-}" ]]; then
  cmd+=(--include-task-name "$TB_INCLUDE_TASK")
fi
if [[ -n "${TB_EXCLUDE_TASK:-}" ]]; then
  cmd+=(--exclude-task-name "$TB_EXCLUDE_TASK")
fi
if [[ -n "${TB_N_TASKS:-}" ]]; then
  cmd+=(--n-tasks "$TB_N_TASKS")
fi

set +e
"${cmd[@]}" 2>&1 | tee "$LOG_FILE"
status="${PIPESTATUS[0]}"
set -e

{
  echo
  echo "## Finished"
  echo
  echo "- Finished: $(date -u +"%Y-%m-%dT%H:%M:%SZ")"
  echo "- Exit code: $status"
} >>"$RUN_NOTES"

exit "$status"

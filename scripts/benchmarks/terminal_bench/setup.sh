#!/bin/sh
#
# Installs the Alysis Code wheel into a Terminal-Bench task container.
#
# Where the artifacts of a run end up
# -----------------------------------
# Install logs go to /logs/agent/setup and /logs/artifacts/setup (below).
# The agent's own session logs go to /logs/artifacts/alysis-session, set by
# the adapter as `session_log_dir`.
#
# The harness harvests a task's artifacts from <workspace>/.alysis -- that is
# what `--artifact /app/.alysis` collects. Nothing in normal mode ever creates
# that directory, so two full 89-task trials retained ZERO session logs and
# every question afterwards had to be answered from console text.
#
# The adapter therefore appends a symlink to its run command, created only
# AFTER the agent exits: <workspace>/.alysis -> /logs/artifacts/alysis-session.
# Doing it post-run rather than pointing session_log_dir at the workspace is
# deliberate -- Terminal-Bench grades the workspace, agents routinely run
# `git add -A`, and a directory that materialises mid-run can be committed
# into the solution or trip a tree-state verifier. Set
# ALYSIS_TBENCH_MIRROR_SESSION=0 on the host to suppress it; default is on.

SETUP_LOG_DIR="${ALYSIS_SETUP_LOG_DIR:-/logs/agent/setup}"
SETUP_ARTIFACT_DIR="${ALYSIS_SETUP_ARTIFACT_DIR:-/logs/artifacts/setup}"
SETUP_LOG="$SETUP_LOG_DIR/install.log"
mkdir -p "$SETUP_LOG_DIR" "$SETUP_ARTIFACT_DIR"
if [ "${ALYSIS_SETUP_LOG_ACTIVE:-0}" != "1" ]; then
  export ALYSIS_SETUP_LOG_ACTIVE=1
  status_file="${TMPDIR:-/tmp}/alysis-setup-status.$$"
  set +e
  (
    "$0" "$@"
    status="$?"
    printf '%s\n' "$status" >"$status_file"
    exit "$status"
  ) 2>&1 | tee -a "$SETUP_LOG" "$SETUP_ARTIFACT_DIR/install.log"
  if [ -f "$status_file" ]; then
    status="$(cat "$status_file")"
    rm -f "$status_file"
  else
    status=1
  fi
  exit "$status"
fi

set -eu

export DEBIAN_FRONTEND=noninteractive
export PATH="$HOME/.local/bin:/opt/alysis-venv/bin:$PATH"

retry() {
  label="$1"
  shift
  for attempt in 1 2 3; do
    echo "setup_step label=$label attempt=$attempt started_at=$(date -u +%FT%TZ)"
    if "$@"; then
      echo "setup_step label=$label attempt=$attempt status=ok"
      return 0
    fi
    status="$?"
    echo "setup_step label=$label attempt=$attempt status=failed exit_code=$status"
    if [ "$attempt" -ge 3 ]; then
      return "$status"
    fi
    sleep $((attempt * 10))
  done
}

bootstrap_system_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    retry apt-get-update apt-get update
    retry apt-get-install apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      git \
      python3 \
      python3-pip \
      python3-venv
  elif command -v apk >/dev/null 2>&1; then
    retry apk-add apk add --no-cache \
      bash \
      ca-certificates \
      curl \
      git \
      python3 \
      py3-pip
  elif command -v dnf >/dev/null 2>&1; then
    retry dnf-install dnf install -y \
      ca-certificates \
      curl \
      git \
      python3 \
      python3-pip
  elif command -v yum >/dev/null 2>&1; then
    retry yum-install yum install -y \
      ca-certificates \
      curl \
      git \
      python3 \
      python3-pip
  else
    echo "setup_warning no_supported_package_manager_found"
  fi
}

python_is_311_or_newer() {
  python3 - <<'PY'
import sys

raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
}

install_target() {
  if [ -n "${ALYSIS_WHEEL:-}" ] && [ -f "$ALYSIS_WHEEL" ]; then
    printf '%s\n' "$ALYSIS_WHEEL"
  elif [ -f /installed-agent/alysis-source/pyproject.toml ]; then
    printf '%s\n' /installed-agent/alysis-source
  else
    printf '%s\n' "${ALYSIS_INSTALL_SPEC:-alysis-code}"
  fi
}

install_uv() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi
  retry uv-installer sh -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
  export PATH="$HOME/.local/bin:$PATH"
  command -v uv >/dev/null 2>&1
}

install_with_python_venv() {
  target="$1"
  rm -rf /opt/alysis-venv
  retry python-venv python3 -m venv /opt/alysis-venv || return 1
  retry pip-upgrade /opt/alysis-venv/bin/python -m pip install --no-input --upgrade pip || return 1
  retry pip-install-alysis /opt/alysis-venv/bin/python -m pip install --no-input "$target"
}

install_with_uv_python() {
  target="$1"
  install_uv
  retry uv-python-install uv python install 3.12
  rm -rf /opt/alysis-venv
  retry uv-venv uv venv --python 3.12 /opt/alysis-venv
  retry uv-pip-install uv pip install --python /opt/alysis-venv/bin/python "$target"
}

configure_alysis() {
  # Note: the wall-clock run budget is NOT configured here. It is read at run
  # time from ALYSIS_RUN_BUDGET_SECONDS (with ALYSIS_BUDGET_GRACE_SECONDS
  # and ALYSIS_BUDGET_CHECKPOINT_FRACTION), which box_harbor_agent.py
  # forwards from the host into the run environment. Set them on the host to
  # match the harness allowance; unset, a run is capped at the built-in 3600s.
  alysis config set web_search_mode "${ALYSIS_TBENCH_WEB_SEARCH_MODE:-off}" || true
  alysis config set default_mode fullaccess || true
  alysis config set stream false || true
  python - <<'PY'
from alysis_code.config import load_config, save_config
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.sandbox_settings import apply_sandbox_mode_to_config
import os

cfg = load_config()
profile = ProfileSpec(
    name="tbench",
    base_url=os.environ["ALYSIS_BASE_URL"].strip(),
    api_key_env="ALYSIS_API_KEY",
    default_model=os.environ["ALYSIS_MODEL"].strip(),
    web_search_adapter="auto",
    web_search_model="",
    notes="Terminal-Bench Xiaomi MiMo via Alysis Code proxy.",
)
add_profile(cfg, profile)
set_active_profile(cfg, "tbench")
cfg.web_search_mode = os.environ.get("ALYSIS_TBENCH_WEB_SEARCH_MODE", "off").strip() or "off"
cfg.default_mode = "fullaccess"
cfg.stream = False
verify_cmd = os.environ.get("ALYSIS_VERIFY_CMD")
if verify_cmd is not None and verify_cmd.strip():
    cfg.verify_commands = [verify_cmd.strip()]
    cfg.extra_fields.pop("managed_host_verifier_unavailable", None)
else:
    cfg.verify_commands = []
    cfg.extra_fields["managed_host_verifier_unavailable"] = True
cfg.integration_verify_mode = "off"
apply_sandbox_mode_to_config(cfg, "off")
save_config(cfg)
PY
}

verify_build_identity() {
  # `alysis --version` now prints the version, commit, build timestamp and
  # dirty flag on one line. Recording it here puts the identity of the build
  # under test into the setup log of every task, which is what an earlier
  # campaign lacked: three behaviourally different builds all self-reported
  # "0.9.8", and one campaign ran against an unpinned "latest main", so no
  # score from those runs can be attributed to a source tree.
  identity="$(alysis --version 2>&1 | head -1)"
  echo "alysis_setup build_identity=$identity"

  case "${ALYSIS_REQUIRE_CLEAN_BUILD:-}" in
    1 | true | yes | on | enabled) ;;
    *) return 0 ;;
  esac

  # --version is deliberately exempt from the runtime gate (diagnosing a
  # refusal starts by asking the binary what it thinks it is), so the check is
  # made here against what it printed.
  case "$identity" in
    *"commit: unknown"* | *"dirty: yes"*)
      echo "setup_error unidentifiable_build=$identity" >&2
      echo "setup_error ALYSIS_REQUIRE_CLEAN_BUILD is set; refusing to benchmark a build that cannot name its commit. Build the wheel after running scripts/generate_build_info.py from a clean tree, or set ALYSIS_REQUIRE_CLEAN_BUILD=0 to profile a work-in-progress build deliberately." >&2
      return 1
      ;;
  esac
  return 0
}

main() {
  echo "alysis_setup started_at=$(date -u +%FT%TZ)"
  bootstrap_system_packages
  target="$(install_target)"
  echo "alysis_setup install_target=$target"
  if python_is_311_or_newer; then
    if ! install_with_python_venv "$target"; then
      echo "setup_warning python_venv_path_failed_falling_back_to_uv"
      install_with_uv_python "$target"
    fi
  else
    install_with_uv_python "$target"
  fi
  ln -sf /opt/alysis-venv/bin/alysis /usr/local/bin/alysis
  configure_alysis
  verify_build_identity
  echo "alysis_setup finished_at=$(date -u +%FT%TZ)"
}

main "$@"

#!/usr/bin/env bash
# Run every dependency-light stdlib test added across the 0.13.0 wave (PR1-PR7),
# the 0.13.1 Terminal-Bench calibration wave (PR8), and the 0.14.0 provenance
# hardening wave (PR9).
#
# One command to sanity-check the wave's guardrails without pytest and without
# installing the package. Each listed test loads its module under test directly
# by file path, so it runs in a bare Python 3 interpreter with no third-party
# dependencies -- which is exactly the environment the fixes were authored in
# (Python 3.10, no pytest), and a fast smoke check on any runner box.
#
# Usage:
#   bash scripts/run_all_stdlib_tests.sh
#
# Exit code: 0 when every test passed, 1 when any test failed or errored.
set -u

# Resolve the repo root from this script's location so it works from anywhere.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
cd "${REPO_ROOT}"

PYTHON="${PYTHON:-python3}"

# The dependency-light stdlib tests, in PR order. Extend this list when you add
# another stdlib-only test to the wave.
TESTS=(
  "tests/test_logging_redaction.py"      # PR1: write-path secret redaction
  "tests/test_budget_policy.py"          # PR2: run budget / stop reason / exit code
  "tests/test_dispatch_timing.py"        # PR3: budget-preemptible waits + dispatch telemetry
  "tests/test_service_persistence.py"    # PR4: persistent services + probes
  "tests/test_edit_discipline.py"        # PR5: rewrite / thrash / scratch guards
  "tests/test_build_identity.py"         # PR6: non-fakeable build identity
  "tests/test_sampling_config.py"        # PR6: sampling determinism + fingerprints
  "tests/test_regression_suite.py"       # PR7: defect regression assertion core
  "tests/test_canary_suite.py"           # PR7: canary parsing / aggregation core
  "tests/test_task_budgets.py"           # PR8: per-task Terminal-Bench budgets
  "tests/test_transport_retry.py"        # PR8: connection-drop retry classification
  "tests/test_session_mirror.py"         # PR8: post-run session-capture mirror
  "tests/test_adapter_provenance.py"     # PR9: adapter identity + provenance guard
)

echo "Running ${#TESTS[@]} stdlib tests with: ${PYTHON} ($(${PYTHON} --version 2>&1))"
echo "Repo root: ${REPO_ROOT}"
echo "========================================================================"

failed=0
declare -a FAILED_TESTS=()

for test_file in "${TESTS[@]}"; do
  if [[ ! -f "${test_file}" ]]; then
    printf '  [MISSING] %s\n' "${test_file}"
    failed=1
    FAILED_TESTS+=("${test_file} (missing)")
    continue
  fi
  # Capture output; show it only on failure to keep a green run readable.
  if output="$("${PYTHON}" "${test_file}" 2>&1)"; then
    # unittest prints its "Ran N tests" / "OK" summary to stderr (captured above).
    summary="$(printf '%s\n' "${output}" | grep -E '^Ran [0-9]+ test' | tail -1)"
    printf '  [PASS] %-38s %s\n' "${test_file}" "${summary}"
  else
    printf '  [FAIL] %-38s (exit %d)\n' "${test_file}" "$?"
    printf '%s\n' "${output}" | sed 's/^/         | /'
    failed=1
    FAILED_TESTS+=("${test_file}")
  fi
done

echo "========================================================================"
if [[ "${failed}" -eq 0 ]]; then
  echo "ALL ${#TESTS[@]} STDLIB TESTS PASSED"
else
  echo "FAILURES (${#FAILED_TESTS[@]}):"
  for t in "${FAILED_TESTS[@]}"; do
    echo "  - ${t}"
  done
fi
exit "${failed}"

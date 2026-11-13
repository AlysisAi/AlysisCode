#!/bin/zsh
set -eu

# Finder/Terminal launcher for testing the current checkout with the active
# Alysis Code configuration. Provider, model, API key, and reasoning settings are
# intentionally not overridden here.
repo_root="${0:A:h}"
python_bin="$repo_root/.venv/bin/python"

if [[ ! -x "$python_bin" ]]; then
  python_bin="${commands[python3]:-}"
fi
if [[ -z "$python_bin" ]]; then
  print -u2 "Python 3 was not found. Create .venv or install Python 3 first."
  exit 1
fi

cd "$repo_root"
export PYTHONPATH="$repo_root/src${PYTHONPATH:+:$PYTHONPATH}"

exec "$python_bin" -m alysis_code.cli chat --path "$repo_root" "$@"

#!/usr/bin/env bash
set -euo pipefail
umask 077

# All children run as alysis; Compose's init reaps orphaned processes.
pids=()
cleanup() {
  trap - EXIT TERM INT
  if ((${#pids[@]})); then
    kill "${pids[@]}" 2>/dev/null || true
    wait "${pids[@]}" 2>/dev/null || true
  fi
}
trap cleanup EXIT TERM INT
start() { "$@" & pids+=("$!"); }

start Xvfb "$DISPLAY" -screen 0 1280x800x24 -nolisten tcp -ac
ready=false
for ((attempt=0; attempt<100; attempt++)); do
  if xdpyinfo >/dev/null 2>&1; then ready=true; break; fi
  sleep 0.1
done
if [[ "$ready" != true ]]; then echo 'X11 failed to start.' >&2; exit 1; fi
start openbox --sm-disable
start x11vnc -display "$DISPLAY" -listen 0.0.0.0 -rfbport 5900 -nopw -forever -shared -noxdamage
# The API is accessible only from this container's loopback interface.
# The VNC listener is on the private offline network; only the viewer can reach it.
# There are no host credentials, model keys, or published API/CDP/VNC ports.
start alysis server start --host 127.0.0.1 --port 7070 --data-dir /var/lib/alysis/server
start chromium --no-first-run --no-default-browser-check \
  --disable-background-networking --disable-component-update --disable-sync \
  --user-data-dir=/tmp/desktop-chromium about:blank

# Fail the container if any required service exits, including an unexpected exit 0.
wait -n "${pids[@]}" || true
echo 'A development service exited; stopping the container.' >&2
exit 1

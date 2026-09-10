#!/usr/bin/env bash
# Wrapper: resolve the KDE session environment, then run the bridge.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UID_NUM="$(id -u)"
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/${UID_NUM}}"

# WAYLAND_DISPLAY from the running kwin session (survives reboots / session restarts)
if [ -z "${WAYLAND_DISPLAY:-}" ]; then
    KWIN_PID="$(pgrep -u "${UID_NUM}" -x kwin_wayland | head -1 || true)"
    if [ -n "$KWIN_PID" ]; then
        WAYLAND_DISPLAY="$(tr '\0' '\n' < "/proc/$KWIN_PID/environ" | sed -n 's/^WAYLAND_DISPLAY=//p' | head -1)"
    fi
fi
export WAYLAND_DISPLAY="${WAYLAND_DISPLAY:-wayland-0}"

PYTHON="${SCRIPT_DIR}/venv/bin/python"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

cd "$SCRIPT_DIR"
exec "$PYTHON" monitor_bridge.py "$@"

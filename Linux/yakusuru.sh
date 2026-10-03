#!/usr/bin/env bash
# Yakusuru — Linux launcher.
# First run builds a private environment (one time) and opens the app.
# Options: --reset (rebuild environment)  --setup (open the setup wizard)
set -u
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"

# Native Python for this CPU (x86_64 or aarch64), newest supported first.
HW="$(uname -m)"
PY=""
for c in python3.14 python3.13 python3.12 python3.11 python3.10 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if "$p" -c "import sys, platform; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,14) and platform.machine() == '$HW' else 1)" 2>/dev/null; then
        PY="$p"; break
    fi
done

notify() {
    echo "$1"
    if [ ! -t 1 ] && command -v notify-send >/dev/null 2>&1; then notify-send "Yakusuru" "$1"; fi
}

if [ -z "$PY" ]; then
    notify "Python 3.10+ is required. Install it with your package manager, e.g.
  Debian/Ubuntu:  sudo apt install python3 python3-venv
  Fedora:         sudo dnf install python3
  Arch:           sudo pacman -S python"
    exit 1
fi

if ! "$PY" -c 'import venv, ensurepip' 2>/dev/null; then
    notify "The Python 'venv' module is missing. On Debian/Ubuntu run:  sudo apt install python3-venv"
    exit 1
fi

# Qt 6 needs libxcb-cursor on X11 desktops.
if [ "${XDG_SESSION_TYPE:-x11}" = "x11" ] && command -v ldconfig >/dev/null 2>&1; then
    if ! ldconfig -p 2>/dev/null | grep -q libxcb-cursor; then
        echo "Note: if the window does not open, install libxcb-cursor0 (Debian/Ubuntu) / xcb-util-cursor (Fedora/Arch)."
    fi
fi

exec "$PY" "$ROOT/app/bootstrap.py" "$@"

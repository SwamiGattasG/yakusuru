#!/bin/bash
# Yakusuru — macOS installer / repair tool.
# Double-click this file (or run it in Terminal). It finds a suitable Python,
# prepares the app's private environment, then opens the app.
#   Options:  --reset   rebuild the environment from scratch

cd "$(dirname "$0")" || exit 1
ROOT="$(cd .. && pwd)"

# On Apple Silicon, make sure this script itself runs natively (not under Rosetta),
# otherwise Python, PyTorch and MLX would all be set up for Intel.
if [ "$(sysctl -n hw.optional.arm64 2>/dev/null)" = "1" ] && [ "$(uname -m)" != "arm64" ]; then
    exec arch -arm64 /bin/bash "$0" "$@"
fi
ARCH="$(uname -m)"
export PATH="/opt/homebrew/bin:$HOME/.homebrew/bin:/usr/local/bin:$PATH"

echo ""
echo "  訳  Yakusuru (訳する) — setup"
echo "  ─────────────────────────────────"
echo ""

# Architecture-aware version list (newest first). See app/yakusuru/platform_matrix.py.
if [ "$ARCH" = "arm64" ]; then
    VERSIONS="3.14 3.13 3.12 3.11 3.10"; MAXV="3.14"; BREW_PY="python@3.14"
else
    VERSIONS="3.12 3.11 3.10";           MAXV="3.12"; BREW_PY="python@3.12"   # Intel Mac: PyTorch stops at 3.12
fi

pick_python() {
    local cands=()
    for v in $VERSIONS; do
        cands+=("/opt/homebrew/bin/python$v" "/Library/Frameworks/Python.framework/Versions/$v/bin/python$v"
                "$HOME/.homebrew/bin/python$v" "/usr/local/bin/python$v" "python$v")
    done
    cands+=("python3")
    for c in "${cands[@]}"; do
        local p
        p="$(command -v "$c" 2>/dev/null)" || continue
        [ -x "$p" ] || continue
        "$p" -c "import sys,venv; sys.exit(0 if (3,10) <= sys.version_info[:2] <= tuple(map(int,'$MAXV'.split('.'))) else 1)" \
            >/dev/null 2>&1 || continue
        # Native builds only: an Intel Python on Apple Silicon (Rosetta) is skipped.
        [ "$("$p" -c 'import platform; print(platform.machine())' 2>/dev/null)" = "$ARCH" ] || continue
        echo "$p"
        return 0
    done
    return 1
}

PY="$(pick_python)"
if [ -z "$PY" ]; then
    echo "No suitable Python found. Needed: a native $ARCH Python 3.10 – $MAXV."
    echo "(Intel/Rosetta builds can't use the Apple GPU and are skipped.)"
    echo ""
    BREW=""
    if [ "$ARCH" = "arm64" ] && [ -x /opt/homebrew/bin/brew ]; then BREW=/opt/homebrew/bin/brew;   # native Homebrew
    elif [ "$ARCH" = "x86_64" ] && command -v brew >/dev/null 2>&1; then BREW="$(command -v brew)"; fi
    if [ -n "$BREW" ]; then
        read -r -p "Install $BREW_PY with Homebrew now? [Y/n] " ans
        if [ -z "$ans" ] || [[ "$ans" =~ ^[Yy] ]]; then
            "$BREW" install "$BREW_PY" && PY="$(pick_python)"
        fi
    fi
    if [ -z "$PY" ]; then
        osascript -e "display dialog \"Yakusuru needs a native $ARCH Python $MAXV.\n\nInstall it from python.org (macOS 64-bit universal2 installer), then open Yakusuru again.\" buttons {\"Cancel\", \"Open python.org\"} default button 2 with icon caution" \
                  -e 'if button returned of result is "Open python.org" then open location "https://www.python.org/downloads/macos/"' >/dev/null 2>&1
        echo "Install Python $MAXV from https://www.python.org/downloads/macos/ and run this again."
        read -r -p "Press Return to close." _
        exit 1
    fi
fi

echo "Using $PY ($("$PY" --version 2>&1))"
echo ""
if "$PY" "$ROOT/app/bootstrap.py" --no-launch "$@"; then
    chmod +x "$ROOT/macOS/Yakusuru.app/Contents/MacOS/Yakusuru" 2>/dev/null
    echo ""
    echo "Opening Yakusuru… (you can close this window)"
    open "$ROOT/macOS/Yakusuru.app" 2>/dev/null || \
        (cd "$ROOT/app" && nohup "$HOME/Library/Application Support/Yakusuru/venv/bin/python" -m yakusuru >/dev/null 2>&1 &)
    sleep 2
    exit 0
else
    echo ""
    echo "Setup did not finish. Scroll up for the error, fix it, and run this again."
    echo "Tip: run it with --reset to rebuild from scratch:"
    echo "     \"$0\" --reset"
    read -r -p "Press Return to close." _
    exit 1
fi

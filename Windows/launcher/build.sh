#!/usr/bin/env bash
# Builds Windows/Yakusuru.exe (x64; also runs on Windows on ARM through emulation).
# Works on macOS, Linux or Windows (Git Bash) with Zig:  pip install ziglang
set -euo pipefail
cd "$(dirname "$0")"
ZIG="${ZIG:-python3 -m ziglang}"
$ZIG cc -target x86_64-windows-gnu -O2 -s -municode -Wl,--subsystem,windows \
    -o ../Yakusuru.exe yakusuru_launcher.c yakusuru.rc -lbcrypt
rm -f ../Yakusuru.pdb
echo "Built $(cd .. && pwd)/Yakusuru.exe"

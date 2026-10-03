#!/usr/bin/env bash
# Prepares the environment (showing progress here) and adds Yakusuru
# to your desktop's application menu.
set -e
HERE="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
chmod +x "$HERE/yakusuru.sh"

echo "Preparing Yakusuru (first time can take a few minutes)…"
"$HERE/yakusuru.sh" --no-launch

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
mkdir -p "$DATA/applications" "$DATA/icons/hicolor/512x512/apps"
cp "$ROOT/app/assets/icon.png" "$DATA/icons/hicolor/512x512/apps/yakusuru.png"
cat > "$DATA/applications/yakusuru.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Yakusuru
Comment=Japanese to English AI subtitles
Exec="$HERE/yakusuru.sh" %F
Icon=yakusuru
Terminal=false
Categories=AudioVideo;Video;Utility;
MimeType=video/mp4;video/x-matroska;video/webm;video/quicktime;audio/mpeg;audio/x-wav;audio/flac;
EOF
chmod +x "$DATA/applications/yakusuru.desktop"
# Remove the menu entry from when the app was called "Language Interpreter".
rm -f "$DATA/applications/language-interpreter.desktop" "$DATA/icons/hicolor/512x512/apps/language-interpreter.png"
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DATA/applications" || true
echo "✓ Added 'Yakusuru' to your applications menu."

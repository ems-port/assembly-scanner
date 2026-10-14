#!/bin/bash
# Install "Assembly Scanner.app" into ~/Applications and put an alias on the Desktop.
#   launcher/install.sh            install / update
#   launcher/install.sh --uninstall
# The installed copy records this repo's path in Contents/Resources/repo_path and is
# ad-hoc code-signed, so macOS (TCC) keeps one stable identity for the camera permission.
# Re-running after a change re-signs; macOS may then ask for camera access once more.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
SRC="$HERE/Assembly Scanner.app"
DEST_DIR="$HOME/Applications"
DEST="$DEST_DIR/Assembly Scanner.app"
ALIAS="$HOME/Desktop/Assembly Scanner"

if [ "${1:-}" = "--uninstall" ]; then
  rm -rf "$DEST"
  rm -f "$ALIAS"
  echo "Removed $DEST and the Desktop alias. Camera permission entry: run"
  echo "  tccutil reset Camera ai.polystage.assembly-scanner"
  exit 0
fi

[ -x "$REPO/.venv/bin/python" ] || { echo "error: $REPO/.venv/bin/python not found"; exit 1; }
"$REPO/.venv/bin/python" -c "import PySide6, cv2, zxingcpp" \
  || { echo "error: the venv is missing packages; run: .venv/bin/pip install -r requirements.txt"; exit 1; }

# The venv's base interpreter, as a GUI app binary (python.org framework build).
PYHOME="$(sed -n 's/^home *= *//p' "$REPO/.venv/pyvenv.cfg")"
EMBED_SRC="$PYHOME/../Resources/Python.app/Contents/MacOS/Python"
[ -x "$EMBED_SRC" ] || { echo "error: $EMBED_SRC not found (expects a python.org framework Python)"; exit 1; }

mkdir -p "$DEST_DIR"
rm -rf "$DEST"
cp -R "$SRC" "$DEST"
chmod +x "$DEST/Contents/MacOS/AssemblyScanner"
printf '%s\n' "$REPO" > "$DEST/Contents/Resources/repo_path"
cp "$EMBED_SRC" "$DEST/Contents/MacOS/python"
xattr -cr "$DEST" 2>/dev/null || true
# Sign the embedded interpreter with the app's identifier (TCC keys the camera grant on the
# code identity of the process that asks, which is this binary), then seal the bundle.
codesign --force --sign - --identifier ai.polystage.assembly-scanner "$DEST/Contents/MacOS/python"
codesign --force --sign - "$DEST"
codesign --verify --verbose=1 "$DEST"
# Make Finder/LaunchServices pick up the bundle now.
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister -f "$DEST" 2>/dev/null || true

# Desktop alias (a real Finder alias; falls back to a symlink).
rm -f "$ALIAS"
if ! /usr/bin/osascript >/dev/null 2>&1 <<EOF
tell application "Finder"
  set a to make alias file to (POSIX file "$DEST" as alias) at (path to desktop folder)
  set name of a to "Assembly Scanner"
end tell
EOF
then
  ln -s "$DEST" "$ALIAS"
fi

echo "Installed: $DEST"
echo "Desktop:   $ALIAS"
echo "Repo:      $REPO"
echo "Log:       ~/Library/Logs/AssemblyScanner.log"
echo
echo "First launch: double-click 'Assembly Scanner' on the Desktop. macOS asks"
echo "  \"Assembly Scanner\" would like to access the camera  ->  click Allow."
echo "If you clicked Don't Allow: System Settings > Privacy & Security > Camera >"
echo "turn on Assembly Scanner, then relaunch."

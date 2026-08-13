#!/usr/bin/env bash
#
# Bundle the Babylon.js visualiser into the Android app's assets.
#
# `MapFragment` loads file:///android_asset/visualizer/index.html. That bundle
# is built from web-visualizer/ rather than maintained separately, so the
# semantic colour code and scene graph cannot drift between the handheld and
# the command-post view.
#
# The app copy differs from the served one in two ways, both handled here:
#   1. Babylon is copied out of node_modules (there is no server to mount it).
#   2. The WebSocket URL cannot be same-origin, because the page is loaded from
#      file://. It is rewritten to read a URL injected by the WebView instead.
#
# Usage:  tools/bundle-visualizer.sh [--minify]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="$ROOT/web-visualizer"
DEST="$ROOT/android-app/app/src/main/assets/visualizer"

if [[ ! -d "$SRC/node_modules/@babylonjs" ]]; then
  echo "error: Babylon is not installed. Run:  (cd web-visualizer && npm install)" >&2
  exit 1
fi

echo "==> cleaning $DEST"
rm -rf "$DEST"
mkdir -p "$DEST/src" "$DEST/vendor/babylonjs"

echo "==> copying application sources"
cp "$SRC/public/index.html" "$DEST/index.html"
cp "$SRC/public/styles.css" "$DEST/styles.css"
cp "$SRC"/src/*.js "$DEST/src/"

echo "==> copying Babylon runtime"
# core + loaders only; the GUI package is not used by the scene and would add
# ~2 MB to the APK for nothing.
cp -r "$SRC/node_modules/@babylonjs/core" "$DEST/vendor/babylonjs/core"
if [[ -d "$SRC/node_modules/@babylonjs/loaders" ]]; then
  cp -r "$SRC/node_modules/@babylonjs/loaders" "$DEST/vendor/babylonjs/loaders"
fi

# Strip anything the browser never requests: source maps and TypeScript
# declarations are roughly half the package size.
find "$DEST/vendor" \( -name "*.map" -o -name "*.d.ts" -o -name "*.md" \) -delete

echo "==> rewriting the transport for file:// origins"
# On file:// there is no host to derive a WebSocket URL from, and there is no
# proxy either. The WebView injects window.AURA_AGENT_URL (see MapFragment);
# fall back to the documented default mesh address when it is absent.
python3 - "$DEST/src/DataFetcher.js" <<'PY'
import re, sys
path = sys.argv[1]
source = open(path, encoding="utf-8").read()

original = """    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}/ws`;"""

replacement = """    // Android asset build: the page is served from file://, so there is no
    // same-origin host and no proxy. MapFragment injects window.AURA_AGENT_URL;
    // fall back to the default mesh address documented in the user manual.
    let url;
    if (window.location.protocol === 'file:') {
      const base = window.AURA_AGENT_URL || 'http://10.8.0.1:8080';
      url = base.replace(/^http/, 'ws') + '/ws/agent/events';
    } else {
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      url = `${protocol}//${window.location.host}/ws`;
    }"""

if original not in source:
    print("error: DataFetcher.js does not match the expected shape; "
          "update tools/bundle-visualizer.sh", file=sys.stderr)
    raise SystemExit(1)

open(path, "w", encoding="utf-8").write(source.replace(original, replacement))
print("    patched DataFetcher.js")
PY

# The REST helper must target the agent directly too.
python3 - "$DEST/src/DataFetcher.js" <<'PY'
import sys
path = sys.argv[1]
source = open(path, encoding="utf-8").read()
original = "  async rest(path, options = {}) {\n    const response = await fetch(path, {"
replacement = ("  async rest(path, options = {}) {\n"
               "    // Same reasoning as connect(): no proxy behind file://.\n"
               "    const base = window.location.protocol === 'file:'\n"
               "      ? (window.AURA_AGENT_URL || 'http://10.8.0.1:8080')\n"
               "      : '';\n"
               "    const response = await fetch(base + path, {")
if original in source:
    open(path, "w", encoding="utf-8").write(source.replace(original, replacement))
    print("    patched rest() base URL")
PY

if [[ "${1:-}" == "--minify" ]] && command -v npx >/dev/null 2>&1; then
  echo "==> minifying application sources"
  for file in "$DEST"/src/*.js; do
    npx --yes terser "$file" -c -m --module -o "$file" 2>/dev/null || true
  done
fi

SIZE=$(du -sh "$DEST" | cut -f1)
FILES=$(find "$DEST" -type f | wc -l)
echo "==> done: $FILES files, $SIZE in $DEST"
echo
echo "Note: assets/visualizer/ is gitignored. Run this before packaging a"
echo "release; debug builds show a placeholder when it is absent."

#!/usr/bin/env bash
# Build a clean zip of the extension for the Chrome Web Store.
#
#   extension/scripts/package.sh [output-dir]      (default: extension/dist)
#
# Only the files the extension runs are packaged: an explicit allowlist, so
# docs, tests, scripts and editor litter never reach the store. The zip has
# manifest.json at its root, which is what the Developer Dashboard expects.
set -euo pipefail

EXT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT_DIR="${1:-$EXT_DIR/dist}"

command -v python3 >/dev/null || { echo "package.sh: python3 is required" >&2; exit 1; }
command -v zip >/dev/null || { echo "package.sh: zip is required" >&2; exit 1; }

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

cd "$EXT_DIR"
shopt -s nullglob
for file in *.js *.html *.css icons/*.png lib/*.js; do
  mkdir -p "$STAGE/$(dirname "$file")"
  cp "$file" "$STAGE/$file"
done
shopt -u nullglob

# Write the manifest last, validated. A "key" pins the id of an unpacked copy;
# the store assigns its own id and rejects a first upload that carries one.
VERSION="$(python3 - "$EXT_DIR/manifest.json" "$STAGE" <<'PY'
import json
import sys
from pathlib import Path

source, stage = Path(sys.argv[1]), Path(sys.argv[2])
manifest = json.loads(source.read_text(encoding="utf-8"))
manifest.pop("key", None)

if manifest.get("manifest_version") != 3:
    sys.exit("package.sh: manifest_version must be 3")
version = manifest.get("version", "")
if not version or not all(part.isdigit() for part in version.split(".")):
    sys.exit(f"package.sh: bad version {version!r}")

# Every file the manifest names must be in the package.
referenced = [
    manifest.get("background", {}).get("service_worker"),
    manifest.get("side_panel", {}).get("default_path"),
    manifest.get("options_ui", {}).get("page"),
    manifest.get("action", {}).get("default_popup"),
    *manifest.get("icons", {}).values(),
    *manifest.get("action", {}).get("default_icon", {}).values(),
]
missing = sorted({name for name in referenced if name and not (stage / name).is_file()})
if missing:
    sys.exit(f"package.sh: manifest names files that are not packaged: {', '.join(missing)}")

(stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
print(version)
PY
)"

mkdir -p "$OUT_DIR"
OUT_DIR="$(cd "$OUT_DIR" && pwd)"
ZIP="$OUT_DIR/mastisk-clipper-$VERSION.zip"
rm -f "$ZIP"

# -X drops macOS extra attributes; a sorted list keeps the archive order stable.
(cd "$STAGE" && find . -type f | sed 's|^\./||' | LC_ALL=C sort | zip -X -q "$ZIP" -@)

echo "Built $ZIP"
unzip -Z1 "$ZIP" | sed 's/^/  /'

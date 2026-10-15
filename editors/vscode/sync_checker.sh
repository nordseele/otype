#!/usr/bin/env bash
# Refresh the checker bundled with the extension from the repo:
#   tools/otype.py                 -> checker/tools/otype.py
#   modules/otype/{__init__,ops}.py -> checker/modules/otype/   (the registry: all `check` imports)
#   modules/otype/examples/*.OTY    -> checker/modules/otype/examples/
# Only what `otype.py check` / `ops --json` read: not the simulator, the C, the asm or the tests (`run` and
# `dump` stay repo commands). The layout is the repo's, so `from modules.otype import ops` keeps working.
# Run it after every change of the language, the registry or the checker.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
dest="$here/checker"

[ -f "$repo/tools/otype.py" ] || { echo "tools/otype.py not found in $repo" >&2; exit 1; }
[ -d "$repo/modules/otype" ] || { echo "modules/otype not found in $repo" >&2; exit 1; }

rm -rf "$dest"
mkdir -p "$dest/tools" "$dest/modules"
cp "$repo/tools/otype.py" "$dest/tools/otype.py"
mkdir -p "$dest/modules/otype/examples"
cp "$repo/modules/otype/__init__.py" "$repo/modules/otype/ops.py" "$repo/modules/otype/menu_limits.py" "$dest/modules/otype/"
for f in "$repo"/modules/otype/examples/*.OTY; do
    [ -s "$f" ] && cp "$f" "$dest/modules/otype/examples/"
done
echo "checker synced from $repo:"
( cd "$dest" && find . -type f | sort | sed 's/^/  /' )

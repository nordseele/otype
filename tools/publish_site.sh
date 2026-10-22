#!/usr/bin/env bash
# Publish the OType website after editing tools/site.css, docs/learn/ or docs/cookbook.md:
# rebuild the site into ../otype-docs, check it, show what changed, ask, then commit and push both repositories.
#
#   tools/publish_site.sh "what you changed"
set -euo pipefail
cd "$(dirname "$0")/.."
DOCS=../otype-docs
MSG=${1:-"site: update"}
ID=(-c user.name=nordseele -c user.email=nordseele@users.noreply.github.com)

python3 tools/site.py "$DOCS" >/dev/null      # stops here if a listing no longer passes the checker
python3 tools/cheatsheet.py >/dev/null        # docs/cheatsheet.html, the copy kept in the repository
python3 -m unittest -q tools.test_otype.TestDocsAndEditor

echo "--- otype";      git status --short
echo "--- otype-docs"; git -C "$DOCS" status --short
read -r -p "Publish these changes? [y/N] " ok
case "$ok" in y|Y|o|O) ;; *) echo "Nothing published."; exit 0 ;; esac

git add -A docs tools README.md
git diff --cached --quiet || { git "${ID[@]}" commit -qm "$MSG"; git push -q origin HEAD:main; }
git -C "$DOCS" add -A
git -C "$DOCS" diff --cached --quiet || { git -C "$DOCS" "${ID[@]}" commit -qm "$MSG"; git -C "$DOCS" push -q origin main; }
echo "Published: https://nordseele.github.io/otype-docs/ (online in about a minute)"

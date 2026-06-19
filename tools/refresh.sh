#!/usr/bin/env bash
# Rebuild Note-O-Meter and push to GitHub Pages.
# Run from anywhere:  bash tools/refresh.sh
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Find high-reach posts that match a debunk (best-effort; needs X keys in .env).
"$ROOT/venv/bin/python" viral.py || echo "viral matcher failed; keeping last matches."

"$ROOT/venv/bin/python" build.py "$@"

git add docs/ data/
if git diff --cached --quiet; then
  echo "No changes to commit."
  exit 0
fi

git commit -m "Refresh notes ($(date -u '+%Y-%m-%d %H:%M UTC'))"

# Push only if an 'origin' remote is configured.
if git remote get-url origin >/dev/null 2>&1; then
  git push origin HEAD
  echo "Pushed to origin."
else
  echo "Committed locally. Add a GitHub remote ('origin') to publish."
fi

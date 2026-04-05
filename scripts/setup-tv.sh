#!/bin/bash
# setup-tv.sh — DEPRECATED
#
# npm link is no longer required.
#
# The tv CLI is now a local file dependency declared in the root package.json.
# Run this ONE command from the project root instead:
#
#   npm install
#
# That's it. ./node_modules/.bin/tv will be available and the Python CLI
# will find it automatically — no global install, no PATH changes.

echo "ℹ️  setup-tv.sh is no longer needed."
echo ""
echo "Run from the project root:"
echo "  npm install"
echo ""
echo "The tv CLI will be available at ./node_modules/.bin/tv automatically."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

read -r -p "Run 'npm install' now? [y/N] " response
if [[ "${response}" =~ ^[Yy]$ ]]; then
    cd "${REPO_ROOT}"
    npm install
    echo "✅ Done. You can now run: bash scripts/dev.sh"
fi


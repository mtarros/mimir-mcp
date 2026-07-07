#!/bin/bash
# Register mimir with Claude Code at USER scope — available in every project
# on this machine with no .mcp.json file to create, gitignore, or accidentally
# commit per-repo. Run once per machine; re-run after reinstalling Claude Code.
set -e

if ! command -v claude &>/dev/null; then
    echo "ERROR: 'claude' CLI not found on PATH. Install Claude Code first:"
    echo "  https://docs.claude.com/en/docs/claude-code"
    exit 1
fi

if ! command -v mimir &>/dev/null; then
    echo "ERROR: 'mimir' not found on PATH. Run install.sh first."
    exit 1
fi

claude mcp add mimir --scope user -- mimir

echo ""
echo "Done. mimir is now available in every project you open with Claude Code."
echo "Restart any running Claude Code session (or run /mcp to check) to pick it up."

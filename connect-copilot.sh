#!/bin/bash
# Register mimir with GitHub Copilot in VS Code at USER-PROFILE scope —
# available in every workspace on this machine with no .vscode/mcp.json to
# create, gitignore, or accidentally commit per-repo. Run once per machine.
set -e

if ! command -v code &>/dev/null; then
    echo "ERROR: 'code' CLI not found on PATH."
    echo "  In VS Code: Command Palette -> 'Shell Command: Install code command in PATH'"
    exit 1
fi

if ! command -v mimir &>/dev/null; then
    echo "ERROR: 'mimir' not found on PATH. Run install.sh first."
    exit 1
fi

# ${workspaceFolder} is a VS Code variable resolved at server-start time to
# whichever workspace is currently open, same as it would be in a per-repo
# .vscode/mcp.json — user-profile scope only changes where the config file
# lives, not how the variable resolves.
code --add-mcp '{"name":"mimir","command":"mimir","env":{"MCP_WORKSPACE_ROOT":"${workspaceFolder}"}}'

echo ""
echo "Done. mimir is now available to Copilot Chat (agent mode) in every VS Code workspace."
echo "Reload the VS Code window to pick it up."

#!/bin/bash
# Mimir install script — macOS / Linux
set -e

REPO="git+https://github.com/yourusername/mimir-mcp.git"

# Install pipx if missing
if ! command -v pipx &>/dev/null; then
    echo "pipx not found — installing..."
    pip install --quiet pipx
    pipx ensurepath
    echo "pipx installed. You may need to restart your terminal for PATH changes."
fi

# Install or upgrade mimir
if pipx list | grep -q "mimir-mcp"; then
    echo "Updating mimir..."
    pipx install --force "$REPO"
else
    echo "Installing mimir..."
    pipx install "$REPO"
fi

echo ""
echo "Done. Run this in any project to add config files:"
echo "  mimir-setup"

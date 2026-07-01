#!/bin/bash
# Mimir install script — macOS / Linux
set -e

REPO="git+https://github.com/mtarros/mimir-mcp.git"

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

# Install ripgrep (optional — makes find_callers ~10x faster on large repos)
if command -v rg &>/dev/null; then
    echo "ripgrep already installed ($(rg --version | head -1))."
else
    echo ""
    echo "Installing ripgrep (optional, for faster find_callers)..."
    if command -v brew &>/dev/null; then
        brew install ripgrep
    elif command -v apt-get &>/dev/null; then
        sudo apt-get install -y ripgrep
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y ripgrep
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm ripgrep
    else
        echo "  Could not install ripgrep automatically — no recognised package manager."
        echo "  Install manually: https://github.com/BurntSushi/ripgrep#installation"
        echo "  Mimir works fine without it; find_callers will use the Python fallback."
    fi
fi

echo ""
echo "Done. Run this in any project to add config files:"
echo "  mimir-setup"

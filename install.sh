#!/bin/bash
# Mimir install script — macOS / Linux
set -e

REPO="git+https://github.com/mtarros/mimir-mcp.git"

# Find a Python with a working pip module. Don't assume bare `pip` is on
# PATH — modern macOS ships python3/pip3 but often no bare pip/python at all.
PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" &>/dev/null && "$candidate" -m pip --version &>/dev/null; then
        PYTHON="$candidate"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "ERROR: no Python with a working pip module found on PATH."
    echo "  Install Python 3.10+ (e.g. 'brew install python' on macOS, or your"
    echo "  distro's package manager on Linux), then re-run this script."
    exit 1
fi

# Install pipx if missing. Invoke it via `$PYTHON -m pipx` (not the bare
# `pipx` command) throughout — right after installing, pipx's own script may
# not be on PATH yet in this shell, only after ensurepath + a terminal restart.
if ! command -v pipx &>/dev/null; then
    echo "pipx not found — installing..."
    "$PYTHON" -m pip install --quiet --user pipx
    "$PYTHON" -m pipx ensurepath
    echo "pipx installed. You may need to restart your terminal for PATH changes."
fi

# Install or upgrade mimir
if "$PYTHON" -m pipx list | grep -q "mimir-mcp"; then
    echo "Updating mimir..."
    "$PYTHON" -m pipx install --force "$REPO"
else
    echo "Installing mimir..."
    "$PYTHON" -m pipx install "$REPO"
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
echo "Done. Next, connect mimir to your AI client — mimir-setup does both the MCP"
echo "server registration and the workflow instructions:"
echo ""
echo "  Personal use, nothing shared yet (no files added to any repo):"
echo "    mimir-setup claude --global        # Claude Code"
echo "    mimir-setup copilot-cli --global   # GitHub Copilot CLI"
echo ""
echo "  Ready to share with a team on this project (writes committed .mcp.json"
echo "  for claude; copilot-cli has no project scope, always registers at"
echo "  ~/.copilot/mcp-config.json):"
echo "    mimir-setup            # defaults to claude"
echo "    mimir-setup copilot-cli"

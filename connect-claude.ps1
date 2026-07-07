# Register mimir with Claude Code at USER scope - available in every project
# on this machine with no .mcp.json file to create, gitignore, or accidentally
# commit per-repo. Run once per machine; re-run after reinstalling Claude Code.
$ErrorActionPreference = "Stop"

if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: 'claude' CLI not found on PATH. Install Claude Code first:" -ForegroundColor Red
    Write-Host "  https://docs.claude.com/en/docs/claude-code"
    exit 1
}

if (-not (Get-Command mimir -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: 'mimir' not found on PATH. Run install.ps1 first." -ForegroundColor Red
    exit 1
}

claude mcp add mimir --scope user -- mimir

Write-Host ""
Write-Host "Done. mimir is now available in every project you open with Claude Code."
Write-Host "Restart any running Claude Code session (or run /mcp to check) to pick it up."

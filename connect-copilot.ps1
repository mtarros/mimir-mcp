# Register mimir with GitHub Copilot in VS Code at USER-PROFILE scope -
# available in every workspace on this machine with no .vscode/mcp.json to
# create, gitignore, or accidentally commit per-repo. Run once per machine.
$ErrorActionPreference = "Stop"

if (-not (Get-Command code -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: 'code' CLI not found on PATH." -ForegroundColor Red
    Write-Host "  In VS Code: Command Palette -> 'Shell Command: Install code command in PATH'"
    exit 1
}

if (-not (Get-Command mimir -ErrorAction SilentlyContinue)) {
    Write-Host "ERROR: 'mimir' not found on PATH. Run install.ps1 first." -ForegroundColor Red
    exit 1
}

# ${workspaceFolder} is a VS Code variable resolved at server-start time to
# whichever workspace is currently open, same as it would be in a per-repo
# .vscode/mcp.json - user-profile scope only changes where the config file
# lives, not how the variable resolves. Single-quoted so PowerShell doesn't
# try to expand it itself.
$mcpJson = '{"name":"mimir","command":"mimir","env":{"MCP_WORKSPACE_ROOT":"${workspaceFolder}"}}'
code --add-mcp $mcpJson

Write-Host ""
Write-Host "Done. mimir is now available to Copilot Chat (agent mode) in every VS Code workspace."
Write-Host "Reload the VS Code window to pick it up."

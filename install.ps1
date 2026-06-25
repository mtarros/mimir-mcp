# Mimir install script — Windows (PowerShell)
$ErrorActionPreference = "Stop"

$Repo = "git+https://github.com/mtarros/mimir-mcp.git"

# Install pipx if missing
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    Write-Host "pipx not found — installing..."
    pip install --quiet pipx
    pipx ensurepath
    Write-Host "pipx installed. Restart PowerShell if 'mimir' is not found after install."
}

# Install or upgrade mimir
$installed = pipx list 2>&1 | Select-String "mimir-mcp"
if ($installed) {
    Write-Host "Updating mimir..."
    pipx install --force $Repo
} else {
    Write-Host "Installing mimir..."
    pipx install $Repo
}

Write-Host ""
Write-Host "Done. Run this in any project to add config files:"
Write-Host "  mimir-setup"

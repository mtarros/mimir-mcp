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

# Install ripgrep (optional — makes find_callers ~10x faster on large repos)
if (Get-Command rg -ErrorAction SilentlyContinue) {
    $rgVersion = & rg --version | Select-Object -First 1
    Write-Host "ripgrep already installed ($rgVersion)."
} else {
    Write-Host ""
    Write-Host "Installing ripgrep (optional, for faster find_callers)..."
    $installed = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            winget install --id BurntSushi.ripgrep.MSVC --silent --accept-source-agreements --accept-package-agreements
            $installed = $true
        } catch {
            Write-Host "  winget install failed, trying scoop..."
        }
    }
    if (-not $installed -and (Get-Command scoop -ErrorAction SilentlyContinue)) {
        try {
            scoop install ripgrep
            $installed = $true
        } catch {
            Write-Host "  scoop install failed."
        }
    }
    if (-not $installed) {
        Write-Host "  Could not install ripgrep automatically."
        Write-Host "  Install manually: https://github.com/BurntSushi/ripgrep#installation"
        Write-Host "  Mimir works fine without it; find_callers will use the Python fallback."
    }
}

Write-Host ""
Write-Host "Done. Run this in any project to add config files:"
Write-Host "  mimir-setup"

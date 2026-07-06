# Mimir install script - Windows (PowerShell)
$ErrorActionPreference = "Stop"

$Repo = "git+https://github.com/mtarros/mimir-mcp.git"

# Find a Python with a working pip module. Don't assume bare `pip`/`py` are on
# PATH - a common Windows failure mode is another app (Inkscape, Blender,
# GIMP, etc.) putting its own bundled python.exe on PATH ahead of a real
# install, and that bundled copy usually has no pip module at all.
$PythonCmd = $null
foreach ($candidate in @("python", "py")) {
    if (Get-Command $candidate -ErrorAction SilentlyContinue) {
        # $ErrorActionPreference = "Stop" turns a native command's stderr
        # output into a terminating error even when redirected to $null, so
        # a broken candidate (e.g. Inkscape's bundled python.exe with no pip)
        # must be caught here rather than allowed to abort the whole script.
        try {
            & $candidate -m pip --version *> $null
            if ($LASTEXITCODE -eq 0) { $PythonCmd = $candidate; break }
        } catch {
            continue
        }
    }
}

if (-not $PythonCmd) {
    $pyPath = (Get-Command python -ErrorAction SilentlyContinue).Source
    Write-Host "ERROR: no Python with a working pip module found on PATH." -ForegroundColor Red
    if ($pyPath) {
        Write-Host "  'python' resolves to: $pyPath"
        Write-Host "  That's likely another application's bundled Python (e.g. Inkscape,"
        Write-Host "  Blender, GIMP) shadowing a real install on PATH, rather than a full"
        Write-Host "  Python distribution."
    }
    Write-Host "  Install Python 3.10+ from https://python.org (or 'winget install"
    Write-Host "  Python.Python.3.12'), make sure it's on PATH ahead of any other"
    Write-Host "  python.exe, then re-run this script."
    exit 1
}

# Install pipx if missing. Invoke it via `$PythonCmd -m pipx` (not the bare
# `pipx` command) throughout - right after installing, pipx's own script may
# not be on PATH yet in this session, only after ensurepath + a shell restart.
#
# Every native call below is wrapped in try/catch: with $ErrorActionPreference
# = "Stop", ANY stderr line from a native exe becomes a terminating error in
# this PowerShell version - even benign/informational ones (e.g. pipx prints
# "nothing has been installed with pipx" to stderr on an empty list, and
# ensurepath/install can emit non-error notices the same way). Checking
# $LASTEXITCODE inside the catch/try distinguishes a real failure from that.
if (-not (Get-Command pipx -ErrorAction SilentlyContinue)) {
    Write-Host "pipx not found - installing..."
    try { & $PythonCmd -m pip install --quiet pipx } catch {
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: pip install pipx failed." -ForegroundColor Red; exit 1 }
    }
    try { & $PythonCmd -m pipx ensurepath } catch {
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: pipx ensurepath failed." -ForegroundColor Red; exit 1 }
    }
    Write-Host "pipx installed. Restart PowerShell if 'mimir' is not found after install."
}

# Install or upgrade mimir
$listOutput = ""
try { $listOutput = & $PythonCmd -m pipx list 2>&1 | Out-String } catch { $listOutput = $_.Exception.Message }
if ($listOutput -match "mimir-mcp") {
    Write-Host "Updating mimir..."
    try { & $PythonCmd -m pipx install --force $Repo } catch {
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: pipx install --force failed." -ForegroundColor Red; exit 1 }
    }
} else {
    Write-Host "Installing mimir..."
    try { & $PythonCmd -m pipx install $Repo } catch {
        if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: pipx install failed." -ForegroundColor Red; exit 1 }
    }
}

# Install ripgrep (optional - makes find_callers ~10x faster on large repos)
if (Get-Command rg -ErrorAction SilentlyContinue) {
    $rgVersion = "installed"
    try { $rgVersion = & rg --version | Select-Object -First 1 } catch {}
    Write-Host "ripgrep already installed ($rgVersion)."
} else {
    Write-Host ""
    Write-Host "Installing ripgrep (optional, for faster find_callers)..."
    $installed = $false
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        try {
            # --source winget: without it, winget errors out asking which
            # source to use whenever the (often broken/unauthenticated)
            # msstore source also has a same-named match.
            winget install --id BurntSushi.ripgrep.MSVC --source winget --silent --accept-source-agreements --accept-package-agreements
            if ($LASTEXITCODE -eq 0) { $installed = $true } else { Write-Host "  winget install failed, trying scoop..." }
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

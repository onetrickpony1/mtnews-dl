# mtnews-dl installer for Windows. No admin rights needed (the optional OCR engine may ask).
#
#   Double-click install.cmd in the downloaded folder, or run:
#     powershell -ExecutionPolicy ByPass -File install.ps1
#   Straight from GitHub (needs access to the private repo through git):
#     $env:MTNEWS_DL_SOURCE = "git+https://github.com/onetrickpony1/mtnews-dl"; .\install.ps1
#
# What it does:
#   1. installs uv (https://docs.astral.sh/uv/) if missing: it brings its own Python
#   2. installs mtnews-dl with its Python packages in an isolated environment
#   3. puts mtnews-dl, tom-dl, mttd-dl and talkmt-dl on your PATH
#   4. checks everything with `mtnews-dl doctor`, offering to install the OCR engine
#      (Tesseract, via winget) with English + Maltese data and, if no Chrome is installed,
#      a private Chrome for Testing for the Times of Malta archive
# Run it again to update. Uninstall: uv tool uninstall mtnews-dl

# "Continue": Windows PowerShell turns harmless messages from programs into errors under "Stop";
# every step checks $LASTEXITCODE instead
$ErrorActionPreference = "Continue"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
if ($env:MTNEWS_DL_SOURCE) { $Source = $env:MTNEWS_DL_SOURCE }
elseif ($Here -and (Test-Path (Join-Path $Here "pyproject.toml")) -and
        (Select-String -Path (Join-Path $Here "pyproject.toml") -Pattern '^name = "mtnews-dl"' -Quiet)) { $Source = $Here }
else { $Source = "git+https://github.com/onetrickpony1/mtnews-dl" }
$Python = if ($env:MTNEWS_DL_PYTHON) { $env:MTNEWS_DL_PYTHON } else { "3.12" }

function Say($msg) { Write-Host "`n==> $msg" -ForegroundColor Cyan }
# throw, not exit: when run with "irm ... | iex", exit would close the user's PowerShell window
function Fail($msg) { Write-Host "`nmtnews-dl installer: $msg" -ForegroundColor Red; throw "mtnews-dl was not installed" }

Say "Installing mtnews-dl from $Source"

# 1. uv
$uv = Get-Command uv -ErrorAction SilentlyContinue
if ($uv) { $UV = $uv.Source }
else {
    $cand = Join-Path $env:USERPROFILE ".local\bin\uv.exe"
    if (-not (Test-Path $cand)) {
        Say "Installing uv (Python manager, from astral.sh)"
        try { Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression }
        catch { Fail "could not download uv: $($_.Exception.Message)" }
    }
    if (Test-Path $cand) { $UV = $cand } else { Fail "uv was installed but can't be found; open a new window and run this again" }
}

# 2. mtnews-dl (+ Python if needed)
Say "Installing mtnews-dl and its packages (Python $Python is downloaded if needed)"
& $UV tool install --force --python $Python $Source
if ($LASTEXITCODE -ne 0) { Fail "installing mtnews-dl failed (see above)" }

# 3. PATH
try { & $UV tool update-shell 2>&1 | Out-Null } catch { }
$Bin = (& $UV tool dir --bin).Trim()
$MT = Join-Path $Bin "mtnews-dl.exe"
if (-not (Test-Path $MT)) { Fail "mtnews-dl was installed but $MT is missing" }

# 4. dependency check (+ optional OCR)
Say "Checking the installation"
$answer = ""
if ([Environment]::UserInteractive -and -not $env:CI) {
    try { $answer = Read-Host "Install the remaining parts: OCR engine (Tesseract) + English/Maltese data, and a browser for the Times of Malta archive if no Chrome is installed? [Y/n]" } catch { $answer = "" }
}
if (-not $env:CI -and ($answer -eq "" -or $answer -match "^[Yy]")) { & $MT doctor --fix } else { & $MT doctor }

Say ("Done: " + (& $MT --version))
if (-not (($env:Path -split ";") -contains $Bin)) {
    Write-Host "Open a NEW terminal window so the commands are found (they are in $Bin)."
}
Write-Host ""
Write-Host "Start with:   mtnews-dl --easy        (guided: sources, dates, folder)"
Write-Host "Sources:      mtnews-dl sources"
Write-Host "Check setup:  mtnews-dl doctor"
Write-Host "Each paper on its own: tom-dl, mttd-dl, talkmt-dl (same options; add --help)"

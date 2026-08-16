# WakeUpMarshall - one-command installer for Windows (BLE via 'bleak').
#
# Usage (PowerShell):
#   irm https://raw.githubusercontent.com/firelake/WakeUpMarshall/main/install.ps1 | iex
#   powershell -ExecutionPolicy Bypass -File install.ps1   # from a local checkout
#
$ErrorActionPreference = "Stop"
$App = "wakeupmarshall"
$Base = Join-Path $HOME ".wakeupmarshall"
$AppDir = Join-Path $Base "app"
$VenDir = Join-Path $Base "venv"
$RepoUrl = "https://github.com/firelake/WakeUpMarshall.git"
$SrcDir = $args[0]

function Info($m) { Write-Host "[$App] $m" -ForegroundColor Cyan }
function Die($m)  { Write-Host "[$App] ERROR: $m" -ForegroundColor Red; exit 1 }

# --- locate python -------------------------------------------------------
$Py = $null
foreach ($cand in @("py", "python")) {
    try {
        $v = & $cand -c "import sys; print(sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $v) { $Py = $cand; break }
    } catch {}
}
if (-not $Py) { Die "Python 3.9+ is required. Install it from https://www.python.org (tick 'Add to PATH')." }

New-Item -ItemType Directory -Force -Path $Base | Out-Null

# --- get the code --------------------------------------------------------
if ($SrcDir) {
    Info "Installing from local checkout: $SrcDir"
    if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
    Copy-Item -Recurse $SrcDir $AppDir
} else {
    $HaveGit = $null -ne (Get-Command git -ErrorAction SilentlyContinue)
    if (-not (Test-Path (Join-Path $AppDir ".git"))) {
        if ($HaveGit) {
            Info "Cloning repository..."
            git clone --depth 1 $RepoUrl $AppDir
        } else {
            Info "git not found - downloading source zip..."
            $Zip = Join-Path $Base "src.zip"
            Invoke-WebRequest -Uri "https://codeload.github.com/firelake/WakeUpMarshall/zip/refs/heads/main" -OutFile $Zip
            Expand-Archive -Force $Zip $Base
            if (Test-Path $AppDir) { Remove-Item -Recurse -Force $AppDir }
            Move-Item (Join-Path $Base "WakeUpMarshall-main") $AppDir
        }
    } else {
        Info "Updating repository..."
        if ($HaveGit) { git -C $AppDir pull --ff-only }
    }
}

# --- venv + install ------------------------------------------------------
if (-not (Test-Path (Join-Path $VenDir "Scripts\python.exe"))) {
    Info "Creating virtualenv..."
    & $Py -m venv $VenDir
    if ($LASTEXITCODE -ne 0) { Die "Failed to create virtualenv." }
}
$VenvPy = Join-Path $VenDir "Scripts\python.exe"
Info "Installing dependencies..."
& $VenvPy -m pip install --quiet --upgrade pip
& $VenvPy -m pip install --quiet $AppDir
if ($LASTEXITCODE -ne 0) { Die "pip install failed." }

# --- autostart (scheduled task at logon) ---------------------------------
$TaskName = "WakeUpMarshall"
$Exe = Join-Path $VenDir "Scripts\wakeupmarshall.exe"
try {
    $Action  = New-ScheduledTaskAction -Execute $Exe -Argument "serve --no-open"
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Description "WakeUpMarshall speaker waker" -Force | Out-Null
    Info "Autostart registered: Task Scheduler -> $TaskName"
} catch {
    Info "Could not register autostart task: $($_.Exception.Message)"
}

# --- start now -----------------------------------------------------------
$Old = Get-Process -Name "wakeupmarshall" -ErrorAction SilentlyContinue
if ($Old) { Stop-Process -Name "wakeupmarshall" -Force }
Start-Process -FilePath $Exe -ArgumentList "serve --no-open" -WindowStyle Hidden
Start-Sleep -Seconds 2
Start-Process "http://127.0.0.1:8756"

Write-Host ""
Info "Install complete."
Write-Host "  Web UI   : http://127.0.0.1:8756"
Write-Host "  Data dir : $Base"
Write-Host "  CLI      : $Exe --help"
Write-Host "  Stop     : Stop-Process -Name wakeupmarshall"
Write-Host "  Autostart: schtasks /query /tn $TaskName"

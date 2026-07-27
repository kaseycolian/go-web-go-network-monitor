# Go Web, Go! - one-click launcher.
# Starts the monitor (if the Scheduled Task isn't already running) and
# opens the dashboard in the default browser. Used by the desktop shortcut
# created by make-shortcut.ps1 - not normally run by hand.
$ErrorActionPreference = "SilentlyContinue"

$port = 46299
$cfg = Join-Path $PSScriptRoot "config.json"
if (Test-Path $cfg) {
    try {
        $p = (Get-Content $cfg -Raw | ConvertFrom-Json).port
        if ($p) { $port = $p }
    } catch {}
}

$task = Get-ScheduledTask -TaskName "GoWebGo" -ErrorAction SilentlyContinue
if ($task) {
    if ($task.State -ne "Running") {
        Start-ScheduledTask -TaskName "GoWebGo"
    }
} else {
    # not installed as a service yet - run it directly in the background
    $venvPy = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
    if (Test-Path $venvPy) {
        Start-Process -WindowStyle Hidden $venvPy `
            -ArgumentList "-m", "app.main" -WorkingDirectory $PSScriptRoot
    } else {
        [System.Windows.Forms.MessageBox]::AddType(
            "System.Windows.Forms", [ref]$null) 2>$null
        Add-Type -AssemblyName System.Windows.Forms
        [System.Windows.Forms.MessageBox]::Show(
            "Go Web, Go! isn't installed yet. Run install.ps1 first.",
            "Go Web, Go!", "OK", "Warning") | Out-Null
        exit 1
    }
}

# wait briefly for the server to answer before opening the browser
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    try {
        Invoke-WebRequest "http://localhost:$port/api/status" `
            -UseBasicParsing -TimeoutSec 1 | Out-Null
        $ok = $true
        break
    } catch { Start-Sleep -Milliseconds 500 }
}

Start-Process "http://localhost:$port"

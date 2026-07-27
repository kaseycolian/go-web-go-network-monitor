# Go Web, Go! - Windows installer.
# NOTE: keep this file pure ASCII - Windows PowerShell 5.1 misreads UTF-8
# without a BOM, and characters like em dashes break parsing.
# Creates a venv, installs deps, sets a dashboard passcode, opens the firewall
# port (Private profile only) and registers a per-user Scheduled Task so the
# monitor starts at logon. Run from the project folder:

#   powershell -ExecutionPolicy Bypass -File install.ps1

param(
    [int]$Port = 46299,
    [string]$PythonExe = ""
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# -- find python (prefer the newest py launcher version)
if (-not $PythonExe) {
    $PythonExe = "python"
    if (Get-Command py -ErrorAction SilentlyContinue) { $PythonExe = "py" }
}
Write-Host "Using Python:" (& $PythonExe --version)

# -- venv + deps
if (-not (Test-Path "$root\.venv")) {
    & $PythonExe -m venv "$root\.venv"
}
$venvPy = "$root\.venv\Scripts\python.exe"
& $venvPy -m pip install --upgrade pip --quiet
& $venvPy -m pip install -r "$root\requirements.txt" --quiet
Write-Host "Dependencies installed."

# -- passcode: require it from other devices on the network. The host machine
#    (localhost) is always exempt and can reset it later from Settings.
$cfg = "$root\config.json"
$hasPin = (Test-Path $cfg) -and
          ((Get-Content $cfg -Raw | ConvertFrom-Json).password_hash)
if (-not $hasPin) {
    Write-Host ""
    Write-Host "Set a dashboard passcode (numbers only, at least 4 digits)."
    Write-Host "Other devices on your network will need it; this machine won't."
    while ($true) {
        $sec = Read-Host "Passcode" -AsSecureString
        $pin = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
            [Runtime.InteropServices.Marshal]::SecureStringToBSTR($sec))
        if ($pin -match '^\d{4,}$') { break }
        Write-Host "  Must be at least 4 digits (numbers only). Try again."
    }
    & $venvPy -m app.auth set-passcode $pin
} else {
    Write-Host ""
    Write-Host "Passcode already set - keeping it."
    Write-Host ""
}

# -- firewall: allow inbound on the Private profile only
$ruleName = "Go Web, Go! ($Port)"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    try {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound `
            -Action Allow -Protocol TCP -LocalPort $Port -Profile Private | Out-Null
        Write-Host "Firewall rule added (Private networks only)."
    } catch {
        Write-Warning ("Could not add firewall rule (need admin). " +
            "Dashboard will still work on this machine; run this script " +
            "as admin once to allow phones/other PCs to reach it.")
    }
}

# -- scheduled task: start at logon, restart on failure
$taskName = "GoWebGo"
$action = New-ScheduledTaskAction -Execute $venvPy `
    -Argument "-m app.main" -WorkingDirectory $root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit (New-TimeSpan -Days 3650) `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
try { Unregister-ScheduledTask -TaskName $taskName -Confirm:$false -ErrorAction Stop } catch {}
Register-ScheduledTask -TaskName $taskName -Action $action `
    -Trigger $trigger -Settings $settings | Out-Null
Start-ScheduledTask -TaskName $taskName
Write-Host ""
Write-Host "Scheduled task '$taskName' registered and started."
Write-Host ""

$ip = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike "169.254*" -and $_.IPAddress -ne "127.0.0.1" } |
    Select-Object -First 1).IPAddress
Write-Host ""
Write-Host "Done! Dashboard:  http://localhost:$Port"
if ($ip) { Write-Host "From your phone: http://${ip}:$Port" }

Write-Host ""
Write-Host "Creating Windows Desktop Shortcut..."

Write-Host ""

try { powershell -ExecutionPolicy Bypass -File make-shortcut.ps1 } catch { 
    Write-Host "Unable to create shortcut. Must run launch.ps1 script for future usages." }

Write-Host ""
Write-Host ""
Write-Host "Use Go, Web, Go! desktop shortcut to launch in the future."

Write-Host ""
Write-Host ""

Write-Host "Mission control has been activated, you may close this terminal at any time...."

Write-Host ""
Write-Host ""
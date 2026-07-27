# Go Web, Go! - creates a one-click desktop shortcut.
# Run once from the project folder:  powershell -ExecutionPolicy Bypass -File make-shortcut.ps1
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

# -- build a .ico matching the app icon (dark tile, pink bolt, green dot),
#    since Windows shortcuts need .ico, not the .svg the dashboard uses
Add-Type -AssemblyName System.Drawing
$icoPath = Join-Path $root "icon.ico"
if (-not (Test-Path $icoPath)) {
    $bmp = New-Object System.Drawing.Bitmap(64, 64)
    $g = [System.Drawing.Graphics]::FromImage($bmp)
    $g.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
    $g.Clear([System.Drawing.Color]::FromArgb(13, 15, 20))
    $pen = New-Object System.Drawing.Pen(
        [System.Drawing.Color]::FromArgb(224, 74, 146), 5)
    $pen.StartCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.EndCap = [System.Drawing.Drawing2D.LineCap]::Round
    $pen.LineJoin = [System.Drawing.Drawing2D.LineJoin]::Round
    $pts = @(
        New-Object System.Drawing.Point(6, 38)
        New-Object System.Drawing.Point(18, 38)
        New-Object System.Drawing.Point(24, 20)
        New-Object System.Drawing.Point(32, 48)
        New-Object System.Drawing.Point(40, 28)
        New-Object System.Drawing.Point(44, 38)
        New-Object System.Drawing.Point(58, 38)
    )
    $g.DrawLines($pen, $pts)
    $greenBrush = New-Object System.Drawing.SolidBrush(
        [System.Drawing.Color]::FromArgb(16, 166, 89))
    $g.FillEllipse($greenBrush, 53, 33, 10, 10)
    $g.Dispose()

    $hIcon = $bmp.GetHicon()
    $icon = [System.Drawing.Icon]::FromHandle($hIcon)
    $fs = New-Object System.IO.FileStream($icoPath,
        [System.IO.FileMode]::Create)
    $icon.Save($fs)
    $fs.Close()
    $bmp.Dispose()
}

# -- the shortcut itself
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "Go Web, Go!.lnk"
$ws = New-Object -ComObject WScript.Shell
$shortcut = $ws.CreateShortcut($lnkPath)
$shortcut.TargetPath = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"
$shortcut.Arguments = "-WindowStyle Hidden -ExecutionPolicy Bypass -File `"$root\launch.ps1`""
$shortcut.WorkingDirectory = $root
$shortcut.IconLocation = $icoPath
$shortcut.Description = "Open the Go Web, Go! network dashboard"
$shortcut.WindowStyle = 7   # minimized - the launcher runs invisibly anyway
$shortcut.Save()

Write-Host "Shortcut created: $lnkPath"
Write-Host "Double-click it any time to open the dashboard (starts the monitor if needed)."

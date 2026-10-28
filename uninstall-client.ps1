<#
Uninstaller for the Async Storage desktop client on Windows. It removes the
desktop shortcut and the virtualenv (venv\) made by install-client.ps1, then
offers to delete config.toml - kept by default.

The code in this folder, your download folder and everything you downloaded
are always left in place. Double-click uninstall-client.bat, or from
PowerShell:

  powershell -ExecutionPolicy Bypass -File .\uninstall-client.ps1
#>

$ErrorActionPreference = 'Continue'

$Root = $PSScriptRoot
$VenvDir = Join-Path $Root 'venv'
$ConfigPath = Join-Path $Root 'config.toml'

function Say([string]$Text = '') { Write-Host $Text }
function Warn([string]$Text) { Write-Host "    ! $Text" -ForegroundColor Yellow }
function Die([string]$Text) {
    Write-Host "error: $Text" -ForegroundColor Red
    exit 1
}

# Confirm-Choice "Question" [Y|N] - $true on yes.
function Confirm-Choice([string]$Question, [string]$Default = 'Y') {
    $hint = if ($Default -eq 'Y') { 'Y/n' } else { 'y/N' }
    while ($true) {
        $ans = Read-Host "    $Question [$hint]"
        if ([string]::IsNullOrWhiteSpace($ans)) { $ans = $Default }
        if ($ans -match '^[Yy]') { return $true }
        if ($ans -match '^[Nn]') { return $false }
        Say '    Please answer y or n.'
    }
}

function Test-InVenv([string]$Path) {
    return [bool]($Path -and $Path.StartsWith($VenvDir + '\', [StringComparison]::OrdinalIgnoreCase))
}

if ([Console]::IsInputRedirected) { Die 'this asks questions - run it from a terminal or double-click uninstall-client.bat.' }

Say 'Async Storage desktop client - uninstall'
Say "    folder:  $Root"
Say ''

# Desktop shortcuts that start the app from this checkout's venv. Shortcuts to
# other checkouts are left alone.
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcuts = @(Get-ChildItem -LiteralPath $desktop -Filter '*.lnk' -ErrorAction SilentlyContinue |
    Where-Object { Test-InVenv $shell.CreateShortcut($_.FullName).TargetPath })

$hasVenv = Test-Path -LiteralPath $VenvDir
$hasConfig = Test-Path -LiteralPath $ConfigPath

# Read the download folder from config.toml only to tell the user it's kept.
$downloadDir = ''
if ($hasConfig) {
    $m = Select-String -LiteralPath $ConfigPath -Encoding UTF8 -Pattern '^\s*download_dir\s*=\s*"(.*)"\s*$' |
        Select-Object -First 1
    if ($m) { $downloadDir = $m.Matches[0].Groups[1].Value -replace '\\\\', '\' }
}

if (-not $hasVenv -and $shortcuts.Count -eq 0 -and -not $hasConfig) {
    Say 'Nothing to remove: no venv\, no config.toml and no desktop shortcut to this folder.'
    exit 0
}

if ($hasVenv -or $shortcuts.Count -gt 0) {
    Say '    This removes:'
    foreach ($s in $shortcuts) { Say "      - the desktop shortcut $($s.FullName)" }
    if ($hasVenv) { Say "      - the Python environment $VenvDir" }
}
Say '    Always kept:'
Say "      - the code in $Root"
if ($downloadDir) {
    Say "      - your download folder $downloadDir and everything in it"
} else {
    Say '      - your download folder and everything in it'
}
if ($hasVenv -and (Test-Path -LiteralPath (Join-Path $Root 'server-config.toml'))) {
    Warn 'This folder also has a server-config.toml. If you run the server from here, it may use venv\ too and will not start until you recreate it.'
}
Say ''

if ($hasVenv -or $shortcuts.Count -gt 0) {
    if (-not (Confirm-Choice 'Go ahead?' 'Y')) {
        Say 'Stopped. Nothing was changed.'
        exit 0
    }

    # Files in venv\ can't be deleted while the app (or anything else) runs from it.
    $running = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try { Test-InVenv $_.Path } catch { $false }
    })
    if ($hasVenv -and $running.Count -gt 0) {
        Say '    These programs are running from venv\ and have to close first:'
        foreach ($p in $running) { Say "      - $($p.ProcessName) (pid $($p.Id))" }
        if (-not (Confirm-Choice 'Close them now?' 'Y')) { Die 'close them yourself, then run this again.' }
        $running | Stop-Process -Force -ErrorAction SilentlyContinue
        Start-Sleep -Seconds 1
    }

    foreach ($s in $shortcuts) {
        Remove-Item -LiteralPath $s.FullName -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $s.FullName) { Warn "Couldn't remove $($s.FullName)." } else { Say "    OK - removed $($s.Name) from the desktop." }
    }
    if ($hasVenv) {
        Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $VenvDir) { Warn "Couldn't fully remove $VenvDir; delete what's left by hand." } else { Say '    OK - removed venv\.' }
    }
}

if ($hasConfig) {
    if (Confirm-Choice "Also delete config.toml (holds the server URL and API key)? $ConfigPath" 'N') {
        Remove-Item -LiteralPath $ConfigPath -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath $ConfigPath) { Warn "Couldn't remove $ConfigPath." } else { Say '    OK - removed config.toml.' }
    } else {
        Say '    Kept config.toml; install-client.bat will offer its settings as the defaults.'
    }
}

Say ''
Say 'Done. Run install-client.bat to set the client up again.'

<#
Step-by-step setup for the Async Storage desktop client on Windows.

It walks through everything a client machine needs, one question at a time,
offering a default for each (press Enter to accept it):

  1. Python    - finds Python 3.11+ (offers to install it with winget), creates
                 venv/ and installs the client's requirements
  2. Settings  - server URL, API key and download folder, then a connection
                 test against the server
  3. Shortcut  - an "Async Storage" shortcut on your desktop
  4. Review    - shows the plan; your config and shortcut are not touched
                 until you confirm
  5. Apply     - writes config.toml, creates the shortcut, offers to start
                 the app

Run it from your checkout. Double-click install-client.bat, or from PowerShell:

  powershell -ExecutionPolicy Bypass -File .\install-client.ps1

Re-running it is safe: your current settings are offered as the defaults.
Remove everything again with uninstall-client.bat.
#>

# Native commands (python, pip, winget) report failure through $LASTEXITCODE,
# which is checked explicitly; 'Stop' would also trip on their stderr in 5.1.
$ErrorActionPreference = 'Continue'

$TotalSteps = 5
$Root = $PSScriptRoot
$VenvDir = Join-Path $Root 'venv'
$VenvPy = Join-Path $VenvDir 'Scripts\python.exe'
$VenvPyw = Join-Path $VenvDir 'Scripts\pythonw.exe'
$ConfigPath = Join-Path $Root 'config.toml'
$IconPath = Join-Path $Root 'client\app_icon.ico'
$ShortcutName = 'Async Storage.lnk'
$DefaultUrl = 'http://server-laptop:8000'
$WingetPythonId = 'Python.Python.3.13'
# The example config's placeholder key counts as "no key yet".
$PlaceholderKey = 'PASTE_THE_SAME_KEY_AS_THE_SERVER'

# --- Prompt helpers ----------------------------------------------------------
function Say([string]$Text = '') { Write-Host $Text }
function Step([int]$N, [string]$Title) {
    Write-Host ''
    Write-Host "[$N/$TotalSteps] $Title" -ForegroundColor Cyan
}
function Warn([string]$Text) { Write-Host "    ! $Text" -ForegroundColor Yellow }
function Die([string]$Text) {
    Write-Host "error: $Text" -ForegroundColor Red
    exit 1
}

# Ask "Question" [default] - reads one line; empty input takes the default.
function Ask([string]$Question, [string]$Default = '') {
    if ($Default) { $ans = Read-Host "    $Question [$Default]" } else { $ans = Read-Host "    $Question" }
    if ([string]::IsNullOrWhiteSpace($ans)) { return $Default }
    return $ans.Trim()
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

function Hide-Key([string]$Key) {
    if ($Key.Length -le 4) { return '****' }
    return '****' + $Key.Substring($Key.Length - 4)
}

# --- Python helpers ----------------------------------------------------------
# Returns @{ Version; Exe } for a Python that runs, else $null. The Microsoft
# Store "python" alias prints nothing useful and fails, so it's skipped.
function Get-PythonInfo([string]$Command, [string[]]$Arguments = @()) {
    if (-not (Get-Command $Command -ErrorAction SilentlyContinue)) { return $null }
    $out = @(& $Command @Arguments -c "import sys; print('%d.%d.%d' % sys.version_info[:3]); print(sys.executable)" 2>$null)
    if ($LASTEXITCODE -ne 0 -or $out.Count -lt 2) { return $null }
    try { $ver = [version]$out[0].Trim() } catch { return $null }
    return @{ Version = $ver; Exe = $out[1].Trim() }
}

function Test-PythonVersion($Info) { return [bool]($Info -and $Info.Version -ge [version]'3.11') }

# Finds a Python 3.11+ to build the venv from. Sets $script:OldPython to the
# first too-old one seen, so the caller can say what was found.
function Find-Python {
    $candidates = @(
        @{ Cmd = 'py'; Args = @('-3') },
        @{ Cmd = 'python'; Args = @() },
        @{ Cmd = 'python3'; Args = @() }
    )
    # Per-user installs (e.g. just made by winget) that aren't on PATH yet.
    $local = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue
    foreach ($f in $local) { $candidates += @{ Cmd = $f.FullName; Args = @() } }

    foreach ($c in $candidates) {
        $info = Get-PythonInfo $c.Cmd $c.Args
        if (Test-PythonVersion $info) { return $info }
        if ($info -and -not $script:OldPython) { $script:OldPython = $info }
    }
    return $null
}

# Re-reads PATH from the registry so a Python installed by winget a moment ago
# is found without opening a new terminal.
function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

# Runs a Python snippet with the venv's Python, passing values through
# environment variables rather than the command line (no quoting trouble with
# paths or keys). Returns its output lines.
function Invoke-VenvPython([string]$Code, [hashtable]$Values = @{}) {
    foreach ($k in $Values.Keys) { Set-Item "env:$k" $Values[$k] }
    try {
        return @($Code | & $VenvPy -)
    } finally {
        foreach ($k in $Values.Keys) { Remove-Item "env:$k" -ErrorAction SilentlyContinue }
    }
}

function Test-VenvImport([string]$Module) {
    & $VenvPy -c "import $Module" 2>$null
    return ($LASTEXITCODE -eq 0)
}

$ReadConfigPy = @'
import json, os, tomllib
try:
    with open(os.environ['AS_CFG'], 'rb') as fh:
        raw = tomllib.load(fh)
except Exception:
    raw = {}
server = raw.get('server') or {}
client = raw.get('client') or {}
print(json.dumps({
    'url': str(server.get('url', '')),
    'key': str(server.get('api_key', '')),
    'dir': str(client.get('download_dir', '')),
}))
'@

# Prints a status word, then a detail line: OK <file count>, BADKEY,
# NOTSERVER <http status>, or UNREACHABLE <error>.
$TestServerPy = @'
import os, httpx
url = os.environ.get('AS_URL', '')
key = os.environ.get('AS_KEY', '')
try:
    r = httpx.get(url + '/health', headers={'X-API-Key': key}, timeout=10)
except Exception as e:
    print('UNREACHABLE')
    print(str(e) or type(e).__name__)
    raise SystemExit
try:
    body = r.json()
except ValueError:
    body = None
if r.status_code != 200 or not isinstance(body, dict) or body.get('status') != 'ok':
    print('NOTSERVER')
    print('HTTP %d' % r.status_code)
elif 'files' in body:
    # /health only reports the file count to a caller with the right key.
    print('OK')
    print(body['files'])
else:
    print('BADKEY')
    print('')
'@

# Uses the app's own writer, so the file is exactly what Settings would save.
$SaveConfigPy = @'
import os
from client.config import save_config
print(save_config(os.environ['AS_URL'], os.environ['AS_KEY'], os.environ.get('AS_DIR', ''), os.environ['AS_CFG']))
'@

# --- Settings helpers --------------------------------------------------------
# "server-laptop:8000" -> "http://server-laptop:8000"; $null if it isn't a URL.
function ConvertTo-ServerUrl([string]$Text) {
    $u = $Text.Trim()
    if (-not $u) { return $null }
    if ($u -notmatch '^[A-Za-z][A-Za-z0-9+.-]*://') { $u = "http://$u" }
    $uri = $null
    if (-not [Uri]::TryCreate($u, [UriKind]::Absolute, [ref]$uri)) { return $null }
    if ($uri.Scheme -notin @('http', 'https') -or -not $uri.Host) { return $null }
    return $u.TrimEnd('/')
}

function Read-ServerUrl([string]$Current) {
    while ($true) {
        $url = ConvertTo-ServerUrl (Ask 'Server URL' $Current)
        if ($url) { return $url }
        Say '    That is not a server address. Example: http://server-laptop:8000'
    }
}

function Read-ApiKey([string]$Current) {
    while ($true) {
        if ($Current) {
            $prompt = "    API key (Enter = keep the current one, $(Hide-Key $Current))"
        } else {
            $prompt = "    API key (the server's [server].api_key)"
        }
        $secure = Read-Host $prompt -AsSecureString
        $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
        try {
            $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr).Trim()
        } finally {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
        if (-not $key) {
            if ($Current) { return $Current }
            Say '    The API key is required.'
            continue
        }
        if ($key.Length -lt 16) {
            Warn 'That key is shorter than 16 characters, which the server never uses. Check you pasted all of it.'
        }
        return $key
    }
}

function Get-DownloadsFolder {
    try {
        $p = (New-Object -ComObject Shell.Application).NameSpace('shell:Downloads').Self.Path
        if ($p -and (Test-Path -LiteralPath $p -PathType Container)) { return $p }
    } catch {}
    return (Join-Path $env:USERPROFILE 'Downloads')
}

# Opens the Windows folder picker, starting at $Start (or its nearest existing
# parent). Returns the chosen folder, or $null if cancelled / unavailable.
function Select-FolderDialog([string]$Start) {
    try {
        Add-Type -AssemblyName System.Windows.Forms -ErrorAction Stop
        $dlg = New-Object System.Windows.Forms.FolderBrowserDialog
        $dlg.Description = 'Choose the folder Async Storage saves downloads into'
        $dlg.ShowNewFolderButton = $true
        $p = $Start
        while ($p -and -not (Test-Path -LiteralPath $p -PathType Container)) { $p = Split-Path $p -Parent }
        if ($p) { $dlg.SelectedPath = $p }
        # An invisible topmost owner keeps the dialog from opening behind the console.
        $owner = New-Object System.Windows.Forms.Form
        $owner.TopMost = $true
        try {
            if ($dlg.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { return $dlg.SelectedPath }
        } finally {
            $owner.Dispose()
            $dlg.Dispose()
        }
    } catch {
        Warn "Couldn't open the folder picker ($($_.Exception.Message)). Type the path instead."
    }
    return $null
}

# A typed folder -> full path; $null unless it's absolute (C:\... or \\server\...).
function ConvertTo-FolderPath([string]$Text) {
    $p = [Environment]::ExpandEnvironmentVariables($Text.Trim().Trim('"'))
    if ($p -eq '~' -or $p -like '~\*' -or $p -like '~/*') { $p = $env:USERPROFILE + $p.Substring(1) }
    if ($p -notmatch '^[A-Za-z]:[\\/]' -and $p -notmatch '^\\\\[^\\]') { return $null }
    try { return [IO.Path]::GetFullPath($p).TrimEnd('\') } catch { return $null }
}

# Fresh setup: the folder picker opens straight away. Either way the result is
# confirmed at a prompt that also takes B (browse again) or a typed path.
function Read-DownloadFolder([string]$Current, [string]$Suggested) {
    $default = $Current
    if (-not $default) {
        Say '    Choose the folder downloads are saved into (a folder picker is opening) ...'
        $picked = Select-FolderDialog $Suggested
        $default = if ($picked) { $picked } else { $Suggested }
    }
    while ($true) {
        $ans = Read-Host "    Download folder [$default] (Enter = use it, B = browse, or type a path)"
        if ($ans -match '^\s*[Bb]\s*$') {
            $picked = Select-FolderDialog $default
            if ($picked) { $default = $picked }
            continue
        }
        $text = if ([string]::IsNullOrWhiteSpace($ans)) { $default } else { $ans }
        $full = ConvertTo-FolderPath $text
        if (-not $full) {
            Say '    Please enter a full path, e.g. C:\Users\you\Downloads\Async Storage'
            continue
        }
        if (Test-Path -LiteralPath $full -PathType Leaf) {
            Say '    That is a file, not a folder.'
            continue
        }
        return $full
    }
}

function Test-Server([string]$Url, [string]$Key) {
    $out = Invoke-VenvPython $TestServerPy @{ AS_URL = $Url; AS_KEY = $Key }
    $status = if ($out.Count -ge 1) { "$($out[0])".Trim() } else { 'UNREACHABLE' }
    $detail = if ($out.Count -ge 2) { "$($out[1])".Trim() } else { '' }
    return @{ Status = $status; Detail = $detail }
}

# =============================================================================
# Start
# =============================================================================
if (-not (Test-Path -LiteralPath (Join-Path $Root 'client\gui.py'))) {
    Die "can't find the 'client\' package next to this script. Run it from your checkout ($Root)."
}
if ([Console]::IsInputRedirected) { Die 'this setup asks questions - run it from a terminal or double-click install-client.bat.' }
Set-Location -LiteralPath $Root

Say 'Async Storage desktop client setup'
Say "    folder:  $Root"
Say '    Press Enter to accept the default shown in [brackets].'

# =============================================================================
Step 1 'Python and dependencies'
# =============================================================================
$venvInfo = if (Test-Path -LiteralPath $VenvPy) { Get-PythonInfo $VenvPy } else { $null }
if ($venvInfo -and -not (Test-PythonVersion $venvInfo)) {
    Warn "The existing venv\ uses Python $($venvInfo.Version); the client needs 3.11 or newer."
    $venvInfo = $null
}
if (-not $venvInfo -and (Test-Path -LiteralPath $VenvDir)) {
    if (-not (Confirm-Choice 'venv\ is broken or too old. Delete and recreate it?' 'Y')) {
        Die 'keeping the old venv\ - remove it yourself and re-run.'
    }
    Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $VenvDir) { Die "couldn't delete venv\. Close the app if it's running and re-run." }
}

if ($venvInfo) {
    Say "    Using the existing venv\ (Python $($venvInfo.Version))."
} else {
    $script:OldPython = $null
    $base = Find-Python
    if (-not $base) {
        if ($script:OldPython) {
            Say "    Found Python $($script:OldPython.Version) at $($script:OldPython.Exe), but the client needs 3.11 or newer."
        } else {
            Say '    No Python 3.11+ found.'
        }
        if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
            Die 'winget is not available to install it. Install Python 3.11+ from https://www.python.org/downloads/ (tick "Add python.exe to PATH"), then re-run.'
        }
        if (-not (Confirm-Choice "Install Python with winget now ($WingetPythonId, just for your user)?" 'Y')) {
            Die 'Python 3.11+ is required. Install it from https://www.python.org/downloads/ and re-run.'
        }
        & winget install --id $WingetPythonId --exact --scope user
        Update-SessionPath
        $base = Find-Python
        if (-not $base) { Die 'Python still not found after installing. Open a new terminal and re-run this setup.' }
    }
    Say "    Found Python $($base.Version) at $($base.Exe)."
    Say '    Creating venv\ ...'
    & $base.Exe -m venv $VenvDir
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $VenvPy)) { Die "couldn't create the virtualenv in $VenvDir." }
}
if (-not (Test-Path -LiteralPath $VenvPyw)) { Die "venv\ has no pythonw.exe; delete $VenvDir and re-run." }

if (-not (Test-VenvImport 'tkinter')) {
    Die 'this Python was installed without Tk, which the desktop app needs. Re-run the Python installer, choose Modify, tick "tcl/tk and IDLE", then re-run this setup.'
}

# Only the client's packages; requirements.txt also lists the server's. The
# version pins are taken from it so they live in one place.
$reqs = @(Get-Content -LiteralPath (Join-Path $Root 'requirements.txt') |
    ForEach-Object { ($_ -replace '#.*$', '').Trim() } | Where-Object { $_ })
$httpxReq = @($reqs | Where-Object { $_ -match '^httpx\b' })[0]
$dndReq = @($reqs | Where-Object { $_ -match '^tkinterdnd2\b' })[0]
if (-not $httpxReq) { $httpxReq = 'httpx' }
if (-not $dndReq) { $dndReq = 'tkinterdnd2' }

Say '    Installing requirements (this can take a minute) ...'
& $VenvPy -m pip install --disable-pip-version-check -q $httpxReq
if (-not (Test-VenvImport 'httpx')) { Die "couldn't install httpx. Check your internet connection and re-run." }
& $VenvPy -m pip install --disable-pip-version-check -q $dndReq
if (-not (Test-VenvImport 'tkinterdnd2')) {
    Warn "Couldn't install tkinterdnd2, so drag-and-drop uploads are off. The app's 'Add files...' button still works."
}
Say '    OK - dependencies ready.'

# =============================================================================
Step 2 'Settings'
# =============================================================================
if ($env:STORAGE_CLIENT_CONFIG) {
    Warn "STORAGE_CLIENT_CONFIG is set ($env:STORAGE_CLIENT_CONFIG), so the app reads that file, not the config.toml written here. Clear it to use this setup's settings."
}
$current = @{ url = ''; key = ''; dir = '' }
if (Test-Path -LiteralPath $ConfigPath) {
    $json = @(Invoke-VenvPython $ReadConfigPy @{ AS_CFG = $ConfigPath }) | Select-Object -Last 1
    try {
        $parsed = $json | ConvertFrom-Json
        $current = @{ url = $parsed.url; key = $parsed.key; dir = $parsed.dir }
        Say '    Found an existing config.toml - its settings are the defaults.'
    } catch {
        Warn "Couldn't read the existing config.toml; starting from scratch (it's replaced only if you confirm)."
    }
}
if ($current.key -eq $PlaceholderKey) { $current.key = '' }

$url = Read-ServerUrl $(if ($current.url) { $current.url } else { $DefaultUrl })
$key = Read-ApiKey $current.key
$downloadDir = Read-DownloadFolder $current.dir (Join-Path (Get-DownloadsFolder) 'Async Storage')

$connection = ''
while ($true) {
    Say "    Testing the connection to $url ..."
    $r = Test-Server $url $key
    if ($r.Status -eq 'OK') {
        $connection = "OK - the server has $($r.Detail) file(s)"
        Say "    OK - connected; the server has $($r.Detail) file(s)."
        break
    }
    switch ($r.Status) {
        'BADKEY' {
            Warn 'The server answered, but it rejected the API key.'
            Say "      Copy [server].api_key from server-config.toml on the server (or the key the server installer printed)."
            $connection = 'FAILED - wrong API key'
        }
        'NOTSERVER' {
            Warn "Something answered at $url ($($r.Detail)), but it isn't an Async Storage server."
            Say '      Check the address and port.'
            $connection = 'FAILED - not an Async Storage server'
        }
        default {
            Warn "Can't reach the server: $($r.Detail)"
            Say '      Is the server running? Is Tailscale connected on this machine?'
            Say "      Is the server's [server].host set to its Tailscale IP rather than 127.0.0.1?"
            $connection = 'FAILED - server unreachable'
        }
    }
    $choice = Ask 'R = re-enter the URL and key, K = keep these settings anyway' 'R'
    if ($choice -match '^[Kk]') { break }
    $url = Read-ServerUrl $url
    $key = Read-ApiKey $key
}

# =============================================================================
Step 3 'Desktop shortcut'
# =============================================================================
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Join-Path $desktop $ShortcutName
$existingTarget = ''
if (Test-Path -LiteralPath $shortcutPath) {
    $old = $shell.CreateShortcut($shortcutPath)
    $existingTarget = "$($old.TargetPath) $($old.Arguments)".Trim()
    Say "    There's already an '$ShortcutName' on your desktop, pointing to:"
    Say "      $existingTarget"
}
$makeShortcut = Confirm-Choice "Create an 'Async Storage' shortcut on your desktop?" 'Y'

# =============================================================================
Step 4 'Review'
# =============================================================================
Say '    The setup will:'
Say "      - write $ConfigPath"
Say "          server URL:       $url"
Say "          API key:          $(Hide-Key $key)"
Say "          download folder:  $downloadDir$(if (-not (Test-Path -LiteralPath $downloadDir)) { '  (will be created)' })"
Say "          connection test:  $connection"
if ($makeShortcut) {
    $verb = if ($existingTarget) { 'replace' } else { 'create' }
    Say "      - $verb the desktop shortcut $shortcutPath"
} else {
    Say '      - not create a desktop shortcut'
}
Say ''
if (-not (Confirm-Choice 'Go ahead?' 'Y')) {
    Say 'Stopped. Nothing was changed apart from the Python environment (venv\).'
    exit 0
}

# =============================================================================
Step 5 'Applying'
# =============================================================================
try {
    New-Item -ItemType Directory -Force -Path $downloadDir -ErrorAction Stop | Out-Null
} catch {
    Die "couldn't create the download folder ${downloadDir}: $($_.Exception.Message)"
}
Say "    OK - download folder ready."

Invoke-VenvPython $SaveConfigPy @{ AS_URL = $url; AS_KEY = $key; AS_DIR = $downloadDir; AS_CFG = $ConfigPath } | Out-Null
if ($LASTEXITCODE -ne 0) { Die "couldn't write $ConfigPath." }
Say "    OK - wrote config.toml."

if ($makeShortcut) {
    try {
        $lnk = $shell.CreateShortcut($shortcutPath)
        $lnk.TargetPath = $VenvPyw
        $lnk.Arguments = '-m client.gui'
        $lnk.WorkingDirectory = $Root
        $lnk.IconLocation = "$IconPath,0"
        $lnk.Description = 'Async Storage desktop client'
        $lnk.Save()
        Say "    OK - created the desktop shortcut."
    } catch {
        Warn "Couldn't create the desktop shortcut: $($_.Exception.Message)"
    }
}

Say ''
Say 'Done.'
if ($makeShortcut) {
    Say "    Start the app from the 'Async Storage' shortcut on your desktop."
} else {
    Say "    Start the app from this folder with:  venv\Scripts\pythonw.exe -m client.gui"
}
Say '    Re-run install-client.bat any time to change settings; uninstall-client.bat removes it.'
if (Confirm-Choice 'Start Async Storage now?' 'Y') {
    Start-Process -FilePath $VenvPyw -ArgumentList '-m', 'client.gui' -WorkingDirectory $Root
}

# FPV Sync installer for Premiere Pro (Windows). Run install.bat, or:
#   powershell -ExecutionPolicy Bypass -File install.ps1
[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:APPDATA 'Adobe\CEP\extensions\fpvsync'),
    [switch]$NoRegistry          # testing only: leave the Premiere setting alone
)
$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'fpvsync'
function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }

Say "`nFPV Sync - installing the Premiere Pro panel`n" Cyan

# 1. Copy the panel (an update keeps your settings and the analysis cache)
if (-not (Test-Path (Join-Path $src 'CSXS\manifest.xml'))) {
    Say "Can't find the 'fpvsync' folder next to this script. Unzip the whole download first." Red
    exit 1
}
if ((Test-Path $Destination) -and ((Get-Item $Destination -Force).Attributes -match 'ReparsePoint')) {
    Say "$Destination is a link to a development copy; leaving it alone." Yellow
    exit 1
}
New-Item -ItemType Directory -Force -Path $Destination | Out-Null
$keepConfig = Test-Path (Join-Path $Destination 'config.json')
Get-ChildItem $src | Where-Object { -not ($keepConfig -and $_.Name -eq 'config.json') } |
    Copy-Item -Destination $Destination -Recurse -Force
Say "[ok] Panel copied to $Destination" Green

# 2. Let Premiere load a panel that isn't signed by Adobe (per-user setting, every CEP version)
if (-not $NoRegistry) { foreach ($v in 9..12) {
    $key = "HKCU:\Software\Adobe\CSXS.$v"
    New-Item -Path $key -Force | Out-Null
    Set-ItemProperty -Path $key -Name 'PlayerDebugMode' -Value '1' -Type String
}
    Say "[ok] Premiere allowed to load the panel (PlayerDebugMode)" Green
}

# 3. Python 3 with numpy
function Find-Python {
    $tries = @(
        @{ exe = 'py'; pre = @('-3') },
        @{ exe = 'python'; pre = @() },
        @{ exe = 'python3'; pre = @() }
    )
    foreach ($t in $tries) {
        try {
            $exe = & $t.exe @($t.pre) -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $exe) {
                $exe = "$exe".Trim()
                # skip the Microsoft Store placeholder that only opens the Store
                if ((Test-Path $exe) -and ($exe -notmatch 'WindowsApps')) { return $exe }
            }
        } catch {}
    }
    return $null
}
$py = Find-Python
if (-not $py) {
    Say "[!!] Python 3 not found. Install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then run install.bat again." Yellow
} else {
    & $py -c "import numpy" 2>$null
    if ($LASTEXITCODE -ne 0) {
        Say "     Installing numpy for $py ..."
        & $py -m pip install --user numpy
    }
    & $py -c "import numpy" 2>$null
    if ($LASTEXITCODE -eq 0) { Say "[ok] Python with numpy: $py" Green }
    else { Say "[!!] numpy didn't install. Try:  `"$py`" -m pip install numpy" Yellow }
    $cfgPath = Join-Path $Destination 'config.json'
    $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
    $cfg.python = $py
    [IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 5))
}

# 4. ffmpeg and ffprobe
$ff = Get-Command ffmpeg -ErrorAction SilentlyContinue
$fp = Get-Command ffprobe -ErrorAction SilentlyContinue
if ($ff -and $fp) { Say "[ok] ffmpeg found: $($ff.Source)" Green }
else {
    Say "[!!] ffmpeg isn't installed (or not on PATH). Easiest: open a terminal and run" Yellow
    Say "         winget install Gyan.FFmpeg" Yellow
    Say "     then restart the PC (or at least Premiere) so it's found." Yellow
}

Say "`nDone. Restart Premiere Pro, then in its top menu bar open:  Window > Extensions > FPV Sync`n" Cyan

<#
.SYNOPSIS
    Install the XAMS slow-control services as Windows services, under NSSM.

.DESCRIPTION
    Run in an ELEVATED PowerShell from the repository root:

        .\tools\install_services.ps1              # start at boot (production)
        .\tools\install_services.ps1 -Manual      # restart on crash, not at boot
        .\tools\install_services.ps1 -Uninstall   # remove them again
        .\tools\install_services.ps1 -Only turbo  # add or update ONE service,
                                                  # leaving the others running

    -Only touches nothing but the services named: no other service is stopped,
    no other process is killed and no other lock is removed. It is how a new
    driver is added to a running system without a gap in the data or in the
    alarms.

    Idempotent: re-running updates the existing services in place.

    WHAT THIS CHANGES, AND WHY IT MATTERS (DESIGN.md §12).

    Until now the services were plain processes started by `xams-ctl`, which
    meant a reboot left the lab PC with a broker, a database and a Grafana —
    and nothing acquiring, storing or alarming. Nothing would have said so,
    because the thing that would say so was also down.

    §12 keeps auto-start off "while LabVIEW is the fallback", because every
    device admits one process and a service starting at boot would claim the
    hardware and lock LabVIEW out. That condition has passed: LabVIEW is
    closed, this system holds all four instruments, and real alarm thresholds
    depend on it.

    GOING BACK TO LABVIEW is therefore a deliberate action, and one command:

        xams-ctl stop --for-labview

    That stops the services AND suspends their auto-start, so a reboot does
    not quietly take the hardware back. `xams-ctl start` restores both.
#>

[CmdletBinding()]
param(
    [switch]$Manual,
    [switch]$Uninstall,
    [string[]]$Only,
    [string]$NssmVersion = "2.24"
)

$ErrorActionPreference = "Stop"

$RepoRoot   = Split-Path -Parent $PSScriptRoot
$Python     = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogDir     = Join-Path $RepoRoot "logs"
$StagingDir = Join-Path $PSScriptRoot "installers"
$NssmExe    = Join-Path $StagingDir "nssm.exe"
$Prefix     = "XAMS-"

# name -> the module it runs. Order matters only for readability; the real
# ordering is expressed as service dependencies below.
$Services = [ordered]@{
    "sinks"     = @("-m", "xams_sc.sinks")
    "cdaq"      = @("-m", "xams_sc.devices", "cdaq")
    "caen"      = @("-m", "xams_sc.devices", "caen")
    "lakeshore" = @("-m", "xams_sc.devices", "lakeshore")
    "ups"       = @("-m", "xams_sc.devices", "ups")
    "turbo"     = @("-m", "xams_sc.devices", "turbo")
    "rga"       = @("-m", "xams_sc.devices", "rga")
    "scale"     = @("-m", "xams_sc.devices", "scale")
    "derived"   = @("-m", "xams_sc.devices", "derived")
    "alarms"    = @("-m", "xams_sc.alarms")
    "webui"     = @("-m", "xams_sc.api")
}

if ($Only) {
    foreach ($n in $Only) {
        if (-not $Services.Contains($n)) {
            Write-Host "  FAIL unknown service '$n'; known: $($Services.Keys -join ', ')" -ForegroundColor Red
            exit 1
        }
    }
    foreach ($n in @($Services.Keys)) {
        if ($Only -notcontains $n) { $Services.Remove($n) }
    }
}

function Say  ($m) { Write-Host "  $m" }
function Step ($m) { Write-Host "`n=== $m ===" -ForegroundColor Cyan }
function Good ($m) { Write-Host "  OK   $m" -ForegroundColor Green }
function Fail ($m) { Write-Host "  FAIL $m" -ForegroundColor Red }

# ---------------------------------------------------------------- preconditions

$identity  = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal $identity
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail "This script must run elevated."
    Say  "Open PowerShell as Administrator, cd to $RepoRoot, and run it again."
    exit 1
}

if (-not (Test-Path $Python)) {
    Fail "No virtual environment at $Python"
    Say  "Create it first:  python -m venv .venv"
    exit 1
}

# ---------------------------------------------------------------- uninstall

if ($Uninstall) {
    Step "Removing the XAMS services"
    foreach ($name in $Services.Keys) {
        $svc = "$Prefix$name"
        if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
            Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue
            & $NssmExe remove $svc confirm | Out-Null
            Say "removed $svc"
        }
    }
    Good "done. The services are gone; xams-ctl will run them as plain processes again."
    exit 0
}

# ---------------------------------------------------------------- nssm

Step "NSSM"

if (-not (Test-Path $NssmExe)) {
    New-Item -ItemType Directory -Force -Path $StagingDir | Out-Null
    $zip = Join-Path $StagingDir "nssm.zip"
    $url = "https://nssm.cc/release/nssm-$NssmVersion.zip"
    Say "downloading $url ..."
    try {
        $ProgressPreference = "SilentlyContinue"
        Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing -TimeoutSec 300
    } catch {
        Fail "could not download NSSM"
        Say  "Fetch $url by hand, unpack it, and put nssm.exe at $NssmExe"
        exit 1
    }
    $unpack = Join-Path $StagingDir "nssm-unpack"
    Expand-Archive -Path $zip -DestinationPath $unpack -Force
    # win64 on any 64-bit machine; the zip carries both.
    $found = Get-ChildItem $unpack -Recurse -Filter nssm.exe |
             Where-Object { $_.FullName -match 'win64' } | Select-Object -First 1
    if (-not $found) {
        Fail "nssm.exe not found inside the archive"
        exit 1
    }
    Copy-Item $found.FullName $NssmExe -Force
    Remove-Item $zip, $unpack -Recurse -Force -ErrorAction SilentlyContinue
}
Good "nssm at $NssmExe"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

# ---------------------------------------------------------------- install

$startType = if ($Manual) { "SERVICE_DEMAND_START" } else { "SERVICE_AUTO_START" }
Step "Installing services ($startType)"

foreach ($name in $Services.Keys) {
    $svc  = "$Prefix$name"
    $args = $Services[$name] -join " "

    if (Get-Service -Name $svc -ErrorAction SilentlyContinue) {
        Stop-Service -Name $svc -Force -ErrorAction SilentlyContinue
        & $NssmExe remove $svc confirm | Out-Null
        Start-Sleep -Milliseconds 400
    }

    & $NssmExe install $svc $Python $args | Out-Null
    & $NssmExe set $svc AppDirectory $RepoRoot | Out-Null
    & $NssmExe set $svc DisplayName "XAMS slow control - $name" | Out-Null
    & $NssmExe set $svc Description "XAMS slow control $name service. See DESIGN.md." | Out-Null
    & $NssmExe set $svc Start $startType | Out-Null

    # Everything needs the broker. The sinks also need the database, and
    # starting before it is up just means a minute of retry noise in the log.
    # One argument per dependency. A single "a b" string makes NSSM look for a
    # service of that whole name, which fails and leaves the service with NO
    # dependencies at all - silently, because the error is only printed.
    $depends = @("mosquitto")
    if ($name -eq "sinks") { $depends = @("mosquitto", "postgresql-x64-18") }
    & $NssmExe set $svc DependOnService @depends | Out-Null

    # RESTART ON CRASH, WITH A THROTTLE.
    #
    # A service that cannot start - a missing instrument, a bad config - would
    # otherwise be restarted forever, several times a second, filling the disk
    # with logs and hiding the real error. 10 s between attempts, and NSSM
    # stops retrying anything that dies within 30 s of starting often enough
    # to look hopeless.
    & $NssmExe set $svc AppExit Default Restart | Out-Null
    & $NssmExe set $svc AppRestartDelay 10000 | Out-Null
    & $NssmExe set $svc AppThrottle 30000 | Out-Null

    # The services already log to logs\<name>.log themselves; this captures
    # anything that escapes logging entirely, such as an import error.
    & $NssmExe set $svc AppStdout (Join-Path $LogDir "$name.service.log") | Out-Null
    & $NssmExe set $svc AppStderr (Join-Path $LogDir "$name.service.log") | Out-Null
    & $NssmExe set $svc AppRotateFiles 1 | Out-Null
    & $NssmExe set $svc AppRotateBytes 10485760 | Out-Null

    Say "installed $svc"
}

Good "$($Services.Count) services installed"

# ----------------------------------------------------------------- manual

# The manual is served by the API at /manual, from src/xams_sc/api/site. That
# directory is generated and not in git, so it must be built here or the link
# in the web UI leads nowhere. Never fatal: a missing manual is a nuisance,
# and refusing to install the monitoring over it would not be.

Step "Building the manual"

& $Python (Join-Path $RepoRoot "tools\build_docs.py")
if ($LASTEXITCODE -eq 0) {
    Good "manual built; it is served at http://127.0.0.1:8000/manual"
} else {
    Fail "the manual did not build - /manual will return 404"
    Say  "Install the documentation tools and try again:"
    Say  "    .\.venv\Scripts\python.exe -m pip install -e `".[docs]`""
    Say  "    .\.venv\Scripts\python.exe tools\build_docs.py"
}

# ---------------------------------------------------------------- start

Step "Starting"

# Stop anything xams-ctl left running as a plain process, or the new services
# hit their own single-instance locks and refuse. With -Only, only the named
# services' processes and locks: everything else keeps running.
if ($Only) {
    foreach ($name in $Services.Keys) {
        Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { ($_.CommandLine -join " ") -match ("xams_sc\S*\s+" + $name + "(\s|$)") } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
        Remove-Item (Join-Path $LogDir "$name.lock") -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Seconds 1
} else {
    Get-CimInstance Win32_Process -Filter "name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -match 'xams_sc' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 3
    Get-ChildItem (Join-Path $LogDir "*.lock") -ErrorAction SilentlyContinue |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

foreach ($name in $Services.Keys) {
    $svc = "$Prefix$name"
    try {
        Start-Service -Name $svc -ErrorAction Stop
        Say "started $svc"
    } catch {
        Fail "$svc did not start: $($_.Exception.Message.Split([char]10)[0])"
        Say  "Look in $LogDir\$name.log and $LogDir\$name.service.log"
    }
    Start-Sleep -Milliseconds 800
}

# ---------------------------------------------------------------- verify

Step "Verification"

$bad = @()
foreach ($name in $Services.Keys) {
    $svc = Get-Service -Name "$Prefix$name" -ErrorAction SilentlyContinue
    if (-not $svc) { $bad += $name; Fail "$name not installed"; continue }
    $line = "{0,-12} {1,-8} startup {2}" -f $name, $svc.Status, $svc.StartType
    if ($svc.Status -eq "Running") { Good $line } else { $bad += $name; Fail $line }
}

Write-Host ""
if ($bad.Count -eq 0) {
    Good "All services running."
    if ($Manual) {
        Say ""
        Say "Startup is MANUAL: they restart on crash but NOT at boot."
        Say "Re-run without -Manual when you want them to start automatically."
    } else {
        Say ""
        Say "Startup is AUTOMATIC: after a reboot the whole stack comes back"
        Say "on its own, and a crashed service restarts within 10 seconds."
    }
    Say ""
    Say "To hand the hardware back to LabVIEW:"
    Say "    .\.venv\Scripts\python.exe -m xams_sc.cli.xams_ctl stop --for-labview"
    Say ""
    Say "That stops the services AND suspends their auto-start, so a reboot"
    Say "does not quietly take the instruments back. `xams-ctl start` restores"
    Say "both."
    exit 0
} else {
    Fail "not running: $($bad -join ', ')"
    exit 1
}

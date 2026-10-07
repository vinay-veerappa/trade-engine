# P4c runtime-owner launcher: hosts one owner and holds its stdin open

<#
.SYNOPSIS
    Launch one P4c runtime owner and keep its stdin open while it serves.

.DESCRIPTION
    The runtime owner is `te.exe serve --config <config.json>`: it binds
    loopback HTTP on the configured port, serves /v1/runtime control requests
    for the configured jobs, and exits gracefully when its stdin closes
    (draining the active job first). A scheduled task gives a process NUL for
    stdin, which ends the serve at once, so this launcher starts the owner with
    a pipe for stdin and holds the write end for as long as the launcher lives.
    If the launcher dies the pipe closes and the owner drains and exits.

    Everything the owner prints is appended to
    <config dir>\logs\trade_engine\RuntimeOwner_<Role>_<yyyy-MM-dd>.log. The
    config sits in an ancestor of its ledger (the engine requires that), which
    is the client repository root, so the owner's log lands beside the tasks'.

    Once the owner reports it is serving, the launcher writes its address to
    <EndpointDir>\<Role>.json: {role, port, generation, config, pid, started}.
    That is NOT a secret (the capability stays in its own file, named by the
    config). The client task wrapper reads it to point the jobs at the owner,
    because the generation is a digest only the owner computes.

    A graceful stop: create <EndpointDir>\<Role>.stop. The launcher closes the
    owner's stdin (it drains the active job and exits 0). Stop-ScheduledTask
    ends the process tree at once with no drain; a restart then marks the
    unfinished job `uncertain`, to be resubmitted under a new request id.

    The launcher exits with the owner's exit code (0 on a graceful stop) and
    removes the endpoint file.

.PARAMETER Config
    The absolute path of the runtime-owner config JSON.

.PARAMETER Role
    The role name used in log file names, the endpoint file and the stop file.

.PARAMETER LogDir
    Where the log goes. Default: <config dir>\logs\trade_engine.

.PARAMETER EndpointDir
    Where the endpoint and stop files go. Default: <LogDir>\runtime-owners.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File launch\runtime\run_runtime_owner.ps1 -Config C:\abs\path\owner.json -Role batch
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$Role,
    [string]$LogDir,
    [string]$EndpointDir
)

$ErrorActionPreference = "Stop"
if (-not [System.IO.Path]::IsPathRooted($Config)) {
    Write-Error "the runtime owner config must be an absolute path (got: $Config)"
}
if (-not $LogDir) { $LogDir = Join-Path (Split-Path -Parent $Config) "logs\trade_engine" }
if (-not $EndpointDir) { $EndpointDir = Join-Path $LogDir "runtime-owners" }
New-Item -ItemType Directory -Force $LogDir | Out-Null
New-Item -ItemType Directory -Force $EndpointDir | Out-Null
$Log = Join-Path $LogDir ("RuntimeOwner_{0}_{1:yyyy-MM-dd}.log" -f $Role, (Get-Date))
$EndpointFile = Join-Path $EndpointDir "$Role.json"
$StopFile = Join-Path $EndpointDir "$Role.stop"

"=== {0:o} runtime-owner start role={1} config={2}" -f (Get-Date), $Role, $Config |
    Add-Content -Path $Log -Encoding UTF8
if (-not (Test-Path $Config)) {
    "config not found at $Config" | Add-Content -Path $Log -Encoding UTF8
    exit 1
}

# TE_BINARY is the absolute path of the certified release te.exe.
if (-not $env:TE_BINARY -or -not (Test-Path $env:TE_BINARY)) {
    "TE_BINARY not found at $env:TE_BINARY" | Add-Content -Path $Log -Encoding UTF8
    exit 1
}
Remove-Item $StopFile, $EndpointFile -ErrorAction SilentlyContinue

# What the owner prints from here on starts at this offset of the log.
$Offset = (Get-Item $Log).Length

$info = New-Object System.Diagnostics.ProcessStartInfo
$info.FileName = $env:ComSpec
$info.Arguments = '/d /c ""{0}" serve --config "{1}" >> "{2}" 2>&1"' -f $env:TE_BINARY, $Config, $Log
$info.UseShellExecute = $false
$info.CreateNoWindow = $true
$info.RedirectStandardInput = $true
$Owner = [System.Diagnostics.Process]::Start($info)

function Read-OwnerOutput {
    $stream = [System.IO.File]::Open($Log, "Open", "Read", "ReadWrite")
    try {
        $null = $stream.Seek($Offset, "Begin")
        (New-Object System.IO.StreamReader($stream)).ReadToEnd()
    } finally {
        $stream.Dispose()
    }
}

$Published = $false
$StopRequested = $false
while (-not $Owner.WaitForExit(500)) {
    if (-not $Published) {
        # The owner's first line of output says it is serving, with its bound port.
        foreach ($line in (Read-OwnerOutput) -split "`r?`n") {
            if ($line -notmatch '"serving":\s*true') { continue }
            try { $serving = $line | ConvertFrom-Json } catch { continue }
            [ordered]@{
                role       = $Role
                port       = $serving.port
                generation = $serving.generation
                config     = $Config
                pid        = $Owner.Id
                started    = (Get-Date -Format o)
            } | ConvertTo-Json | Set-Content -Path $EndpointFile -Encoding UTF8
            $Published = $true
            break
        }
    }
    if (-not $StopRequested -and (Test-Path $StopFile)) {
        $Owner.StandardInput.Close()
        # The owner's redirect holds the log open; this note is best effort.
        try {
            "=== {0:o} stop requested; the owner's stdin is closed" -f (Get-Date) |
                Add-Content -Path $Log -Encoding UTF8
        } catch { }
        $StopRequested = $true
    }
}
$code = $Owner.ExitCode
Remove-Item $StopFile, $EndpointFile -ErrorAction SilentlyContinue
"=== runtime-owner exit $code role=$Role" | Add-Content -Path $Log -Encoding UTF8
exit $code

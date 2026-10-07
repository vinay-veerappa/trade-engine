# P4c T14 staged runtime-owner launcher (template; staged only, never registered)

<#
.SYNOPSIS
    Launch one staged P4c runtime owner and keep its stdin open while it serves.

.DESCRIPTION
    This is the STAGED launcher the P4c T14 rollout kit renders. It is not
    registered with Task Scheduler by anything in this repository: the kit's
    dry-run tests fake every scheduler command, and applying the kit needs a
    separately approved canary (plan section 7, Stage D).

    The runtime owner is `te.exe serve --config <config.json>`: it binds
    loopback HTTP on the configured port, serves /v1/runtime control requests
    for the configured jobs, and exits gracefully when its stdin closes
    (draining the active job first). This launcher therefore runs the owner
    with stdin attached, logs everything the owner prints to
    logs\trade_engine\<Role>_<yyyy-MM-dd>.log, and exits with the owner's
    exit code (0 on a graceful stdin-close stop).

    The per-ledger selector (launch\runtime\runtime_selector.json) decides
    which owner a role uses. Its default is "legacy": a missing Rust
    installation refuses, it never falls back mid-job. This launcher is only
    reached when the selector already says "runtime" for the ledger.

.PARAMETER Config
    The absolute path of the rendered runtime-owner config JSON.

.PARAMETER Role
    The role name used in log file names and the stop summary.

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File launch\runtime\run_runtime_owner.ps1 -Config C:\abs\path\role-runtime.json -Role options-eod
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$Role
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
$LogDir = Join-Path $RepoRoot "logs\trade_engine"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$Log = Join-Path $LogDir ("RuntimeOwner_{0}_{1:yyyy-MM-dd}.log" -f $Role, (Get-Date))

if (-not [System.IO.Path]::IsPathRooted($Config)) {
    Write-Error "the runtime owner config must be an absolute path (got: $Config)"
}

"=== {0:o} runtime-owner start role={1} config={2}" -f (Get-Date), $Role, $Config |
    Add-Content -Path $Log -Encoding UTF8
if (-not (Test-Path $Config)) {
    "config not found at $Config" | Add-Content -Path $Log -Encoding UTF8
    exit 1
}

# The kit renders TE_BINARY as the absolute path of the certified release te.exe.
if (-not (Test-Path $env:TE_BINARY)) {
    "TE_BINARY not found at $env:TE_BINARY" | Add-Content -Path $Log -Encoding UTF8
    exit 1
}

$ErrorActionPreference = "Continue"
& $env:TE_BINARY serve --config $Config 2>&1 |
    ForEach-Object { "$_" } |
    Add-Content -Path $Log -Encoding UTF8 -PassThru
$code = $LASTEXITCODE
"=== runtime-owner exit $code role=$Role" | Add-Content -Path $Log -Encoding UTF8
exit $code
param(
    [string]$Config = (Join-Path (Split-Path $PSScriptRoot -Parent) 'config\local.launcher.json'),
    [switch]$Check,
    [switch]$Menu,
    [switch]$StartOnly
)
$ErrorActionPreference = 'Stop'
try {
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw 'UPDATE_LAUNCHER_MISSING' }
    if ((Get-Item -LiteralPath $Config).Length -gt 65536) { throw 'UPDATE_LAUNCHER_INVALID' }
    $settings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($settings.python -isnot [string] -or $settings.python -notmatch '^[A-Za-z]:[\\/]') {
        throw 'UPDATE_PYTHON_UNAVAILABLE'
    }
    if (-not (Test-Path -LiteralPath $settings.python -PathType Leaf)) { throw 'UPDATE_PYTHON_UNAVAILABLE' }
    $arguments = @('-I', '-X', 'utf8', '-B', (Join-Path $PSScriptRoot 'update_local.py'), '--config', $Config)
    if ($Check) { $arguments += '--check' }
    if ($Menu) { $arguments += '--menu' }
    if ($StartOnly) { $arguments += '--start-only' }
    & $settings.python @arguments
    exit $LASTEXITCODE
} catch {
    $message = $_.Exception.Message
    if ($message -match '^UPDATE_[A-Z_]+$') { Write-Host $message }
    else { Write-Host 'UPDATE_LAUNCHER_INVALID' }
    exit 78
}

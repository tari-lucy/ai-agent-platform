# Create only a new private binding; never read, display or overwrite a local .env.
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskEnvPath = Join-Path $taskRoot '.env'
if (Test-Path -LiteralPath $taskEnvPath) {
    Write-Output 'Local .env already exists; no values read or changed.'
    exit 0
}
$taskSecret = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant()
$taskBytes = [Text.Encoding]::UTF8.GetBytes("AI_PLATFORM_REDIS_PASSWORD=$taskSecret`n")
$taskStream = [IO.File]::Open($taskEnvPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
try { $taskStream.Write($taskBytes,0,$taskBytes.Length) } finally { $taskStream.Dispose() }
Write-Output 'New local Redis auth binding created; value withheld, file ignored by Git.'

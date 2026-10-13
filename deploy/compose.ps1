param(
    [ValidateSet('config','build','up','stop','ps','restart')][string]$Action = 'config',
    [ValidatePattern('^[a-z0-9][a-z0-9_-]*$')][string]$Project = 'ai-agent-platform',
    [ValidateRange(1024,65535)][int]$Port = 8765
)
$ErrorActionPreference = 'Stop'
# Never read an implicit local .env or print interpolated secret values.
$env:COMPOSE_PARALLEL_LIMIT = '1'
$env:COMPOSE_DISABLE_ENV_FILE = '1'
$env:AI_PLATFORM_CORE_HOST_PORT = $Port.ToString()
$taskEnvPath = Join-Path (Split-Path -Parent $PSScriptRoot) '.env'
if (-not (Test-Path -LiteralPath $taskEnvPath)) { throw 'Run deploy/init-local-secrets.ps1 first' }
$taskArgs = @('compose','--env-file',$taskEnvPath,'-f',(Join-Path $PSScriptRoot 'compose.yaml'),'-p',$Project)
switch ($Action) {
    'config' { docker @taskArgs config --quiet }
    'build' { docker @taskArgs build }
    'up' { docker @taskArgs up -d --wait --wait-timeout 90 --no-build }
    'stop' { docker @taskArgs stop }
    'ps' { docker @taskArgs ps }
    'restart' { docker @taskArgs restart }
}
if ($LASTEXITCODE -ne 0) { throw "Compose $Action failed" }

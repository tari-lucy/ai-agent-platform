# Connect the local TUI to the existing agent server. Never start a local agent.
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$Config,
    [switch]$CheckOnly
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

try {
    $launcher = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    $hostFolder = [string]$launcher.host_root
    if (-not [IO.Path]::IsPathRooted($hostFolder)) { throw 'HOST_PATH_INVALID' }
    $hostConfig = Get-Content -LiteralPath (Join-Path $hostFolder 'host.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if (-not $hostConfig.harness.enabled) { throw 'HARNESS_DISABLED' }
    $serverPort = [int]$hostConfig.harness.port
    if ($serverPort -lt 1024 -or $serverPort -gt 65535) { throw 'PORT_INVALID' }
    $address = 'http://127.0.0.1:' + $serverPort
    $passwordPath = Join-Path $hostFolder 'data/harness/password'
    if (-not (Test-Path -LiteralPath $passwordPath -PathType Leaf)) { throw 'PASSWORD_FILE_MISSING' }

    $clientPath = Join-Path ([Environment]::GetFolderPath('ApplicationData')) 'npm/node_modules/opencode-ai/bin/opencode.exe'
    if (-not (Test-Path -LiteralPath $clientPath -PathType Leaf)) {
        $clientCommand = Get-Command opencode.exe -ErrorAction SilentlyContinue
        if ($null -eq $clientCommand) { throw 'OPENCODE_CLIENT_MISSING' }
        $clientPath = $clientCommand.Source
    }

    $connection = New-Object Net.Sockets.TcpClient
    try {
        $connecting = $connection.ConnectAsync('127.0.0.1', $serverPort)
        if (-not $connecting.Wait(2000) -or -not $connection.Connected) { throw 'SERVER_NOT_READY' }
    } finally {
        $connection.Dispose()
    }

    if ($CheckOnly) {
        Write-Host 'OPENCODE_ATTACH_READY (client, configuration and server port checked)'
        return
    }

    $previousPassword = [Environment]::GetEnvironmentVariable('OPENCODE_SERVER_PASSWORD', 'Process')
    $previousUsername = [Environment]::GetEnvironmentVariable('OPENCODE_SERVER_USERNAME', 'Process')
    $previousAutoUpdate = [Environment]::GetEnvironmentVariable('OPENCODE_DISABLE_AUTOUPDATE', 'Process')
    try {
        $password = [IO.File]::ReadAllText($passwordPath).Trim()
        if ([string]::IsNullOrWhiteSpace($password)) { throw 'PASSWORD_FILE_EMPTY' }
        # Only the child process inherits these; no password in arguments or shell history.
        [Environment]::SetEnvironmentVariable('OPENCODE_SERVER_PASSWORD', $password, 'Process')
        [Environment]::SetEnvironmentVariable('OPENCODE_SERVER_USERNAME', 'opencode', 'Process')
        [Environment]::SetEnvironmentVariable('OPENCODE_DISABLE_AUTOUPDATE', 'true', 'Process')
        Write-Host 'Connecting to the existing agent. Ctrl+C exits the client; STOP is in the panel.'
        & $clientPath attach $address --dir /workspace --pure
    } finally {
        [Environment]::SetEnvironmentVariable('OPENCODE_SERVER_PASSWORD', $previousPassword, 'Process')
        [Environment]::SetEnvironmentVariable('OPENCODE_SERVER_USERNAME', $previousUsername, 'Process')
        [Environment]::SetEnvironmentVariable('OPENCODE_DISABLE_AUTOUPDATE', $previousAutoUpdate, 'Process')
        $password = $null
    }
} catch {
    Write-Host 'Cannot connect to OpenCode. Start Docker Desktop, open the platform panel and press START.' -ForegroundColor Yellow
    Write-Host 'If START already completed, wait for OpenCode to be ready and open this shortcut again.'
    Write-Host 'Panel: http://127.0.0.1:8765/'
    Write-Host 'Nothing was started or changed by this launcher.'
}

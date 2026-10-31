param([Parameter(Mandatory = $true)][string]$Config)
$ErrorActionPreference = 'Stop'
$settings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
if ($settings.user -ne 'aw-game-tunnel') { throw 'Unexpected tunnel account.' }
if ($settings.host -notmatch '^[a-zA-Z0-9.-]+$') { throw 'Invalid SSH host.' }
$port = [int]$settings.port
if ($port -lt 1024 -or $port -gt 65535) { throw 'Invalid port.' }
foreach ($path in @($settings.identity, $settings.known_hosts)) {
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw 'SSH key or known_hosts missing.' }
}
$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
# The server listens on its own loopback only; a relay bridges it to the persona's container.
$forward = '127.0.0.1:{0}:127.0.0.1:{0}' -f $port
$arguments = @(
    '-N', '-n', '-T', '-a', '-x', '-R', $forward,
    '-i', $settings.identity, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
    '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
    '-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$($settings.known_hosts)",
    '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3',
    "$($settings.user)@$($settings.host)"
)
# The scheduled task owns only this loop and its SSH child. No global ssh process killing.
# A dead connection returns here; the server drops its half within about 90 seconds
# (ClientAliveInterval), and ExitOnForwardFailure retries until the listener is free again.
while ($true) {
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $ssh @arguments 2>$null
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    Start-Sleep -Seconds 15
}

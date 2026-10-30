param([Parameter(Mandatory = $true)][string]$Config)
$ErrorActionPreference = 'Stop'
$settings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
if ($settings.user -ne 'aw-telegram-proxy') { throw 'Unexpected proxy account.' }
if ($settings.host -notmatch '^[a-zA-Z0-9.-]+$') { throw 'Invalid SSH host.' }
if ([int]$settings.port -lt 1024 -or [int]$settings.port -gt 65535) { throw 'Invalid local port.' }
if (-not (Test-Path -LiteralPath $settings.identity -PathType Leaf)) { throw 'SSH key missing.' }
$ssh = (Get-Command ssh.exe -ErrorAction Stop).Source
$arguments = @(
    '-N', '-n', '-T', '-a', '-x', '-D', "127.0.0.1:$($settings.port)",
    '-i', $settings.identity, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
    '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
    '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
    '-o', 'ConnectionAttempts=1', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
    "$($settings.user)@$($settings.host)"
)
# The scheduled task owns only this loop and its SSH child. No global ssh process killing.
while ($true) {
    # Windows PowerShell 5 turns native stderr into error records. A broken SSH
    # connection must return to this loop instead of terminating the wrapper.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $ssh @arguments 2>$null
    } finally {
        $ErrorActionPreference = $previousPreference
    }
    Start-Sleep -Seconds 15
}

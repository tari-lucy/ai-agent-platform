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
# State and log live next to this script (runtime\game-tunnel), never in the config folder.
$stateFolder = $PSScriptRoot
$pidFile = Join-Path $stateFolder 'ssh.pid'
$loopPidFile = Join-Path $stateFolder 'loop.pid'
$logFile = Join-Path $stateFolder 'tunnel.log'
# What ssh itself says when it leaves (a taken port, a refused key): kept for the last start only.
$errFile = Join-Path $stateFolder 'ssh.err'

function Write-Log([string]$text) {
    # Times and fixed words only: no host, key, port or account in the log.
    try {
        if ((Test-Path -LiteralPath $logFile) -and ((Get-Item -LiteralPath $logFile).Length -gt 200KB)) {
            Move-Item -LiteralPath $logFile -Destination ($logFile + '.old') -Force
        }
        Add-Content -LiteralPath $logFile -Value ('{0} {1}' -f (Get-Date -Format 's'), $text)
    } catch { }
}

# One loop at a time: a second start (task run twice, a manual run) leaves quietly.
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Global\AIAgentPlatformGameTunnel', [ref]$created)
if (-not $created) { Write-Log 'another loop is already running; exit'; return }

# The loop's own PID: the stop button ends exactly this loop and its ssh, nobody else's process.
Set-Content -LiteralPath $loopPidFile -Value $PID

# Game: the server listens on its own loopback only; a relay bridges it to the persona's container.
$forward = '127.0.0.1:{0}:127.0.0.1:{0}' -f $port
# Extra reverse ports (doc37): VTube Studio and OBS on this computer, the same numbers on the server.
$extra = @()
if ($settings.extra_ports) {
    foreach ($extraPort in @($settings.extra_ports)) {
        $number = [int]$extraPort
        if ($number -lt 1024 -or $number -gt 65535 -or $number -eq $port) { throw 'Invalid extra port.' }
        $extra += @('-R', ('127.0.0.1:{0}:127.0.0.1:{0}' -f $number))
    }
}
# Bridge: the panel and OpenCode of the server, on the same addresses as before (127.0.0.1).
$bridge = @()
if ($settings.bridge_ports) {
    foreach ($bridgePort in @($settings.bridge_ports)) {
        $number = [int]$bridgePort
        if ($number -lt 1024 -or $number -gt 65535) { throw 'Invalid bridge port.' }
        $bridge += @('-L', ('127.0.0.1:{0}:127.0.0.1:{0}' -f $number))
    }
}
$arguments = @(
    '-N', '-n', '-T', '-a', '-x', '-R', $forward) + $extra + $bridge + @(
    '-i', $settings.identity, '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
    '-o', 'PasswordAuthentication=no', '-o', 'KbdInteractiveAuthentication=no',
    '-o', 'StrictHostKeyChecking=yes', '-o', "UserKnownHostsFile=$($settings.known_hosts)",
    '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=15', '-o', 'ServerAliveCountMax=3', '-o', 'TCPKeepAlive=yes',
    "$($settings.user)@$($settings.host)"
)

function Stop-Recorded {
    # Only the ssh this loop started earlier (its recorded PID), and only if it is still an ssh.exe
    # of this tunnel. No search and no kill of anybody else's ssh.
    if (-not (Test-Path -LiteralPath $pidFile)) { return }
    $old = 0
    [void][int]::TryParse(((Get-Content -LiteralPath $pidFile -ErrorAction SilentlyContinue) -join '').Trim(), [ref]$old)
    Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
    if ($old -le 0) { return }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$old" -ErrorAction SilentlyContinue
    if ($process -and $process.Name -eq 'ssh.exe' -and $process.CommandLine -like "*$($settings.user)@*") {
        Stop-Process -Id $old -Force -ErrorAction SilentlyContinue
        Write-Log "stopped the earlier ssh $old"
    }
}

function Server-Connection([int]$processId) {
    # The ssh's own TCP connection to the server: the state tells a live tunnel from a dead one.
    Get-NetTCPConnection -OwningProcess $processId -RemotePort 22 -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty State
}

$delay = 5
try {
    Stop-Recorded
    while ($true) {
        $started = Get-Date
        $quoted = $arguments | ForEach-Object { if ($_ -match '\s') { '"' + $_ + '"' } else { $_ } }
        $child = Start-Process -FilePath $ssh -ArgumentList $quoted -WindowStyle Hidden -PassThru `
            -RedirectStandardError $errFile
        Set-Content -LiteralPath $pidFile -Value $child.Id
        Write-Log "ssh started $($child.Id)"
        $bad = 0
        while (-not $child.HasExited) {
            Start-Sleep -Seconds 10
            if ($child.HasExited) { break }
            # A connection that the server has already closed (CLOSE_WAIT and the like) or that
            # is gone: the ssh process does not leave by itself (a known OpenSSH-on-Windows
            # hang), and it keeps the local ports. Three bad looks in a row: restart it.
            $state = Server-Connection $child.Id
            if ($state -eq 'Established') { $bad = 0 } else { $bad++ }
            if ($bad -ge 3 -and ((Get-Date) - $started).TotalSeconds -gt 40) {
                Write-Log "dead connection (state '$state'); restart"
                Stop-Process -Id $child.Id -Force -ErrorAction SilentlyContinue
                break
            }
        }
        Remove-Item -LiteralPath $pidFile -Force -ErrorAction SilentlyContinue
        # Quick exits (the server is unreachable, the listener is not free yet) back off up to 15
        # seconds, so a returning computer takes its ports back soon after the server lets go;
        # a tunnel that lived a while starts again after five seconds.
        if (((Get-Date) - $started).TotalSeconds -gt 120) { $delay = 5 } else { $delay = [Math]::Min($delay * 2, 15) }
        Write-Log "ssh ended; next start in $delay s"
        Start-Sleep -Seconds $delay
    }
} finally {
    Stop-Recorded
    Remove-Item -LiteralPath $loopPidFile -Force -ErrorAction SilentlyContinue
    $mutex.ReleaseMutex()
    $mutex.Dispose()
}

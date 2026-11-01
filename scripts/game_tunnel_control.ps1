# Start or stop the game tunnel by hand (the desktop icons call this).
#   -Action start | stop
# Only this tunnel's own loop and ssh (the PIDs the loop wrote itself) are ever stopped.
param(
    [Parameter(Mandatory = $true)][ValidateSet('start', 'stop')][string]$Action,
    [string]$Config = 'C:\ai-pers2\config\game-tunnel\local.game-tunnel.json'
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
function Say([string]$text) {
    [void][System.Windows.Forms.MessageBox]::Show($text, 'Туннель для игры')
}
$folder = $PSScriptRoot
$loopPidFile = Join-Path $folder 'loop.pid'
$sshPidFile = Join-Path $folder 'ssh.pid'
$taskName = 'AI Agent Platform - Game Tunnel'

function Read-Pid([string]$file) {
    if (-not (Test-Path -LiteralPath $file)) { return 0 }
    $value = 0
    [void][int]::TryParse(((Get-Content -LiteralPath $file -ErrorAction SilentlyContinue) -join '').Trim(), [ref]$value)
    return $value
}
function Own-Process([int]$processId, [string]$nameLike, [string]$commandLike) {
    if ($processId -le 0) { return $null }
    $process = Get-CimInstance Win32_Process -Filter "ProcessId=$processId" -ErrorAction SilentlyContinue
    if ($process -and $process.Name -like $nameLike -and $process.CommandLine -like $commandLike) { return $process }
    return $null
}
function Loop-Process { Own-Process (Read-Pid $loopPidFile) 'powershell*' '*start-game-tunnel.ps1*' }
function Ssh-Process { Own-Process (Read-Pid $sshPidFile) 'ssh.exe' '*aw-game-tunnel@*' }

try {
    if ($Action -eq 'start') {
        if (Loop-Process) {
            Say 'Туннель уже запущен.'
            return
        }
        $script = Join-Path $folder 'start-game-tunnel.ps1'
        if (-not (Test-Path -LiteralPath $script)) { throw 'Нет скрипта туннеля рядом с этим файлом.' }
        $arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Config "{1}"' -f $script, $Config
        Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe') `
            -ArgumentList $arguments -WindowStyle Hidden
        # Up to 30 s for the connection to the server to be established.
        for ($i = 0; $i -lt 30; $i++) {
            Start-Sleep -Seconds 1
            $ssh = Ssh-Process
            if ($ssh) {
                $state = Get-NetTCPConnection -OwningProcess $ssh.ProcessId -RemotePort 22 -ErrorAction SilentlyContinue |
                    Select-Object -First 1 -ExpandProperty State
                if ($state -eq 'Established') {
                    Say 'Туннель включён. Можно запускать игру и Опечатку.'
                    return
                }
            }
        }
        Say 'Туннель запущен, но связь с сервером пока не установилась. Подождите минуту и нажмите «включить» ещё раз; подробности в tunnel.log рядом со скриптом.'
    } else {
        # If the scheduled task owns the loop, let the scheduler end it (it would otherwise restart it).
        & schtasks.exe /end /tn $taskName 2>$null | Out-Null
        $ssh = Ssh-Process
        if ($ssh) { Stop-Process -Id $ssh.ProcessId -Force -ErrorAction SilentlyContinue }
        $loop = Loop-Process
        if ($loop) { Stop-Process -Id $loop.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
        Remove-Item -LiteralPath $loopPidFile, $sshPidFile -Force -ErrorAction SilentlyContinue
        Say 'Туннель выключен.'
    }
} catch {
    Say ('Не получилось: ' + $_.Exception.Message)
}

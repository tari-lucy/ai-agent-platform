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
$audioPidFile = Join-Path $folder 'audio.pid'
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
function Audio-Process { Own-Process (Read-Pid $audioPidFile) 'python*' '*pc_audio_bridge.py*' }

# The audio bridge (the persona's voice into the virtual cable) starts and stops with the tunnel.
# Its settings are the "audio" part of the same config file:
#   "audio": {"script": "C:/ai-pers2/runtime/pc_audio_bridge.py", "port": 8003, "device": "6"}
# and, for listening, "capture_port", "capture_device", "capture_rate". Without "audio": nothing.
function Start-Audio($settings) {
    $audio = $settings.audio
    if (-not $audio) { return '' }
    if (Audio-Process) { return ' Мост звука уже запущен.' }
    $script = [string]$audio.script
    if (-not (Test-Path -LiteralPath $script -PathType Leaf)) { return ' Мост звука: не найден файл ' + $script + '.' }
    $python = (Get-Command python.exe -ErrorAction SilentlyContinue).Source
    if (-not $python) { return ' Мост звука: не найден python.exe.' }
    $port = [int]$audio.port
    if ($port -lt 1024 -or $port -gt 65535) { return ' Мост звука: неверный порт.' }
    $arguments = @('-u', ('"{0}"' -f $script), '--port', "$port", '--device', ('"{0}"' -f $audio.device))
    if ($audio.latency) { $arguments += @('--latency', ([string]$audio.latency)) }
    if ($audio.capture_port) {
        $arguments += @('--capture-port', ([string][int]$audio.capture_port))
        if ($audio.capture_loopback) {
            # Listen to what a playback device (the speakers) plays: no second cable needed.
            $arguments += @('--capture-loopback', ('"{0}"' -f $audio.capture_loopback))
        } else {
            $arguments += @('--capture-device', ('"{0}"' -f $audio.capture_device))
        }
        $arguments += @('--capture-rate', ([string][int]$audio.capture_rate))
    }
    $child = Start-Process -FilePath $python -ArgumentList $arguments -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $folder 'audio.log') -RedirectStandardError (Join-Path $folder 'audio.err')
    Set-Content -LiteralPath $audioPidFile -Value $child.Id
    Start-Sleep -Seconds 3
    if ($child.HasExited) { return ' Мост звука не запустился: причина в audio.err рядом со скриптом.' }
    return ' Мост звука запущен.'
}

try {
    if ($Action -eq 'start') {
        $settings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
        $audioNote = Start-Audio $settings
        if (Loop-Process) {
            Say ('Туннель уже запущен.' + $audioNote)
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
                    Say ('Туннель включён. Можно запускать игру и Опечатку.' + $audioNote)
                    return
                }
            }
        }
        Say ('Туннель запущен, но связь с сервером пока не установилась. Подождите минуту и нажмите «включить» ещё раз; подробности в tunnel.log и ssh.err рядом со скриптом.' + $audioNote)
    } else {
        # If the scheduled task owns the loop, let the scheduler end it (it would otherwise restart it).
        & schtasks.exe /end /tn $taskName 2>$null | Out-Null
        $ssh = Ssh-Process
        if ($ssh) { Stop-Process -Id $ssh.ProcessId -Force -ErrorAction SilentlyContinue }
        $loop = Loop-Process
        if ($loop) { Stop-Process -Id $loop.ProcessId -Force -ErrorAction SilentlyContinue }
        $audio = Audio-Process
        if ($audio) { Stop-Process -Id $audio.ProcessId -Force -ErrorAction SilentlyContinue }
        Start-Sleep -Seconds 1
        Remove-Item -LiteralPath $loopPidFile, $sshPidFile, $audioPidFile -Force -ErrorAction SilentlyContinue
        Say 'Туннель и мост звука выключены.'
    }
} catch {
    Say ('Не получилось: ' + $_.Exception.Message)
}

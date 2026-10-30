param(
    [Parameter(Mandatory = $true)][string]$Config,
    [Parameter(Mandatory = $true)][string]$RuntimeDirectory
)
$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathRooted($Config) -or -not [IO.Path]::IsPathRooted($RuntimeDirectory)) {
    throw 'Absolute paths required.'
}
if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw 'Proxy config missing.' }
$name = 'AI Agent Platform - Telegram Proxy'
$installed = Join-Path $RuntimeDirectory 'start-proxy.ps1'
$executable = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Config "{1}"' -f $installed, $Config
$existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
if ($existing -and ($existing.Actions.Execute -ne $executable -or $existing.Actions.Arguments -ne $arguments)) {
    throw 'Existing task differs; not overwritten.'
}
New-Item -ItemType Directory -Path $RuntimeDirectory -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'start_telegram_proxy.ps1') -Destination $installed -Force
$action = New-ScheduledTaskAction -Execute $executable -Argument $arguments
$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -Hidden -StartWhenAvailable -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $name -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description 'Local-only Telegram SOCKS tunnel; dedicated restricted SSH key.' -Force | Out-Null
Start-ScheduledTask -TaskName $name
Write-Output 'TELEGRAM_PROXY_TASK_READY'

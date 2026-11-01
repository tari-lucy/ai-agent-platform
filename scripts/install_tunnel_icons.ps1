# Put the two tunnel buttons on the desktop: «Туннель - включить» and «Туннель - выключить».
# Run once:  powershell -NoProfile -ExecutionPolicy Bypass -File install_tunnel_icons.ps1
param([string]$RuntimeDirectory = 'C:\ai-pers2\runtime\game-tunnel')
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $RuntimeDirectory -Force | Out-Null
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'start_game_tunnel.ps1') -Destination (Join-Path $RuntimeDirectory 'start-game-tunnel.ps1') -Force
$control = Join-Path $RuntimeDirectory 'game-tunnel-control.ps1'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'game_tunnel_control.ps1') -Destination $control -Force
$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
function New-Button([string]$title, [string]$action, [string]$icon) {
    $link = $shell.CreateShortcut((Join-Path $desktop ($title + '.lnk')))
    $link.TargetPath = $powershell
    $link.Arguments = '-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{0}" -Action {1}' -f $control, $action
    $link.WorkingDirectory = $RuntimeDirectory
    $link.WindowStyle = 7
    $link.IconLocation = $icon
    $link.Description = 'Туннель между компьютером и сервером для игры'
    $link.Save()
}
New-Button 'Туннель - включить' 'start' ((Join-Path $env:SystemRoot 'System32\netshell.dll') + ',0')
New-Button 'Туннель - выключить' 'stop' ((Join-Path $env:SystemRoot 'System32\shell32.dll') + ',27')
Write-Output 'TUNNEL_ICONS_READY'

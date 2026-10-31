param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Files)
# Drop files on the "Отдать Опечатке" icon: they land in the persona's workspace inbox/ on the
# server through the panel (same session, CSRF and limits as the button). The panel password is
# asked once and kept encrypted for this Windows user only (DPAPI).
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$base = 'http://127.0.0.1:8765'
$store = Join-Path $env:APPDATA 'ai-agent-platform\panel-password.txt'
function Say([string]$text, [string]$icon = 'Information') {
    [void][System.Windows.Forms.MessageBox]::Show($text, 'Отдать Опечатке', 'OK', $icon)
}
try {
    if (-not $Files -or $Files.Count -eq 0) { Say 'Перетащите файлы на этот значок.'; exit 0 }
    if (-not (Test-Path -LiteralPath $store)) {
        # A dialog, not the console: the icon runs hidden.
        $typed = (Get-Credential -UserName 'панель' `
            -Message 'Пароль панели Опечатки. Запоминается только для вашего пользователя Windows.').Password
        New-Item -ItemType Directory -Force -Path (Split-Path $store) | Out-Null
        $typed | ConvertFrom-SecureString | Set-Content -LiteralPath $store -Encoding ASCII
    }
    $secure = (Get-Content -LiteralPath $store -Raw).Trim() | ConvertTo-SecureString
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    $body = @{ password = $plain } | ConvertTo-Json
    try {
        $login = Invoke-WebRequest -Uri "$base/api/v1/auth/login" -Method Post -UseBasicParsing `
            -ContentType 'application/json' -Headers @{ Origin = $base } -Body $body -SessionVariable session
    } catch {
        $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        if ($code -in 401, 403) {
            Remove-Item -LiteralPath $store -Force
            Say 'Пароль не подошёл. Перетащите файл ещё раз и введите пароль заново.' 'Warning'
        } else {
            Say 'Панель недоступна. Проверьте, что мост до сервера поднят и Опечатка запущена (START).' 'Warning'
        }
        exit 1
    }
    $info = $login.Content | ConvertFrom-Json
    $scope = $info.scopes | Where-Object { $_.memory_mode -eq 'work' } | Select-Object -First 1
    if (-not $scope) { Say 'Рабочая область Опечатки не найдена.' 'Warning'; exit 1 }
    $done = @()
    foreach ($file in $Files) {
        $item = Get-Item -LiteralPath $file
        if ($item.PSIsContainer) { Say "«$($item.Name)» — папка. Отдавать можно файлы (папку заархивируйте)." 'Warning'; continue }
        $query = 'workspace_id={0}&persona_id={1}&memory_space_id={2}&memory_mode=work&name={3}' -f `
            [uri]::EscapeDataString($scope.workspace_id), [uri]::EscapeDataString($scope.persona_id), `
            [uri]::EscapeDataString($scope.memory_space_id), [uri]::EscapeDataString($item.Name)
        $headers = @{ Origin = $base; 'X-CSRF-Token' = $info.csrf_token }
        $reply = Invoke-WebRequest -Uri "$base/api/v1/agent-inbox?$query" -Method Post -UseBasicParsing `
            -WebSession $session -ContentType 'application/octet-stream' -Headers $headers -InFile $item.FullName
        $done += ($reply.Content | ConvertFrom-Json).path
    }
    if ($done.Count -gt 0) {
        Say ("Отдано Опечатке:`n" + ($done -join "`n") + "`n`nНапишите ей, что файл лежит в inbox (в консоли можно приложить его через @inbox/…).")
    }
} catch {
    $code = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
    $why = switch ($code) { 413 { 'файл слишком большой (до 100 МБ)' } 429 { 'другая загрузка ещё идёт, подождите' } 503 { 'у Опечатки нет рабочей папки на сервере' } default { $_.Exception.Message } }
    Say "Не получилось: $why" 'Error'
    exit 1
}

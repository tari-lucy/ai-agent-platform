param([string]$Config = (Join-Path (Split-Path $PSScriptRoot -Parent) 'config\local.launcher.json'))
$ErrorActionPreference = 'Stop'
try {
    if (-not (Test-Path -LiteralPath $Config -PathType Leaf)) { throw 'LAUNCHER_CONFIG_MISSING: copy config/launcher.example.json to config/local.launcher.json' }
    if ((Get-Item -LiteralPath $Config).Length -gt 65536) { throw 'LAUNCHER_CONFIG_INVALID' }
    $settings = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
    $keys = @('python', 'host_root', 'provider', 'env_file', 'panel_dir')
    $actual = @($settings.PSObject.Properties.Name)
    if ($actual.Count -ne $keys.Count) { throw 'LAUNCHER_CONFIG_INVALID' }
    foreach ($name in $actual) {
        if ($name -notin $keys -or $settings.$name -isnot [string]) { throw 'LAUNCHER_CONFIG_INVALID' }
    }
    if ($settings.provider -notin @('mock', 'live')) { throw 'LAUNCHER_CONFIG_INVALID' }
    foreach ($name in @('python', 'host_root', 'panel_dir')) {
        if (-not [System.IO.Path]::IsPathRooted($settings.$name) -or -not (Test-Path -LiteralPath $settings.$name)) { throw 'LAUNCHER_PATH_INVALID' }
    }
    $arguments = @('-I', '-m', 'ai_agent_core.local_host', 'serve', '--root', $settings.host_root, '--provider', $settings.provider, '--panel-dir', $settings.panel_dir, '--open-browser')
    if ($settings.provider -eq 'live') {
        if (-not [System.IO.Path]::IsPathRooted($settings.env_file) -or -not (Test-Path -LiteralPath $settings.env_file -PathType Leaf)) { throw 'LAUNCHER_ENV_FILE_MISSING' }
        $arguments += @('--env-file', $settings.env_file)
    } elseif ($settings.env_file -ne '') { throw 'LAUNCHER_ENV_FILE_NOT_ALLOWED' }
    Write-Host 'Opening the local panel. Leave this window open. Ctrl+C closes the server.'
    & $settings.python @arguments
    exit $LASTEXITCODE
} catch {
    # Never print arbitrary paths, JSON, environment values or interpreter errors.
    $message = $_.Exception.Message
    if ($message -match '^LAUNCHER_[A-Z_]+(: copy config/launcher.example.json to config/local.launcher.json)?$') { Write-Host $message }
    else { Write-Host 'LAUNCHER_CONFIG_INVALID' }
    exit 78
}

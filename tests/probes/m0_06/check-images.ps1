param(
    [string]$CoreImage = 'ai-agent-platform/core:m0-06',
    [string]$WorkerImage = 'ai-agent-platform/worker:m0-06'
)
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../../..')).Path
$taskRun = 'ai-platform-m0-06-images-' + [guid]::NewGuid().ToString('N').Substring(0,12)
$taskReceipt = @{}
$taskStarted = @()
foreach ($taskRole in @('core','worker')) {
    $taskImage = docker image inspect $(if($taskRole -eq 'core'){$CoreImage}else{$WorkerImage}) --format '{{.Id}}'
    if ($LASTEXITCODE -ne 0) { throw 'Image missing; no implicit pull' }
    $taskReceipt[$taskRole] = $taskImage
    docker run --name "$taskRun-$taskRole-check" --network none --read-only `
        --memory 1536m --memory-swap 1536m --cpus 2 --pids-limit 256 `
        --tmpfs /tmp:rw,size=64m --mount "type=bind,source=$PSScriptRoot,target=/probe,readonly" `
        $taskImage python /probe/image_check.py --role $taskRole
    if ($LASTEXITCODE -ne 0) { throw 'Installed wheel source/state mismatch' }
    $taskErrorOutput = docker run --name "$taskRun-$taskRole-invalid" --network none `
        --read-only --memory 1536m --cpus 2 --pids-limit 256 --tmpfs /tmp:rw,size=64m `
        -e AI_PLATFORM_AUTOSTART=true $taskImage 2>&1
    $taskExit = $LASTEXITCODE
    if ($taskExit -ne 78 -or ($taskErrorOutput -join '') -notmatch 'autostart: must_be_disabled') {
        throw 'Real image entrypoint did not reject unsafe config before start'
    }
}
try {
    foreach ($taskRole in @('core','worker')) {
        $taskImage = $taskReceipt[$taskRole]
        $taskName = "$taskRun-$taskRole-idle"
        docker run -d --name $taskName --network none --read-only --init `
            --cap-drop ALL --security-opt no-new-privileges:true `
            --memory 1536m --memory-swap 1536m --cpus 2 --pids-limit 256 `
            --tmpfs /tmp:rw,size=64m $taskImage | Out-Null
        if ($LASTEXITCODE -ne 0) { throw 'Idle image startup failed' }
        $taskStarted += $taskName
    }
    $taskCoreOK=$false
    for($taskAttempt=0; $taskAttempt -lt 10; $taskAttempt++) {
        docker exec "$taskRun-core-idle" python -c 'import http.client; c=http.client.HTTPConnection("127.0.0.1",8080,timeout=2); c.request("GET","/"); r=c.getresponse(); assert r.status==200' 2>$null
        if($LASTEXITCODE -eq 0) {$taskCoreOK=$true; break}
        Start-Sleep -Milliseconds 500
    }
    if(-not $taskCoreOK) {throw 'Core static panel failed after validated STOPPED bootstrap'}
    docker exec "$taskRun-worker-idle" python /opt/platform/healthcheck.py worker
    if($LASTEXITCODE -ne 0) {throw 'Worker stopped idle heartbeat failed'}
    $taskReceipt['real_entrypoints'] = 'invalid exit 78; valid core HTTP200/worker heartbeat'
    $taskReceipt['published_ports'] = 'none'
} finally {
    foreach($taskName in $taskStarted) {
        docker stop --time 5 $taskName | Out-Null
        if($LASTEXITCODE -ne 0) {throw 'Own probe container stop failed'}
    }
}
[IO.File]::WriteAllText((Join-Path $PSScriptRoot 'image-receipt.json'), ($taskReceipt | ConvertTo-Json)+[Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output 'M0-06 actual installed images/entrypoints passed; own containers stopped.'

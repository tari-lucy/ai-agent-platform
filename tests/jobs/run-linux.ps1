#requires -Version 7.3
param(
    [string]$BaseImage = 'sha256:a35928bd28aab333eed5a98e165ff8402f3b6c7f7fcba776c0f1e459f8cd0d97',
    [string]$RedisImage = 'sha256:2c2dff791878316e3b083188be0d578423b24813cdda5de0e80ea4f7b3e6bfd6'
)
$ErrorActionPreference='Stop'
$PSNativeCommandArgumentPassing='Standard'
$taskRoot=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$taskRun='ai-platform-m1-04-'+[guid]::NewGuid().ToString('N').Substring(0,12)
$taskResults=Join-Path $taskRoot ('runtime/m1-04/'+$taskRun)
New-Item -ItemType Directory -Path $taskResults -Force | Out-Null
$taskImage=docker image inspect $BaseImage --format '{{.Id}}'
if($LASTEXITCODE -ne 0){throw 'Pinned check image missing; no implicit pull'}
$taskRedisImage=docker image inspect $RedisImage --format '{{.Id}}'
if($LASTEXITCODE -ne 0){throw 'Pinned Redis image missing; no implicit pull'}
$taskNetwork=$taskRun+'-network'
$taskVolume=$taskRun+'-sqlite'
$taskRedisVolume=$taskRun+'-redis'
$taskRedis=$taskRun+'-broker'
docker network create --internal --label ai-platform.task=M1-04 $taskNetwork | Out-Null
if($LASTEXITCODE -ne 0){throw 'Isolated internal network failed'}
foreach($taskNamedVolume in @($taskVolume,$taskRedisVolume)){
    docker volume create --label ai-platform.task=M1-04 $taskNamedVolume | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Isolated volume failed'}
}
docker run --pull never --name ($taskRun+'-volume-init') --network none --cpus 0.5 --memory 128m --memory-swap 128m --read-only --pids-limit 64 --mount "type=volume,source=$taskVolume,target=/data" --mount "type=volume,source=$taskRedisVolume,target=/redis-data" $taskImage /check/.venv/bin/python -c "import os; os.chown('/data',10001,10001); os.chown('/redis-data',999,999)"
if($LASTEXITCODE -ne 0){throw 'Volume ownership initialization failed'}
try {
    # Public synthetic password; never a live .env binding.
    docker run -d --pull never --name $taskRedis --network $taskNetwork --network-alias synthetic-broker --user 999:999 --cpus 0.5 --memory 256m --memory-swap 256m --read-only --pids-limit 64 --tmpfs /tmp:rw,size=32m --mount "type=volume,source=$taskRedisVolume,target=/data" $taskRedisImage redis-server --dir /data --appendonly yes --appendfsync everysec --maxmemory 128mb --maxmemory-policy noeviction --requirepass synthetic-test-only | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Synthetic Redis start failed'}
    $taskArgs=@(
        '--pull','never','--name',$taskRun,'--user','10001:10001','--network',$taskNetwork,
        '--cpus','2','--memory','1536m','--memory-swap','1536m','--read-only','--pids-limit','256',
        '--tmpfs','/tmp:rw,size=128m','--workdir','/check',
        '--mount',"type=volume,source=$taskVolume,target=/data",
        '--mount',"type=bind,source=$taskRoot/migrations,target=/check/migrations,readonly",
        '--mount',"type=bind,source=$taskRoot/deploy/compose.yaml,target=/check/deploy/compose.yaml,readonly",
        '-e','M1_02_DB_DIR=/data','-e','M1_03_DB_DIR=/data','-e','M1_04_DB_DIR=/data',
        '-e','M1_04_REDIS_HOST=synthetic-broker',
        '--mount',"type=bind,source=$taskRoot/pyproject.toml,target=/check/pyproject.toml,readonly",
        '--mount',"type=bind,source=$taskRoot/scripts,target=/check/scripts,readonly",
        '--mount',"type=bind,source=$taskRoot/tests,target=/check/tests,readonly",
        '--mount',"type=bind,source=$taskRoot/config/defaults,target=/check/config/defaults,readonly",
        '--mount',"type=bind,source=$taskRoot/deploy/core.py,target=/check/deploy/core.py,readonly",
        '--mount',"type=bind,source=$taskRoot/deploy/worker.py,target=/check/deploy/worker.py,readonly",
        '-e','PYTHONDONTWRITEBYTECODE=1','-e','PYTHONPYCACHEPREFIX=/tmp/pycache',
        '-e','MYPY_CACHE_DIR=/tmp/mypy','-e','RUFF_CACHE_DIR=/tmp/ruff'
    )
    $taskSources=@('apps/core/src','apps/worker/src','packages/contracts/src',
        'packages/domain/src','packages/application/src','packages/adapters/src')
    foreach($taskSource in $taskSources){
        $taskArgs+=@('--mount',"type=bind,source=$taskRoot/$taskSource,target=/check/$taskSource,readonly")
    }
    $taskArgs+=@('--mount',"type=bind,source=$taskRoot/packages/contracts/generated,target=/check/packages/contracts/generated,readonly",
        '-e',('PYTHONPATH='+(($taskSources|ForEach-Object{'/check/'+$_})-join ':')))
    $taskCommand=@'
set -e
/check/.venv/bin/python -c 'import sqlite3,sys; print("Python",sys.version.split()[0],"SQLite",sqlite3.sqlite_version)'
/check/.venv/bin/python -m ai_agent_contracts.export --output packages/contracts/generated --check
/check/.venv/bin/python scripts/check_imports.py
/check/.venv/bin/python -m ruff check packages tests/contracts tests/storage tests/control tests/jobs migrations apps/core/src
/check/.venv/bin/python -m mypy apps/core/src apps/worker/src packages scripts
/check/.venv/bin/python -m pytest tests/jobs tests/control tests/storage tests/tooling tests/contracts tests/probes/m0_03 tests/probes/m0_05 tests/probes/m0_06/test_configuration.py -q -p no:cacheprovider --tb=short
'@
    docker run @taskArgs $taskImage /bin/sh -c $taskCommand.Replace("`r`n","`n")
    if($LASTEXITCODE -ne 0){throw 'M1-04 Linux/Redis combined checks failed'}
    docker restart $taskRedis | Out-Null
    if($LASTEXITCODE -ne 0){throw 'Synthetic Redis restart failed'}
    $taskReconnectArgs=@('--pull','never','--name',($taskRun+'-restart'),'--network',$taskNetwork,
        '--user','10001:10001','--cpus','0.5','--memory','256m','--memory-swap','256m',
        '--read-only','--pids-limit','64','-e','M1_04_REDIS_HOST=synthetic-broker')
    $taskRestartCode=@'
import os
from redis import Redis
r=Redis(host=os.environ["M1_04_REDIS_HOST"],password="synthetic-test-only",socket_timeout=2,socket_connect_timeout=2,decode_responses=True)
assert r.ping()
streams=list(r.scan_iter(match="synthetic-*",count=50))
assert len(streams)>=3
assert any(r.xlen(s)>0 for s in streams)
assert all(len(r.xinfo_groups(s))==1 for s in streams)
print("Actual Redis restart/AOF reconnect passed")
'@
    docker run @taskReconnectArgs $taskImage /check/.venv/bin/python -c $taskRestartCode.Replace("`r`n","`n")
    if($LASTEXITCODE -ne 0){throw 'Synthetic Redis AOF restart receipt failed'}
    $taskReceipt=@{
        image=$taskImage; redis_image=$taskRedisImage
        container=(docker inspect $taskRun --format '{"exit":{{.State.ExitCode}},"oom":{{.State.OOMKilled}},"ports":{{json .HostConfig.PortBindings}}}' | ConvertFrom-Json)
        redis=(docker inspect $taskRedis --format '{"oom":{{.State.OOMKilled}},"ports":{{json .HostConfig.PortBindings}}}' | ConvertFrom-Json)
        internal_network=(docker network inspect $taskNetwork --format '{{.Internal}}')
        uv_lock_sha256=(Get-FileHash (Join-Path $taskRoot 'uv.lock') -Algorithm SHA256).Hash.ToLowerInvariant()
        source_sha256=@{}
    }
    foreach($taskTextPath in @('packages','apps/core/src','apps/worker/src','scripts','migrations')){
        foreach($taskFile in (Get-ChildItem (Join-Path $taskRoot $taskTextPath) -Recurse -File -Filter '*.py')){
            if($taskFile.FullName -match '__pycache__'){continue}
            $taskRelative=[IO.Path]::GetRelativePath($taskRoot,$taskFile.FullName).Replace('\','/')
            $taskText=[IO.File]::ReadAllText($taskFile.FullName).Replace("`r`n","`n")
            $taskHash=[Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($taskText))
            $taskReceipt.source_sha256[$taskRelative]=[Convert]::ToHexString($taskHash).ToLowerInvariant()
        }
    }
    [IO.File]::WriteAllText((Join-Path $taskResults 'run-receipt.json'),
        ($taskReceipt|ConvertTo-Json -Depth 8),[Text.UTF8Encoding]::new($false))
    Write-Output "M1-04 passed. Results: runtime/m1-04/$taskRun"
} finally {
    docker stop $taskRedis | Out-Null
}

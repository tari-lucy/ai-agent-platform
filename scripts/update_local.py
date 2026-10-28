"""Atomic Windows deployment update. No env secrets, migrations, force/reset/clean or START.

Build a separate candidate, then STOP/verify/backup and switch only the launcher.
The old checkout and environment stay usable. GitHub main is never pushed here.
"""

import argparse
import contextlib
import ctypes
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
import webbrowser
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

MESSAGES = {
    "CONFIG": "Не удалось прочитать настройки запуска.",
    "DIRTY": "В checkout есть незакоммиченные изменения. Они сохранены; обновление отменено.",
    "GIT_BUSY": "В checkout не закончено слияние или rebase. Сначала разберите его.",
    "GIT": "Git не выполнил команду. Проверьте доступ к GitHub и состояние репозитория.",
    "CONFLICT": "Изменения main конфликтуют с локальными. Старый запуск не менялся.",
    "MANUAL_RELEASE": "Изменились миграции, Docker или native UI. Нужна отдельная поставка.",
    "TOOLS": "Не найдены нужные uv, Node или npm. Ничего системно не устанавливалось.",
    "VERSIONS": "Версии uv/Node/npm не соответствуют новой версии проекта.",
    "DEPENDENCIES": "Не удалось собрать окружение из локального кэша. Старая версия сохранена.",
    "BUILD": "Сборка панели не прошла. Старый запуск сохранён.",
    "PREFLIGHT": "Проверка нового запуска не прошла. Проверьте Docker и готовность поставки.",
    "PORT": "На порту другой процесс; он не остановлен.",
    "AUTH": "Не удалось выполнить служебный вход. Пароль не выводился.",
    "STOP": "Ядро не подтвердило STOP и очистку ресурсов. Переключение отменено.",
    "JOBS": "Остались незавершённые задания. Переключение отменено; нужна диагностика.",
    "LOCK": "Сервер или другое обновление ещё удерживает каталог. Повторный запуск запрещён.",
    "BACKUP": "Резервная копия не создана или не прошла проверку. Launcher не переключён.",
    "STARTUP": "Новый сервер не запустился. Прежний launcher возвращён; данные не заменялись.",
    "CLEANUP": "Не подтверждено закрытие нового сервера. Не запускайте второй; нужна диагностика.",
    "INTERNAL": "Обновление остановлено из-за технической ошибки. Старая версия сохранена.",
}


class UpdateError(Exception):
    pass


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    code: str = "INTERNAL",
    timeout: int = 120,
    env: dict[str, str] | None = None,
) -> str:
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError, subprocess.TimeoutExpired:
        raise UpdateError(code) from None
    if result.returncode:
        raise UpdateError(code)
    return result.stdout.decode("utf-8-sig", "replace").strip()


def git(root: Path, *args: str) -> str:
    return run(
        ["git", "-c", "safe.directory=" + root.as_posix(), "-C", str(root), *args], code="GIT"
    )


def load_launcher(path: Path) -> dict[str, str]:
    try:
        if path.stat().st_size > 65536:
            raise ValueError()
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        required = {"python", "host_root", "provider", "env_file", "panel_dir"}
        if not isinstance(value, dict) or not required <= value.keys() <= required | {
            "opencode_ui_dir"
        }:
            raise ValueError()
        if not all(isinstance(v, str) for v in value.values()) or value["provider"] not in {
            "live",
            "mock",
        }:
            raise ValueError()
        for key in ("python", "host_root", "panel_dir"):
            if not Path(value[key]).is_absolute() or not Path(value[key]).exists():
                raise ValueError()
        if sys.platform == "win32" and any(v.startswith("\\\\") for v in value.values()):
            raise ValueError()
        return value
    except OSError, ValueError, TypeError:
        raise UpdateError("CONFIG") from None


def require_clean(root: Path) -> None:
    for name in ("MERGE_HEAD", "CHERRY_PICK_HEAD", "rebase-merge", "rebase-apply"):
        path = Path(git(root, "rev-parse", "--git-path", name))
        if (path if path.is_absolute() else root / path).exists():
            raise UpdateError("GIT_BUSY")
    changes = git(root, "status", "--porcelain", "--untracked-files=normal")
    # This exact build artifact was accidentally committed as a Linux symlink.
    # Its Windows removal is preserved; only our fresh candidate drops it from Git.
    if dependency_link(root):
        changes = "\n".join(
            line
            for line in changes.splitlines()
            if line not in {" D apps/panel/node_modules", "D apps/panel/node_modules"}
        )
    if changes:
        raise UpdateError("DIRTY")


def dependency_link(root: Path) -> bool:
    return git(root, "ls-tree", "HEAD", "--", "apps/panel/node_modules").startswith("120000 ")


def release_needed(paths: list[str]) -> bool:
    prefixes = (
        "migrations/",
        "deploy/opencode/",
        "deploy/opencode-ui/patches/",
        "packages/adapters/src/ai_agent_adapters/agent_harness/",
    )
    return any(
        p.startswith(prefixes)
        or p in {"scripts/build_hands_image.py", "scripts/refresh_opencode_harness.py"}
        for p in paths
    )


def candidate(root: Path, target: Path, stamp: str) -> Path:
    require_clean(root)
    head = git(root, "rev-parse", "HEAD")
    git(root, "fetch", "origin")
    try:
        tracked = git(root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    except UpdateError:
        tracked = "origin/main"
    if not re.fullmatch(r"origin/[A-Za-z0-9._/-]+", tracked):
        raise UpdateError("GIT")
    refs = list(dict.fromkeys([tracked, "origin/main"]))
    incoming = [ref for ref in refs if git(root, "rev-list", f"HEAD..{ref}", "--count") != "0"]
    if not incoming and not dependency_link(root):
        return root
    git(root, "worktree", "add", "-b", "codex/local-update-" + stamp, str(target), head)
    try:
        for ref in incoming:
            git(target, "merge", "--no-edit", ref)
    except UpdateError:
        merge = Path(git(target, "rev-parse", "--git-path", "MERGE_HEAD"))
        if (merge if merge.is_absolute() else target / merge).exists():
            git(target, "merge", "--abort")  # only our new, previously clean candidate
        raise UpdateError("CONFLICT") from None
    if release_needed(git(target, "diff", "--name-only", head, "HEAD").splitlines()):
        raise UpdateError("MANUAL_RELEASE")
    if dependency_link(target):
        # target was just created, before any dependency install. Never touch the old tree.
        git(target, "rm", "-f", "--", "apps/panel/node_modules")
        git(target, "commit", "-m", "Remove non-portable dependency link from deployment")
    git(target, "branch", "--set-upstream-to=" + tracked)
    return target


def tools(root: Path) -> tuple[str, str, dict[str, str]]:
    env = os.environ.copy()
    if sys.platform == "win32":
        extra = [
            str(Path(env.get("ProgramFiles", "C:/Program Files")) / "nodejs"),
            str(Path(env.get("LOCALAPPDATA", "")) / "Microsoft/WinGet/Links"),
        ]
        env["PATH"] = os.pathsep.join(extra + [env.get("PATH", "")])
    uv, node, npm = (
        shutil.which(name, path=env.get("PATH"))
        for name in ("uv", "node", "npm.cmd" if sys.platform == "win32" else "npm")
    )
    if not uv or not node or not npm:
        raise UpdateError("TOOLS")
    required = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf8"))["tool"]["uv"][
        "required-version"
    ]
    if not required.startswith("==") or not run([uv, "--version"]).startswith(
        "uv " + required[2:] + " "
    ):
        raise UpdateError("VERSIONS")
    engines = json.loads((root / "apps/panel/package.json").read_text())["engines"]
    for binary, name in ((node, "node"), (npm, "npm")):
        match = re.fullmatch(r">=(\d+)\.(\d+)\.(\d+) <(\d+)", engines[name])
        version = run([binary, "--version"], env=env).lstrip("v").split(".")
        if match is None or len(version) != 3 or not all(v.isdigit() for v in version):
            raise UpdateError("VERSIONS")
        numbers = tuple(map(int, version))
        if numbers < tuple(map(int, match.groups()[:3])) or numbers[0] >= int(match[4]):
            raise UpdateError("VERSIONS")
    return uv, npm, env


def prepare(root: Path, old_python: str) -> Path:
    uv, npm, env = tools(root)
    env["UV_PROJECT_ENVIRONMENT"] = str(root / ".venv")
    base_python = run(
        [old_python, "-I", "-X", "utf8", "-c", "import sys; print(sys._base_executable)"]
    )
    run(
        [uv, "sync", "--locked", "--offline", "--python", base_python, "--no-python-downloads"],
        cwd=root,
        code="DEPENDENCIES",
        timeout=300,
        env=env,
    )
    python = root / ".venv/Scripts/python.exe"
    run(
        [npm, "ci", "--offline", "--no-audit", "--no-fund", "--engine-strict"],
        cwd=root / "apps/panel",
        code="DEPENDENCIES",
        timeout=300,
        env=env,
    )
    run([npm, "run", "build"], cwd=root / "apps/panel", code="BUILD", timeout=300, env=env)
    return python


def process_at(port: int) -> tuple[int, str] | None:
    if sys.platform != "win32":
        raise UpdateError("PORT")
    text = run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
            f"$c=@(Get-NetTCPConnection -LocalPort {port} -State Listen "
            "-ErrorAction SilentlyContinue); "
            "if($c.Count){if($c.Count -ne 1 -or $c[0].LocalAddress -ne '127.0.0.1'){exit 78}; "
            "Get-CimInstance Win32_Process -Filter ('ProcessId='+$c[0].OwningProcess) | "
            "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress}",
        ],
        code="PORT",
        timeout=15,
    )
    if not text:
        return None
    value = json.loads(text)
    return int(value["ProcessId"]), value["CommandLine"]


def own_command(command: str, host: Path) -> bool:
    count = ctypes.c_int()
    shell = ctypes.windll.shell32
    shell.CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    shell.CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    ctypes.windll.kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    ctypes.windll.kernel32.LocalFree.restype = ctypes.c_void_p
    args = shell.CommandLineToArgvW(command, ctypes.byref(count))
    if not args:
        return False
    try:
        values = [args[i] for i in range(count.value)]
        pos = values.index("-m")
        root = values.index("--root")
        return (
            values[pos + 1 : pos + 3] == ["ai_agent_core.local_host", "serve"]
            and Path(values[root + 1]).resolve() == host.resolve()
        )
    except ValueError, IndexError:
        return False
    finally:
        ctypes.windll.kernel32.LocalFree(args)


def launched_by(pid: int, child: subprocess.Popen[bytes]) -> bool:
    if pid == child.pid:
        return True
    if sys.platform != "win32" or child.poll() is not None:
        return False
    # Windows venv python.exe is a redirector: the real interpreter owns the socket.
    parent = run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').ParentProcessId",
        ],
        code="PORT",
        timeout=10,
    )
    return parent == str(child.pid) and child.poll() is None


def settled_jobs(host: Path) -> bool:
    with sqlite3.connect((host / "data/platform.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        count = db.execute(
            "SELECT count(*) FROM records r JOIN record_revisions v "
            "USING(scope_id,kind,id,version) WHERE r.kind='Job' AND r.deleted=0 "
            "AND COALESCE(json_extract(v.payload,'$.status'),'') "
            "NOT IN ('SUCCEEDED','FAILED','CANCELLED','REJECTED','EXPIRED')"
        ).fetchone()[0]
    return int(count) == 0


def stop_server(host: Path, port: int, password_file: Path) -> None:
    found = process_at(port)
    if found is None:
        return
    pid, command = found
    if not own_command(command, host):
        raise UpdateError("PORT")
    kernel = ctypes.windll.kernel32
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    kernel.WaitForSingleObject.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(
        0x100001, False, pid
    )  # terminate + synchronize, exact process object
    if not handle:
        raise UpdateError("PORT")
    try:
        # Recheck after acquiring the process handle: the first PID may have exited.
        if process_at(port) != found:
            raise UpdateError("PORT")
        origin = f"http://127.0.0.1:{port}"
        with httpx.Client(
            base_url=origin, timeout=5, follow_redirects=False, trust_env=False
        ) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={"password": password_file.read_text().strip()},
                headers={"Origin": origin},
            )
            if login.status_code != 200:
                raise UpdateError("AUTH")
            auth = client.get("/api/v1/auth/session").json()
            query = None
            for scope in auth["scopes"]:
                q = {
                    k: scope[k]
                    for k in ("workspace_id", "persona_id", "memory_space_id", "memory_mode")
                }
                page = client.get("/api/v1/sessions", params=q)
                if page.status_code != 200:
                    raise UpdateError("STOP")
                if page.json()["items"]:
                    current, query = page.json()["items"][0], q
                    break
            if query is None:
                raise UpdateError("STOP")
            response = client.post(
                "/api/v1/agents/" + current["agent_id"] + "/stop",
                json={
                    "type": "agent.stop",
                    "request_id": "update-" + uuid4().hex,
                    "target": current["agent_id"],
                    "scope": current["scope"],
                },
                headers={"Origin": origin, "x-csrf-token": auth["csrf_token"]},
            )
            if response.status_code != 200:
                raise UpdateError("STOP")
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                page = client.get("/api/v1/sessions", params=query)
                if page.status_code != 200 or len(page.json()["items"]) != 1:
                    raise UpdateError("STOP")
                current = page.json()["items"][0]
                if (
                    current["state"] == "STOPPED"
                    and not current["unconfirmed_resource_ids"]
                    and settled_jobs(host)
                ):
                    break
                time.sleep(0.25)
            else:
                raise UpdateError("STOP")
        if not kernel.TerminateProcess(ctypes.c_void_p(handle), 0):
            raise UpdateError("STOP")
        if kernel.WaitForSingleObject(ctypes.c_void_p(handle), 10000) != 0:
            raise UpdateError("STOP")
    finally:
        kernel.CloseHandle(ctypes.c_void_p(handle))


@contextlib.contextmanager
def update_lock(path: Path) -> Iterator[None]:
    import msvcrt

    with path.open("a+b") as handle:
        if not path.stat().st_size:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise UpdateError("LOCK") from None
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


def write_launcher(path: Path, value: dict[str, str]) -> None:
    temporary = path.with_name(path.name + ".update-" + uuid4().hex)
    try:
        temporary.write_text(json.dumps(value, indent=2), encoding="utf8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def open_page(settings: dict[str, str], port: int) -> None:
    suffix = "/opencode/" if settings.get("opencode_ui_dir") else "/"
    url = f"http://127.0.0.1:{port}{suffix}"
    try:
        opened = webbrowser.open(url)
    except Exception:
        opened = False
    if not opened:
        print("Откройте адрес в браузере: " + url, flush=True)


def close_launch(child: subprocess.Popen[bytes], settings: dict[str, str], port: int) -> None:
    """Close only this launch, including the Windows venv interpreter child."""
    try:
        listener = process_at(port)
        host = Path(settings["host_root"])
        if (
            listener is not None
            and own_command(listener[1], host)
            and launched_by(listener[0], child)
        ):
            # If our server is listening, first use the ordinary independent STOP.
            stop_server(host, port, host / "secrets/operator-password.txt")
        if child.poll() is None:
            if sys.platform == "win32":
                try:
                    # The Popen handle pins this exact root PID; never select by image name.
                    run(
                        ["taskkill.exe", "/PID", str(child.pid), "/T", "/F"],
                        code="CLEANUP",
                        timeout=15,
                    )
                except UpdateError:
                    if child.poll() is None:
                        raise
            else:
                child.terminate()
        child.wait(timeout=15)
        remaining = process_at(port)
        if remaining is not None and own_command(remaining[1], host):
            raise UpdateError("CLEANUP")
        from ai_agent_core.local_host.process import exclusive_host

        with exclusive_host(host):
            pass  # Also excludes a pre-bind orphan interpreter holding the host lock.
    except Exception:
        raise UpdateError("CLEANUP") from None


def launch(
    settings: dict[str, str], config: Path, previous: dict[str, str], port: int
) -> subprocess.Popen[bytes]:
    args = [
        settings["python"],
        "-I",
        "-m",
        "ai_agent_core.local_host",
        "serve",
        "--root",
        settings["host_root"],
        "--provider",
        settings["provider"],
        "--panel-dir",
        settings["panel_dir"],
    ]
    if settings.get("opencode_ui_dir"):
        args += ["--opencode-ui-dir", settings["opencode_ui_dir"]]
    if settings["provider"] == "live":
        args += ["--env-file", settings["env_file"]]
    try:
        child = subprocess.Popen(args)
    except OSError:
        write_launcher(config, previous)
        raise UpdateError("STARTUP") from None
    try:
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            if child.poll() is not None:
                raise UpdateError("STARTUP")
            try:
                # No login/model call: the loopback page is enough for server startup.
                if (
                    httpx.get(f"http://127.0.0.1:{port}/", timeout=1, trust_env=False).status_code
                    == 200
                ):
                    listener = process_at(port)
                    if (
                        listener is None
                        or not launched_by(listener[0], child)
                        or not own_command(listener[1], Path(settings["host_root"]))
                    ):
                        raise UpdateError("PORT")
                    print(
                        "Сервер запущен. Агент STOPPED; START — в панели. Окно оставьте открытым.",
                        flush=True,
                    )
                    open_page(settings, port)
                    return child
            except httpx.TransportError:
                pass
            time.sleep(0.25)
        raise UpdateError("STARTUP")
    except Exception, KeyboardInterrupt:
        close_launch(child, settings, port)
        write_launcher(config, previous)
        raise


def update(config: Path, check: bool = False) -> int:
    from ai_agent_core.local_host.layout import load_config, paths
    from ai_agent_core.local_host.process import exclusive_host

    settings = load_launcher(config)
    root = Path(settings["python"]).parent.parent.parent.resolve()
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise UpdateError("CONFIG")
    require_clean(root)
    tools(root)
    if check:
        run(
            [
                settings["python"],
                "-I",
                "-X",
                "utf8",
                str(root / "scripts/check_opencode_deployment.py"),
                "--config",
                str(config),
                "--checkout",
                str(root),
            ],
            code="PREFLIGHT",
            timeout=45,
        )
        print("Готово: настройки, Git, версии, Docker и интерфейс проверены.")
        print("Сервер и личные данные не менялись.")
        return 0
    state = config.parent.parent / "runtime/operations/updater"
    state.mkdir(parents=True, exist_ok=True)
    with update_lock(state / "update.lock"):
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + "-" + uuid4().hex[:6]
        target = config.parent.parent / "runtime/deployment/updates" / stamp
        target.parent.mkdir(parents=True, exist_ok=True)
        print(
            "Получаем обновления текущей ветки и main; локальные commits сохраняются…", flush=True
        )
        ready = candidate(root, target, stamp)
        # Never rebuild the environment that an active server is using.
        if ready != root:
            print("Готовим отдельное окружение и панель из локального кэша…", flush=True)
            python = prepare(ready, settings["python"])
        else:
            python = Path(settings["python"])
        newer = {**settings, "python": str(python), "panel_dir": str(ready / "apps/panel/dist")}
        staging = state / (stamp + ".candidate.json")
        write_launcher(staging, newer)
        print("Проверяем готовность новой версии и Docker…", flush=True)
        run(
            [
                str(python),
                "-I",
                str(ready / "scripts/check_opencode_deployment.py"),
                "--config",
                str(staging),
                "--checkout",
                str(ready),
            ],
            code="PREFLIGHT",
            timeout=45,
        )
        place = paths(Path(settings["host_root"]))
        current = load_config(place)
        if current.recovery is None:
            raise UpdateError("BACKUP")
        print("Останавливаем агента и проверяем завершение заданий…", flush=True)
        stop_server(place.root, current.port, place.password)
        with exclusive_host(place.root):
            if not settled_jobs(place.root):
                raise UpdateError("JOBS")
            backup_args = [settings["python"], "-I", "-m", "ai_agent_core.local_backup"]
            common = ["--root", str(place.root), "--store", str(current.recovery.store_dir)]
            print("Создаём резервную копию. Это может занять несколько минут…", flush=True)
            answer = run(
                [*backup_args, "create", *common, "--ledger", str(current.recovery.ledger_path)],
                code="BACKUP",
            )
            match = re.search(r"created: ([A-Za-z0-9_.:-]+) verified=true", answer)
            if match is None:
                raise UpdateError("BACKUP")
            print("Проверяем резервную копию…", flush=True)
            run([*backup_args, "verify", *common, "--backup-id", match[1]], code="BACKUP")
            result = {
                "previous_commit": git(root, "rev-parse", "HEAD"),
                "new_commit": git(ready, "rev-parse", "HEAD"),
                "backup_id": match[1],
            }
            write_launcher(state / (stamp + ".rollback.json"), settings)
            (state / (stamp + ".result.json")).write_text(json.dumps(result), encoding="utf8")
            write_launcher(config, newer)
        print("Запускаем сервер новой версии…", flush=True)
        child = launch(newer, config, settings, current.port)
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.wait(timeout=20)
        return 0


def start_current(config: Path) -> int:
    from ai_agent_core.local_host.layout import load_config, paths

    settings = load_launcher(config)
    current = load_config(paths(Path(settings["host_root"])))
    state = config.parent.parent / "runtime/operations/updater"
    state.mkdir(parents=True, exist_ok=True)
    with update_lock(state / "update.lock"):
        listener = process_at(current.port)
        if listener is not None:
            if not own_command(listener[1], Path(settings["host_root"])):
                raise UpdateError("PORT")
            open_page(settings, current.port)
            print("Установленный сервер уже работает. Открываю его окно; START не выполнялся.")
            return 0
        child = launch(settings, config, settings, current.port)
    try:
        return child.wait()
    except KeyboardInterrupt:
        child.wait(timeout=20)
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--menu", action="store_true")
    parser.add_argument("--start-only", action="store_true")
    args = parser.parse_args()
    try:
        if args.menu:
            print("\nAI Agent Platform\n")
            print("  1. Обновить с GitHub и перезапустить (Enter)")
            print("  2. Открыть установленную версию без обновления")
            print("  3. Проверить готовность без изменений")
            print("  0. Выйти\n")
            while True:
                choice = input("Выберите действие: ").strip() or "1"
                if choice in {"0", "1", "2", "3"}:
                    break
                print("Введите 1, 2, 3 или 0.")
            if choice == "0":
                return 0
            args.start_only, args.check = choice == "2", choice == "3"
        if args.start_only:
            return start_current(args.config.resolve())
        return update(args.config.resolve(), args.check)
    except KeyboardInterrupt, EOFError:
        print("Действие отменено.")
        return 130
    except UpdateError as error:
        code = str(error)
        print("UPDATE_" + code + ": " + MESSAGES.get(code, MESSAGES["INTERNAL"]))
    except Exception:
        print("UPDATE_INTERNAL: " + MESSAGES["INTERNAL"])
    return 78


if __name__ == "__main__":
    raise SystemExit(main())

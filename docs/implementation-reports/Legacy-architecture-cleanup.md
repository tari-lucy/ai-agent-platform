# Отчёт: вывод старого runtime из эксплуатации

Исполнитель — Claude Code, проверка на Windows — Codex. Бриф:
[legacy-architecture-cleanup-claude.md](../agent-tasks/legacy-architecture-cleanup-claude.md)
(ветка `codex/project-cleanup`).

## 0. База

| Что | SHA |
| --- | --- |
| Рабочая линия при выдаче (`claude/personal-diagnostics`) | `b5e02b5`; в брифе указан `0caa05c`, после него личный host обновлялся пять раз |
| Checkpoint интеграции в main, без удаления кода | `af293c3`: merge рабочей линии в `140551c`. Отличие от рабочей линии — только doc35 |
| База этой ветки (`claude/legacy-runtime-cleanup`) | `b5e02b5` |

Уже в рабочей линии (`dfae998`, развёрнуто, проверено вживую 2026-10-29):

- при включённом харнесе `serve` не поднимает старый драйвер сессий;
- права на интернет, журнал egress и HomeFiles выставляет установка харнеса;
- тест: `tests/harness/test_install.py`.

## 1. Карта зависимостей

Режимы host:

- **H** — харнес включён (doc33): работает у оператора.
- **L** — харнес выключен или не настроен: старый путь, работа через ход-обёртку платформы.

### 1.1. Используется doc33 (H) — оставить

| Блок | Кто использует в H |
| --- | --- |
| `work_session_egress` (`EgressSettings`, `resident_factory`, `log_view`) | `local_host/harness.py`: интернет агента, журнал egress в панели |
| `work_session_home` (`rules`, `volume`) | `harness.py`: правила оператора в промпте, том `aw-home-*` |
| `work_session_wake.WakeSettings` | `harness_autonomy` |
| `sandbox/docker.cli`, `reclaim`, `DockerTaskRunner._remove` | `agent_gateway`, `egress`, `home_files`, `policy.Readiness.reconcile` (уборка после сбоя) |
| `sandbox/egress`, `egress_proxy`, `policy`, `home_files` | харнес, egress, панель «файлы дома» |
| `activity.tools` | `harness.internet()`: проверка права на интернет |
| `WorkSessionsConfig` и CLI `enable-work-sessions` | источник `image_id` и egress/wake/home для харнеса |
| `agent_harness/{worker,workspace_mcp,helper_mcp}.py` | **не импортируются**. Их копируют в образ `scripts/build_opencode_sandbox.{py,ps1}`, `refresh_opencode_harness.py`. Харнес запускается из того же образа (`work_sessions.image_id`) |

### 1.2. Нужен для старых данных — оставить

| Блок | Почему |
| --- | --- |
| `ai_agent_application.agent_activity.sessions.SessionBook` | backup и restore (`local_backup/workplace.py`) |
| `work_sessions` (`sessions`, `transcript`, `mutate`) | панель: история старых рабочих разговоров (`views`, `access`) |
| `work_session_journal`, `work_session_progress` | чтение журнала и прогресса старых разговоров (`work_sessions`) |

### 1.3. Только режим L (старый драйвер)

В H эти модули недостижимы. В L достижимы, и это поддерживаемый запасной режим:

- `local_host/work_sessions.install` → `work_session_opencode.OpenCodeSessions`;
- `work_session_runtime`, `delivery`, `access`, `tickets`, `outputs`, `helpers`, `helper_publication`;
- `work_session_workspace`, `work_session_skills`, `work_session_warm`, `work_session_cpu`;
- `work_session_wake.AutonomousWake`;
- `agent_harness/opencode` (драйвер OpenCode на хосте);
- `sandbox/warm`, `warm_entry`, `bridge`, `entry`, `model_protocol`, `helper_protocol`, `DockerTaskRunner` (запуск задач);
- `activity.AutonomousActivity` (старая самостоятельная работа; в H её tick пропускается);
- поля runtime: `session_harness`, `session_capability`, `session_output_area`, `session_helper_capability`, `session_prewarmer`, `session_cpu`, `autonomous_wake`.

### 1.4. Старый native UI через платформу

`opencode_ui` (`assets`, `protocol`, `sse`, `surface`): маршрут `/opencode/` на порту панели (8765) и патченный бандл OpenCode.

- В H его никто не открывает: кнопка панели ведёт на `127.0.0.1:4096`.
- Он подключается ключом `--opencode-ui-dir`, а его передаёт `start-diag.ps1` на личном host.
- `protocol` и `surface` используют `access.surface`, `home_view`, `ticket_reads`, а также события панели (`apps/panel/src/shared/events.ts`). Нужна построчная проверка, что из этого — общая логика панели.

### 1.5. Неясно — не трогать до проверки

- `workplace_tools.executors` (`serve`): инструменты старых шагов activity. Нужно проверить, вызывает ли их кто-то в H.
- `apps/panel/src/ui/work/*` (экран «Работа агента»: Conversation, sessions, Traffic). Traffic показывает журнал egress, он нужен в H. Остальное — история старых разговоров.

## 2. Решения оператора, нужные до удаления

1. **Запасной режим L.** Убрать работу без харнеса совсем?
   - Если да: host с выключенным харнесом не ведёт рабочих разговоров. При старте ему нужен понятный отказ `HARNESS_REQUIRED`, а не тихое переключение. История старых разговоров остаётся читаемой.
   - Если нет: удалять почти нечего, остаётся только мелкая уборка.
2. **Старый `/opencode/` на 8765.** Убрать?
   - Если да: ключ `--opencode-ui-dir` принимается и игнорируется с NOTICE, чтобы не ломать скрипты запуска.

## 3. Поставки

Пока нет: ждём решений §2.

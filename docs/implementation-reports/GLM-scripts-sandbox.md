# GLM: Python-песочница скриптов, подключённая к обычным рабочим разговорам

2026-10-26, GLM. Поручение оператора: связать существующий механизм исполнения
скриптов с обычным запуском и отдельным разрешением — Опечатка пишет Python-скрипт
(например, разбор JAR/ZIP), исполнение идёт в изолированном контейнере без сети и
установки пакетов. Ветка `codex/glm-scripts-sandbox` от `origin/main` `469a75d`.
Начатой работы Codex в репозитории не было (проверено: ветки/коммиты/WIP отсутствуют).

## Что уже было (main) и чего не хватало

Механизм helper-скриптов полный: `Helpers.call` (`save_script`/`run_script`),
отдельный сетевой-нет контейнер, входы через владельца (скрипт —
`/work/inputs/input-0000`, материалы следом + manifest), результаты и stdout —
обратно файлами с receipt'ами, STOP/epoch/бюджет, идемпотентность по отпечатку.
Не хватало ровно связки: в `WorkSessionsConfig` не было helper-полей, `enable-work-sessions`
не сеял возможность, а `install()` создавал драйвер сессии без `helper_template` —
на обычном локальном запуске скрипты были недоступны.

## Поставка

- **Конфиг** (`local_host/layout.py`): `helper_enabled`, `helper_capability_id`
  (по умолчанию `workplace.scripts`), лимиты контейнера — `helper_max_calls`,
  `helper_wall/cpu_seconds`, `helper_memory_mb`, `helper_pids`, `helper_disk_mb`,
  `helper_output_bytes`, `helper_max_files (≤16)`, `helper_max_input_bytes`
  (до 128 МиБ). Bounds: capability ≠ сессионной, helper cpu ≤ wall, wall < сессионного.
- **`local_host/work_sessions.py`**: `helper_template()` (тот же pinned-образ,
  python-префикс helper'а, без модели; id = сессионный — одно семейство runner'а,
  отдельная capability — только ворота разрешений), `template()` получает
  `max_helper_calls` при включённом helper, `install()` передаёт
  `helper_template`/`helper_capability` в драйвер. `enable-work-sessions` сеёт
  manifest (network none, runner_required) и ready-binding для scripts-возможности
  и добавляет в инструкции сессии упоминание скриптов (обнаружение агентом);
  `ceiling_days` действует и на scripts-возможность.
- **CLI**: `--helper/--no-helper` (None = сохранить текущее), `--helper-capability-id`
  и все `--helper-*` лимиты.
- Безопасность без изменений: сеть закрыта (`--network=none`), пакетов нет
  (только stdlib pinned-образа), останов/отзыв не зависит от контейнера, лимиты —
  конфигурация оператора, право — отдельное решение, текст скрипта прав не даёт.

## Как включить на личном host (после интеграции и обновления deployment)

Та же команда `enable-work-sessions` с добавленными флагами (лимиты — рекомендация
для разборов архивов; helper wall обязан быть меньше сессионного):

```text
python -m ai_agent_core.local_host enable-work-sessions --root <host> \
  --image-id <тот же pinned ID> --area projects --area inbox --output-area projects \
  --ceiling-days 7 --helper --wall-seconds 900 --helper-wall-seconds 600 \
  --helper-cpu-seconds 600 --helper-memory-mb 2048 --helper-disk-mb 512 \
  --helper-max-input-bytes 134217728
```

**Где выдать разовый допуск**: Опечатка сама поднимет обращение
(«Нужен запуск Python-скриптов», capability_request для `workplace.scripts`) —
ответить решением в карточке обращения: уровень «автоматически», срок по потолку,
области минимум `inbox` (где лежат материалы) + `projects` (выходная). Решение
идёт через preview/execute прежнего владельца прав; без него каждый вызов
отказан. Право не запускает агента и не снимает модерацию.

## Проверки

- Новый набор `tests/workplace_launcher/test_scripts_wiring.py`: **3 passed** —
  bounds/шаблоны (вкл/выкл, префикс, потолки), сеяние manifest+binding,
  инструкции с упоминанием, `rt.session_helper_capability` через обычный serve,
  tools видят возможность, без решения `granted_areas` пуст, тикет агента →
  structured decision → области выданы, повтор enable идемпотентен, выключенный
  режим не подключает ничего.
- Затронутые соседи: `test_session_launcher`, `test_helpers`, `test_sessions`,
  `test_workspace` — **21 passed** (после изменения общего enable/install).
- Ruff чистый; `mypy` по полном набору проекта — **Success: 307 файлов**.

## Не проверено здесь / границы

- Реальный Docker-прогон с pinned-образом (механика контейнера покрыта
  `tests/workplace_harness/test_helper_transport.py`, opt-in) — за владельцем
  deployment при обновлении host.
- Передача входа в helper идёт одним payload'ом (base64): пик памяти host ≈ 3–4×
  размера входа; поэтому потолок по умолчанию 64 МиБ, максимум 128 МиБ — для
  больших материалов держать меньше или поднимать осознанно.
- Распознавание PDF/изображений и сторонние пакеты этой поставкой не добавлены;
  inline-замены для отчёта об исполнении — как раньше, через outputs/stdout-tail.

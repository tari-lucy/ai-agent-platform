# Автономный внутренний GM приватного MVP

Дата: 2026-10-15. Исполнитель: Claude Code. Ветка `codex/autonomous-private-gm`, отдельный
worktree, база `origin/main` 325da2c (после интеграции web-dialogue-daily-use, 0eb7bd7).
В main не сливалось.

## Стыки и выбранный путь

- `GameMaster`/`GmPolicy`/story-screen уже были; второго GM, журнала или таймера нет.
- `Runtime.tick` → новый шаг `AutonomousGM` (`m3_composition/gm_cycle.py`) после суточной
  памяти: не более одной GM-задачи за проход через существующие Jobs/`Runtime.submit`.
- Задача `kind="gm"` (существующая) открывает линию оператора (если открытой нет) и вызывает
  `GameMaster.suggest` → черновик модели → story-screen → `Opportunity`.
- Контекст персонажа: `m3_composition/dialogue.py` добавляет одно действующее предложение
  `<offer id version until>` в бюджете контекста. Линия, семейство, броски и topic_key не
  передаются. `DialogueDecision.offer` — новое необязательное закрытое поле
  `{opportunity_id, version, decision: accept|decline}`; старые ответы валидны.
- Переход делает владелец GM по точному id/версии показанного предложения (`accept`/`decline`).
  Принятие запоминает ответ, в котором оно сделано. Опытом (`resolve` → Episode) предложение
  становится, только когда этот ответ доставлен и подтверждён, а собеседник затем написал
  снова; новое сообщение — независимое основание исхода.
- **Линия.** В профиле линий нет, а штатного маршрута создания линии в HTTP нет (есть только
  пауза). Поэтому линия — явный текст оператора в offline-конфигурации, и открывается она
  существующим `open_arc` с подтверждённым опытом как основанием. Фиктивных линий нет;
  без линии GM честно возвращает `NO_LINE`.

## Что изменено

- `local_host/layout.py`: `GmConfig` (enabled, daily_offers 1..8 = 2, daily_calls 2..16 = 4,
  interval_minutes ≥15 = 60, dialogue_floor = 10, линия) внутри `LiveConfig`. Валидация:
  utc_day и резерв памяти + GM + dialogue_floor ≤ общий лимит. `live_roles()` — единое правило
  live-ролей; `assignments_fit()` — правило для backup/restore. Маркер `GM-CHANGE-PENDING`.
- `local_host/gm.py` + `cli.py`: `configure-gm --enable|--disable ...` offline под host lock,
  перевод только ролей game-master/story-screen, без Permission/grants. Повтор завершает
  прерванное изменение. `init.py` и `provider.py` используют `live_roles`; `ROLE_MODELS`
  получил `game-master: Draft`, протокол роли — лимит вывода.
- `m3_composition/service.py`: `PrivateAPIBudget` с GM-полями. Вызовы story-screen относятся
  к фактической операции: GM-задачи имеют детерминированные id по дню/слоту, поэтому
  `memory_used` не считает GM-скрининг, а `gm_used` считает. `gm_room` = min(GM-потолок,
  диалог без резерва памяти − dialogue_floor). Автономная отправка `kind="gm"` разрешена
  только при включённом GM, ACTIVE/OPEN/autonomous/WORK и снятой паузе GM.
- `gamemaster/service.py`: GM читает свою линию приватно и проверяет api_transfer её оснований
  через владельца памяти. На production host прежнее api_transfer-чтение линии GM-владельцем
  всегда давало `ARC_NOT_OPEN`: у GM нет transfer-grant на источники памяти. Добавлены
  `accept(choice=…)` и ограничение промпта возможностями семейства (без публикаций и внешней
  деятельности).
- `serve.py`: GM-настройки в бюджет; семейство `private-conversation` (`m3.dialogue`) и линия
  только при явном включении.

## Наблюдение (без текста и id содержимого)

`GM tick skipped: <КОД>` — GM_PAUSED, RUN_NOT_AUTONOMOUS, NOT_WORK, FOREGROUND_BUSY,
INTENSITY_ZERO, INTERVAL, DAILY_OFFERS, OFFER_PENDING, LINE_NOT_OPEN, NO_LINE, LINE_COOLDOWN,
BUDGET, NO_EXPERIENCE, SLOTS_EXHAUSTED. Плюс `GM job: offered=… reason=…` (OK, UNCERTAIN, HARD:…)
и `GM offer decision …`. Вызовы видны в существующей проекции llm-attempts (role_id);
предложения, линии и решения — в существующих проекциях Opportunity/StoryArc.

## Проверки (Linux; synthetic HTTP provider, ни одного живого вызова)

- `tests/integration_gm` (production `initialise` с публичным профилем, `configure_gm`,
  `build`, настоящие owners/HTTP и live-адаптер поверх MockTransport):
  - путь целиком: подтверждённый опыт → автоматический tick → линия + черновик GM + 2 скрининга
    (3 вызова, все в GM-учёте, память 0) → `OFFERED`. Следующий настоящий запрос персонажа
    содержит `<offer>` и не содержит линии, семейства, бросков и topic_key. Принятие по id/version
    → `ACCEPTED`; после подтверждённой доставки и новой реплики → `RESOLVED` + Episode.
    Повтор tick → `OFFER_PENDING`;
  - пропуски без вызовов: STOPPED, пауза GM в новом запуске, нет опыта, нет линии, ответ
    диалога в процессе (`FOREGROUND_BUSY`), бюджет на границе резерва памяти и dialogue_floor
    (gm_room = 2 < 3), GM-потолок;
  - отказ story-screen (`uncertain`) → `BLOCKED`, следующий ответ предложения не видит.
    STOP во время ответа модели GM → задача не SUCCEEDED, ничего не применено. Restart →
    STOPPED, затем `INTERVAL`, без повторных и дополнительно оплаченных вызовов. Отказ
    персонажа → `DECLINED`, разговор продолжается, линия отдыхает (`LINE_COOLDOWN`, без вызова);
  - offline: по умолчанию выключено, `GM_LINE_REQUIRED`, неверный бюджет, `HOST_ALREADY_RUNNING`
    под чужой блокировкой, маркер закрывает serve и снимается повтором, роли
    `character/game-master/story-screen`, `assignments_fit` закрывает старый snapshot,
    выключение возвращает mock, старый host.json без `gm` читается как выключенный.
- Один затронутый набор (gamemaster, dialogue, development, integration_dialogue, local_host,
  integration_gm, memory_quality, m3_runtime): 283 passed, 4 skipped, 2 failed.
  - `local_host/test_memory_budget.py` упал из-за моего кода (строки без `operation_id`):
    исправлено, повтор этого файла — 5 passed, повтор основного пути GM — passed.
  - `m3_runtime/test_workflows.py::test_post_session_uses_existing_extractor_consolidator_and_reflector`
    падает и на чистой базе 325da2c (проверено в отдельном временном worktree) — не связано.
  - Полный набор целиком после исправления не повторялся.
- Ruff, ruff format и strict mypy (215 файлов) чисты.

## Не реализовано и ограничения

- Предложение доходит до настоящего запроса персонажа (проверено по телу запроса к
  synthetic-модели). Качество предложений и решений настоящей модели не проверялось:
  scripted-ответ доказывает маршрут и правила, а не содержание.
- Отдельными тестами не покрыты: истёкшее предложение (фильтр `expires_at` и существующий
  `expire_due` в tick), отзыв основания предложения во время ответа (основания добавляются в
  `extra_sources` и проверяются существующим `verify`), TEST-сценарий (production host — только
  WORK; есть проверки в коде).
- Каждый новый запуск начинается с паузы GM (существующая модель supervisor); оператор снимает
  её в «Сюжет и развитие». Интервал после каждого открытия runtime отсчитывается заново.
- Линия одна и задаётся оператором; в профиле линий нет. Семейство одно:
  «приватный разговор». Инициативы без входящей реплики нет.
- Windows не проверялся. Короткий synthetic smoke для координатора:
  `python -m pytest tests/integration_gm/test_gm_path.py tests/integration_gm/test_gm_offline.py -q`
  (PYTHONPATH как у `tests/memory_quality` плюс `tests/integration_gm`).

## Запросы координатору

1. `local_backup/compose.py`: передать `assignments_fit=ai_agent_core.local_host.layout.
   assignments_fit` в `HostCompatibility(host, config, ...)`. Сейчас restore сравнивает
   bindings только на равенство с живой БД. Проверка: snapshot до `configure-gm --enable`
   отклоняется как `ROLE_BINDINGS_DIFFER`.
2. При желании видеть причины пропуска GM в панели: read-проекция последнего
   `AutonomousGM.last_reason` (фиксированный код). UI в эту задачу не входил.

## Рабочее включение (применяет оператор после интеграции)

1. Остановить host.
2. Подготовить файл линии (до 1000 символов, только оператор).
3. `configure-api-budget` при необходимости: резерв памяти 8 + GM 4 + пол диалога 10 ≤ лимит.
4. Включить GM:
   `python -m ai_agent_core.local_host configure-gm --root <host> --enable --line-direction-file <файл> [--line-boundaries-file <файл>]`.
5. `serve`, затем START (autonomous) и снять паузу GM в «Сюжет и развитие».

Выключение: `configure-gm --root <host> --disable`.

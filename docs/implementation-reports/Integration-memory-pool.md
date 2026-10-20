# Интеграция пула памяти и развития

Координатор: Codex. Дата: 2026-10-20. База `638162c`, код после review `bf6e48d`.
Приёмка относится к объединённому коду и synthetic-проверкам; качество ответов
реальной модели и полная M5-приёмка этим отчётом не объявляются.

## Принятые ветки

Ветки объединены собственными merge-коммитами координатора в указанном порядке,
без включения вспомогательных merge-коммитов `probe` из предпросмотра.

| Порядок | Ветка codex/ | Принятый HEAD |
| --- | --- | --- |
| 0 | fix-baseline-tests | `7cee7ca` |
| 1 | long-term-memory-retention | `851a2bc` |
| 2 | private-social-continuity | `4ec4b28` |
| 3 | long-term-knowledge | `e945b25` |
| 4 | grounded-character-biography | `261c103` |
| 5 | development-manifestation | `bf339dd` |
| 6 | memory-backlog-recovery | `8865e7e` |
| 7 | knowledge-digest | `966a82d` |

Исправление социальной identity `798065c` входит в HEAD этапа 2.
Конфликт этапов 6/7 в memory_cycles.py, schemas.py, service.py разрешён
с сохранением обеих функций. Ранний return дайджеста стоит перед обычной
консолидацией; у continue_failed сохранён собственный return False.
Правка scope в MemoryService._transition присутствует один раз.
До review-доработок diff приложения с веткой предпросмотра пуст;
различаются только три исправленных baseline-теста из этапа 0.

## Решения владельца shared-зон

- Принят optional `RecordRepository.recent(cites=...)`: существующий SQLite
  adapter, текущие source_links, прежние ACL/purpose/TTL/scope проверки.
- По прямому разрешению оператора принят SOCIAL в матрице private host:
  собственная запись; чтение MEMORY/CHANNEL для оснований и подтверждения
  доставки; SOCIAL не получает API-transfer. Оператор читает social-проекции
  через loopback и закрывает точное обещание командой `promise.close`.
  Схемы/OpenAPI/TS соответствуют контрактам; миграций/зависимостей нет.
- Автономный consolidate принят только для непустого `digest_of`, в WORK,
  при включённой обработке памяти, ACTIVE/OPEN/autonomous и текущем scope.
  Остальные consolidate требуют операторского допуска. Расходы дайджеста
  входят в существующие общий лимит и резерв памяти, отдельной квоты нет.
- Приняты ограниченные defaults выдержек: 365 дней от исходного события,
  подготовка за 7 дней до TTL raw, максимум 600 символов на Source.
  Полный разговор по-прежнему истекает через 30 дней. Дайджест не удаляет
  дневные сводки; срок старше года не расширен, права не выдаются моделью.

## Review-доработки координатора

1. **Основание, не попавшее в выдержку.** Усечение по размеру или готовая
   выдержка для другого claim могли перелинковать поздний факт на текст,
   где его нет. Перед перелинковкой проверяется непустое покрытие слов
   исходного основания в выдержке; при нехватке остаются прежняя ссылка и
   TTL. Это консервативная защита от потери основания, не семантический судья.
   Две регрессии: факт за пределом 600 символов и поздний claim при уже
   существующей выдержке. Бессрочная копия raw не создаётся.
2. **Повтор promise.close.** После потерянного ответа старая expected_version
   скрывала успешную квитанцию. Теперь доступ к текущему обещанию проверяется,
   затем владелец сверяет ту же команду по сохранённой transaction receipt.
   Повтор возвращает тот же результат; подмена payload и новый stale request
   отвергаются. Новое основание выполнения не создаётся второй раз.
3. **Полный набор оснований дайджеста.** Scheduler передавал только первые
   20 Source IDs, хотя Digester читал до 200. Теперь закрепляются все основания
   до принятого предела 200. TaskInput допускает >20 только для дайджеста;
   предел остальных задач прежний. Digester отклоняет новый неприкреплённый
   источник/версию и после генерации проверяет актуальность сводок и прав.
   Защита STOP/lease/usage не заменяется проверкой только в worker.
4. Перенесена длинная строка browser-теста; runtime-семантика не менялась.

## Windows-проверки

Изолированный checkout, отдельные временные данные. Системный Python 3.13
не использовался для приложения. Прежний рабочий venv отсутствовал:
восстановлен Python 3.14.8 и editable venv через uv 0.12.23,
`uv sync --locked`. Вспомогательная ошибка uv minor-link обойдена точным
путём реально распакованного интерпретатора. Исходники venv относятся к этому
checkout; PYTHONPATH добавляет только test helpers из check_combined.py.
Никаких ключей/.env для диагностики, платных вызовов, контейнеров или моделей.

- Один совмещённый Windows-набор: memory_quality, retention, social,
  retained excerpts, memory lifecycle, storage, contracts — **226 passed,
  829.66 с**. Он не включает native sqlite-vec suite и весь проект.
- После правки выдержек повторены только retained excerpts/их retention-путь
  и admission — **14 passed, 4.79 с**. После полного закрепления оснований
  дайджеста — два новых guard-сценария и admission **11 passed, 2.84 с**.
- Исправленный baseline privacy-тест conflicts отдельно: **1 passed**.
- Ruff, strict mypy (**224 source files**), import boundaries и generated
  contracts/OpenAPI/TS `export --check` — чисто на финальном коде.
- Node **24.19.0**, npm **11.17.0**, `npm ci --ignore-scripts` по lockfile:
  **183 tests passed**, eslint, tsc/Vite build чисто. Осталось обычное
  предупреждение размера JS chunk; зависимостей/lockfile в diff нет.
- Первый pytest без helper-путей остановился на collection (13 ImportError);
  после добавления существующих HELPERS сборка и совмещённый набор прошли.
  Первое lint-замечание E501 в browser-тесте исправлено.

Финальный узкий runtime/browser прогон после review-правок: **34 passed,
141.07 с**. В него вошли production-host digest и точный replay promise.close,
lease renewal, исправленные workflow/laboratory baseline-сценарии, isolated import,
суточные циклы и Edge-проверки promise close/development/guidance memory.
Остались 5 deprecation warnings websockets/Uvicorn; реальных paid calls — **0**.
Gate объединённого кода пройден. Полный проектный suite намеренно не повторялся.

Воспроизведение helper-путей в PowerShell (из принятого checkout):

```powershell
$env:PYTHONPATH = (& .venv/Scripts/python.exe -c "import os; from scripts.check_combined import ROOT, HELPERS; print(os.pathsep.join(str(ROOT / p) for p in HELPERS))")
$env:AI_PANEL_BROWSER_CHANNEL = 'msedge'
```

Основной набор: `tests/memory_quality tests/retention tests/social
tests/memory/test_retained_excerpts.py tests/memory/test_lifecycle.py
tests/storage/test_storage.py tests/contracts --import-mode=importlib -q`.
При последующих изменениях повторять только затронутые части, не весь список.

## Рабочий host и границы

Перед обновлением подтверждены свободный OS-lock, отсутствие nonterminal jobs
и отсутствие нестандартных work grants: 26 имеющихся строк являются подмножеством
30 строк принятой матрицы. enable-social не затирает ручных дополнительных прав.
Секреты и тексты рабочей биографии при этой проверке не читались.

Перед правками создана штатная manual-копия
`bk-20261020T201021-manual`, `verified=true`, без secrets.
Факт offline enable-social и обновления launcher/panel фиксируется после
выполнения. Restart должен остаться STOPPED; ни START, ни платная проверка
качества не входят в это обновление.

Остаются открытыми:

- StateChange/Episode не перелинковываются на выдержки; причины старого
  развития могут перестать быть доступными через 30 дней.
- Исходные сводки после дайджеста сохраняются; автоматическое их удаление
  и хранение знаний старше 365 дней требуют отдельного решения оператора.
- В backlog не добавлены новые shared поля failure/точных счётчиков;
  неизвестный исход не превращается в автоматический retry.
- Обещание закрывает оператор, намерение закрытия от модели не подключено.
- Наблюдаемый последующий опыт рядом с изменением не доказывает качество
  проявления черты. Нет реальных модельных вызовов, многомесячной нагрузки,
  полного suite/20×3 quality campaign или новой полной аварийной приёмки.

Инструкция: [local host](../local-host-setup.md).
Текущий функционал: [MVP](../mvp-current-state.md).

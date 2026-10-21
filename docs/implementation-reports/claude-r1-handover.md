# Передача Claude → Codex перед R1 (AW-08, AW-05-R)

Дата: 2026-10-21. Автор: Claude Code. Base: main `ef80b68`. Документ, код не менялся.
Основание: [уточнение D62/D63](../agent-tasks/workplace-agency-revision.md).

## 1. AW-08: что передаётся

**Начатой работы нет.** Ветки, коммитов и черновиков AW-08 у Claude нет; OpenCode не
скачивался, Docker не ставился (на Linux-VPS нет ни того, ни другого). Удалять или
сливать нечего. Решение AW-00 по OpenCode (кандидат v1.18.35, `permission` deny/ask,
скрытые title/summary/compaction/small_model, `OPENCODE_CONFIG_CONTENT`, сеть закрыта)
остаётся в [AW-00](AW-00.md) как исходные данные для выбора R1, а не как проверенный факт.

Все ветки Claude (`codex/aw-00…07-*`, `codex/aw-01-message-reads`,
`codex/fix-memory-rights-flake`) уже в main, рабочие копии чистые.

## 2. Зона, которую Claude больше не правит

С этого момента writer — R1 (Codex): `agent_workplace/ports.py`, `agent_activity/*`,
`m3_composition/activity.py`, `m3_composition/workplace_tools.py` (executors библиотеки —
часть tool bridge), session contracts/модули, `agent_harness`. Claude возвращается к ним
только по явной короткой передаче окна. R2-зона Claude: `agent_experience/*`,
`m3_composition/workplace_experience.py`, `tickets/service.py` и новый owner release module.

## 3. Стыки текущего AW-05, которые важно не потерять

1. **Допуск = записанная попытка.** `WorkAdmission.attempt_id` — id `WorkAttempt`
   (`<task>.aNNNN`), не id аренды задания. Файловый владелец ищет попытку по нему.
2. **Права файлов по capability попытки.** Перед IO шаг вызывает `ActivityBook.bind`
   (capability и её версия в попытке). `authorize` файлов пускает в область только при
   текущем automatic `Permission` держателя `workplace-agent` на эту capability и область
   из `conditions.areas`. Инструмент сессии должен пройти тот же путь.
3. **Lineage.** Входы попытки = источники брифа личности + всё, что прочитали прошлые шаги
   задачи (`WorkTask.progress[].read_*`). Иначе записанный файл теряет происхождение, а отзыв
   прочитанного его не закроет. Бриф показывает только источники, видимые ACTIVITY
   (право ACTIVITY→MEMORY только на чтение и только при включённой деятельности).
4. **Модель только через общий шлюз.** `ExecutionModelGateway` → `Execution.generate` →
   `LLMGateway`: журнал попыток, доля `activity` в `PrivateAPIBudget`, отмена. Роли
   `activity`/`activity-goal` в `OPT_IN_KINDS` (init их не включает). Любой внутренний вызов
   OpenCode (повтор, сжатие, title) должен идти сюда же или быть выключен.
5. **Опыт пишет владелец деятельности после шага, не executor:**
   `ExperienceRecorder.step` (успешный шаг с записанными файлами → observation + один
   эпизод `work-<attempt>`) и `.conclusion` (вывод → interpretation, без эпизода). Ключи по
   попытке — replay не удваивает. R2 опубликует порт контекста/опыта для сессии.
6. **Тикеты и права.** Недоступная capability → тикет `capability_request` (areas из
   manifest), ждёт только зависимая задача; `satisfied()` продолжает её, когда capability
   стала доступна или в ней отказали. Выдача — структурное решение AW-01; потолок пишет
   `enable-workplace-files --ceiling-days N`. Учтённый ответ на вопрос агент закрывает сам.
7. **Пределы.** `max_attempts_per_task` (по умолчанию 6) молча останавливает задачу в
   `active`; `NO_WORK` — без вызова модели; деятельность уступает активным заданиям
   (`FOREGROUND_BUSY`). Оператор: `work.assign` (принимается в STOPPED) и `work.abandon`
   (закреплён версией, replay по request_id).
8. **Проверки, которые сейчас держат эти свойства:** `tests/workplace_activity/*`
   (включая `test_library_tools.py`, `test_work_commands.py`), `tests/workplace_experience/*`,
   `tests/workplace_skills/*`, `tests/panel/test_work_browser.py`, `test_library_browser.py`.

## 4. Найдено для R2 (недочёты AW-01, исправит Claude)

- `tickets/service.py::_merge` оставляет последние `MAX_LINKS = 50` ссылок `sources`/
  `materials` при каждом обновлении тикета: более старые основания теряются, хотя тело и
  сообщения могут от них зависеть.
- `tickets` и `ticket-messages` отдаются общим reader: тело не проверяется на отзыв/TTL/
  ledger своих оснований. Первый безопасный вариант по brief — скрывать неразделимое тело,
  если закрыто любое основание; файлы — через guard файлового владельца.

## 5. Что понадобится R3 от R1

Опубликованные session-команды и события, и короткие окна на shared-файлы:
`contracts/api.py` (типы команд), `catalogue.py` (read-модели, exports), панельные
`events.ts`/`validation.ts`, dispatch в `m3_composition/access.py`. Генерацию exports ведёт
текущий writer R1, затем передаёт для совместимых добавлений R3.

## 6. Известное по проверкам

- Браузерный START → STOP: ждать `.active-run` «Работает» (поиск текста «Работает» без
  учёта регистра совпадает с «Агент не работает»; STOP по устаревшему запуску даёт
  штатный `STALE_CONTEXT`).
- Под параллельной нагрузкой тесты со временем падают ложно; повтор отдельно. На этой
  машине `test_guards::test_import_has_no_effects` падает всегда (устаревший venv).

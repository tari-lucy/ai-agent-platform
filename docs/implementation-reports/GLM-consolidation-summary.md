# GLM: сводка сведённой приёмочной ветки OpenCode

2026-10-24. Поручение Codex (через оператора): свести приёмочные тесты и инструкцию
GLM с правкой Codex `60a02e9` в отдельную чистую ветку, проверить ссылки, дать
короткую сводку. Без повторного полного gate и без push в main — интеграцию ведёт
Claude/Codex. Соответствует шагу 1 [handoff-brief](../agent-tasks/opencode-final-handoff-glm.md)
(ветка `codex/oc-07-final-windows` `dfb905d`).

## Ветка и состав

`codex/glm-acceptance-consolidated`. Base: `origin/main` `9e7f268`. Сведено (без
force/reset, одна версия каждого отчёта):

| Что | Коммит в ветке | Исходный коммит |
| --- | --- | --- |
| OC-05: единый начальный допуск | `09581ac` | `claude/oc-05-initial-access` |
| OC-05: replay + grounds в транзакции | `f809a6a` | там же |
| OC-05: handover-заметка Claude | `0b20b77` | `56e1b41` |
| Инструкция первой сессии | `61f30a5` | `codex/glm-first-session-doc` |
| Уточнения Codex к инструкции | `60a02e9` | `codex/oc-first-session-review` |
| Тест: независимая проверка OC-05 | `cde82b3` | `97274b3` (`codex/glm-oc05-acceptance`) |
| Тест: изоляция двух личностей/lifecycle | `fae2ba0` | `e0f39fb` (`codex/glm-opencode-acceptance`) |
| Отчёт GLM A/B/C | `f78db06` | `a13887d` |
| Отчёт GLM финальный (части 1–2) | `6b9b48b` | `c03a108` |

## Проверки на этой ветке

- Оба приёмочных файла: **3 passed, 90.6 s** (PYTHONPATH по SOURCES+HELPERS
  `scripts/check_combined.py` + `tests/workplace_access`, `--import-mode=importlib`).
- Относительные ссылки четырёх документов ветки: 4/4 разрешаются.
- Полный gate не повторялся (поручение); ничего другого не менялось.

## Что готово

- Native-клиент: серия патчей 1–8 на main; Linux-браузерные и Windows/Docker
  сценарии закрыты Claude и Codex (отчёты в main).
- OC-05: реализован и исправлен на `f809a6a`; повторный review Codex `d40fec4`
  закрыл оба замечания; мои четыре независимые проверки прошли (replay после
  смены состава, устаревшие manifest/binding/policy, откат набора, поздняя
  capability).
- Инструкция первой сессии с уточнениями Codex; приёмочные тесты и отчёты GLM.

## Коммиты ещё не в main (main = `9e7f268`)

Вся ветка `codex/glm-acceptance-consolidated` (таблица выше) + вне её:
`codex/oc-05-cross-review` `d40fec4` (review-документ), `codex/oc-07-final-windows`
`dfb905d` (handoff-документы). Ветки-источники GLM (`codex/glm-*`) после сведения
можно считать архивными; повторно их не подмешивать.

## Какие проверки остались

1. **TS2741** — единственный оставшийся блокер личного включения. Исправление
   Claude не опубликовано; по handoff-brief — минимальный патч к pinned v1.19.3
   (тип `awFetch`, `Platform.fetch`, тест-заглушка; без ослабления tsconfig),
   затем upstream typecheck + затронутые Bun-тесты; production-сборку делает Codex.
2. **Хвост общего gate Claude**: pytest был оборван на 86% (1296/1493); 6 падений
   векторной памяти воспроизводятся на чистом main (нет `sqlite_vec` в venv) и к
   пакету не относятся — дозапустить недостающий хвост, не объявляя полный suite
   passed и не ослабляя проверки.
3. **Windows/Deployment** — финальный production gate и переключение личного host
   по runbook остаются за Codex/оператором (OC-07) после закрытия TS2741.

## Блокеры и улучшения на потом

Блокер: TS2741 (см. выше). Улучшения на потом: дедупликация повтора patch 6 по
`stored.text === text` (UX-край); незначительные наблюдения runner/SSE из отчёта
A/B/C. Новые migrations, права, платные API и изменение личного host в задачу
сведения не входили и не выполнялись.

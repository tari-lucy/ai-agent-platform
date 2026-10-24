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

## Коммиты ещё не в main

Сверка после выхода патча 9: main = `f5324ed`; **OC-05 уже в main** (`09581ac`,
`f809a6a`, `56e1b41` — Claude свёл свой пакет). Из ветки сведения в main ещё не
вошли: инструкция `61f30a5` + уточнения Codex `60a02e9`, тесты GLM `97274b3`/
`e0f39fb`, отчёты GLM (`a13887d`, `c03a108`). Вне ветки: `d40fec4` (review-документ
Codex), `dfb905d` (handoff-документы). Ветки-источники GLM (`codex/glm-*`) после
сведения можно считать архивными; повторно их не подмешивать.

## Какие проверки остались

1. ~~TS2741~~ — **закрыт патчем 9** (см. дополнение ниже): патч в main
   (`7225ab7`, main `f5324ed`), upstream typecheck серии 1–9 подтверждён
   независимо (exit 0).
2. **Хвост общего gate Claude**: pytest был оборван на 86% (1296/1493); 6 падений
   векторной памяти воспроизводятся на чистом main (нет `sqlite_vec` в venv) и к
   пакету не относятся — дозапустить недостающий хвост, не объявляя полный suite
   passed и не ослабляя проверки.
3. **Windows/Deployment** — финальный production gate и переключение личного host
   по runbook остаются за Codex/оператором (OC-07).

## Блокеры и улучшения на потом

Блокеров личного включения из моей зоны не осталось. Улучшения на потом:
дедупликация повтора patch 6 по `stored.text === text` (UX-край); незначительные
наблюдения runner/SSE из отчёта A/B/C. Новые migrations, права, платные API и
изменение личного host в задачу сведения не входили и не выполнялись.

## Дополнение: патч 9 и подтверждение typecheck серии 1–9

Main продвинулся до `f5324ed71d62fcc5046b88ed52891fa100c18bf5`; среди новых
коммитов — `7225ab7` «OC native client: patch 9 closes TS2741 preconnect in the
bridge fetch» (автор Claude). Патч закрывает дефект минимально и без ослабления
типов: `bridgeFetch` получает явную сигнатуру
`(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>`, экспорт
`awFetch: typeof globalThis.fetch` собирается через `Object.assign(bridgeFetch,
{ preconnect: () => {} })` — намеренный no-op (свойственных соединений у bridge
нет), без каста и без расширения tsconfig; тест-заглушка получает тот же no-op.

Независимое подтверждение (поручение Codex: только typecheck, без повторного
gate и Windows-сценариев):

- серия **1–9** из `origin/main` `f5324ed` применена `git am` к чистому тегу
  `e3c4eb242585dcd77be1a867391b7c25fd47ce52` во свежем клоне (core.autocrlf=false,
  локальный `.gitattributes`) — все девять легли чисто;
- `git archive | tar -x` (настоящие symlink'и) → контейнер `oven/bun:1.3.14`,
  `bun install --filter '@opencode-ai/app' --ignore-scripts --frozen-lockfile`,
  `bun run typecheck` (`tsgo -b`) в `packages/app` — **exit 0**, ошибок нет
  (до патча 9 те же условия давали 2×TS2741);
- production-сборка, полный suite и браузерные/Docker сценарии не повторялись
  (закрыты владельцами; вне этого поручения).

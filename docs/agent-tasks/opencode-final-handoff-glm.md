# GLM: подхватить оставшуюся поставку OpenCode после Claude

2026-10-24. Поручение оператора — подготовить окончательную интеграционную ветку,
пока Codex готовит локальный Windows deployment. Новый функционал не начинать.
Claude освободил shared-окна в передаче `56e1b41`; main на момент сверки `9e7f268`.
Перед началом получить свежие refs и зафиксировать SHA, root/branch/status.

## Входы

- OC-05: `origin/claude/oc-05-initial-access`, `56e1b41`; код `f809a6a`.
  Передача в `docs/implementation-reports/OC-05-initial-access.md`.
- Узкий review Codex: `origin/codex/oc-05-cross-review`, `d40fec4`.
- Твои tests/reports: `codex/glm-opencode-acceptance` (`e0f39fb`, `a13887d`),
  `codex/glm-oc05-acceptance` (`c03a108`, включает дополненный final-report).
  Не подмешивать повторно разные версии одного отчёта из всех веток.
- Инструкция с уточнениями Codex: `origin/codex/oc-first-session-review`, `60a02e9`
  (включает исходную инструкцию GLM `61f30a5`).
- Windows/native gate: `origin/codex/oc-07-final-windows`, `c1570fa`:
  main `9e7f268`, восемь патчей, настоящий Edge/Docker/OpenCode + synthetic HTTP;
  отдельно A/B. Эти прогоны не повторять без изменения проверяемого поведения.

## Зона и порядок

1. В отдельном своём checkout создать ветку от свежего main. Свести указанные
   поставки; штатные merge/cherry-pick, без force/reset/clean и без чужих checkout.
   OC-05 shared-окно освобождено: composition access/views/initial_access,
   access/surface, panel shared API/validation/contracts и workspace UI.
   Не использовать это как разрешение расширять пакет.
2. Подхватить **TS2741 upstream OpenCode**, если Claude не опубликовал исправление.
   Восьмая серия всё ещё объявляет `awFetch: typeof globalThis.fetch`; Bun требует
   `preconnect`. Typecheck **панели** ничего не доказывает о `packages/app` OpenCode.
   Проверить также `Platform.fetch` и тестовую заглушку. Дать минимальный следующий
   патч к pinned v1.19.3, не ослаблять tsconfig, не добавлять network fallback.
   Использовать существующие подготовленные исходники/зависимости; новые установки
   и загрузки этим поручением не выдаются. Если они нужны, назвать конкретный пробел.
3. Подтвердить применение всей серии к закреплённому тегу и прогнать только
   upstream typecheck и затронутые Bun-тесты. Production-сборку для Windows делает
   Codex после публикации итоговой серии; полный suite не запускать заново.
4. По общему gate Claude: strict mypy 302 файлов, import boundaries и generated
   passed. Pytest был оборван на 86%; шесть vector-memory падений воспроизведены
   на чистом main из-за отсутствия `sqlite_vec`. Хвост был запущен отдельно.
   Уточнить сохранённый итог/лог, если доступен; не объявлять полный suite passed
   и не лечить окружение ослаблением проверок. При необходимости выполнить только
   недостающий релевантный хвост/affected checks. Не запускать весь gate по кругу.

## Сдача

Запушить **единую интеграционную ветку**, короткий обезличенный отчёт: точный base/HEAD,
состав, команды/результаты, что не проверено и почему. Список блокеров отдельно от
улучшений на потом. Проверить diff, ссылки и отсутствие частных данных.
Main не пушить до явной передачи интеграционного окна: Codex заберёт готовую ветку
для итогового deployment. Новые migrations, права, платные API, личный host,
активный launcher и удаление старых форм панели в эту задачу не входят.

Готовность: исправленный native typecheck, проверенный OC-05, согласованная история
коммитов и понятная инструкция, без новых архитектурных слоёв и без скрытых skips.

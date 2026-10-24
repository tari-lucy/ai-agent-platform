# OC-07: кандидат с OC-05 и native-патчем 9

2026-10-24, Codex. Ветка `codex/oc-07-release`, база принятого main `2e0dffd`.
Подготовка обновления Windows deployment после поставок Claude; личный запуск
и его три права не менялись. GLM проверяет upstream typecheck независимо.

## Что обновлено

- Неактивный deployment-кандидат сведён с main обычным merge `35e22cf`.
  `git diff origin/main HEAD` пустой: дерево точно соответствует `2e0dffd`.
  Чужой checkout не переключался, force/reset/clean не применялись.
- Панель пересобрана с OC-05: Node 24.19.0, `npm run build` (включая
  `tsc --noEmit`) — exit 0. Это проверка панели, не upstream typecheck OpenCode.
- Native UI: закреплённый OpenCode v1.19.3
  `e3c4eb242585dcd77be1a867391b7c25fd47ce52`, Bun 1.3.14, патч 9 поверх
  проверенной серии 1–8. Независимый Git index от чистого тега с полной серией
  1–9 дал то же дерево, что фактически собранные исходники.
- Vite `build --base /opencode/`, `OPENCODE_CHANNEL=prod` — exit 0.
  После штатной очистки media/map/`_headers` упаковка и `assets.load` успешны:
  **938 файлов / 34 352 287 байт** без manifest, в пределах 1024 / 48 МиБ.
  Новых установок, загрузок моделей и изменений lockfile нет.

## Единственная новая runtime-проверка

`tests/panel/test_initial_access_browser.py` — **1 passed, 27.16 с**, Windows/Edge,
настоящие init/serve/owners временного host. Новый набор из двух возможностей:
preview → до флажка выдача недоступна → одно подтверждение → оба обычных права
«Разрешено» → прежний запрос закрыт владельцем. Модель не вызывается;
подключение `.invalid` синтетическое, личный `.env` не читался.

Использованы Python 3.14.8, проектные SOURCES/HELPERS + `tests/panel` в PYTHONPATH,
`AI_PANEL_BROWSER_CHANNEL=msedge`; проверялась только что собранная панель.
Сценарий прошёл с первого прогона; исправлений кода не потребовалось.

Прежний настоящий Docker/OpenCode/native Edge journey относится к серии 1–8
на `9e7f268` (отчёт `c1570fa`), он **не повторён**. В патче 9 добавлен no-op
`preconnect`, маршрутизация не менялась; финальная серия собрана/упакована,
но совместный native runtime/Docker journey серии 1–9 здесь не объявляется passed.
Полный suite и Linux/Bun typecheck не дублировались; upstream typecheck подтвердил
автор, независимое подтверждение GLM ещё ожидается на момент этой записи.
Шесть baseline vector-memory падений и шесть upstream persistence unit-падений
из отчётов Claude не скрываются этой проверкой и здесь не исследовались заново.

## Подготовка переключения

Запечатан отдельный runtime artifact `opencode-ui-2e0dffd`; прежние сохранены.
Только неактивный launcher указывает на новый artifact, Python кандидата и новую
панель. Новый локальный receipt —
`runtime/deployment/opencode-release-staging.json`: ревизии, хеши manifest/панели,
результат Windows OC-05 и preflight. Прежний receipt помечен заменённым,
локальный cutover plan актуализирован. Машинные файлы/сборки в Git не входят.

Read-only preflight — **exit 0**, все шесть кодов OK, включая RUNNER_OK и
OPENCODE_UI_OK. Хеш активного launcher прежний. Backup/restore, выдача прав,
новый личный serve и START не выполнялись. Никаких платных API.

После независимого подтверждения GLM остаётся согласованное окно: остановить
старый serve при известных исходах, штатный backup с существующим ledger и verify,
переключение launcher и проверка STOPPED. START — отдельное действие оператора.
Этот отчёт сдаёт подготовленный кандидат, не утверждает, что личный OpenCode уже
обновлён или что весь MVP прошёл полный gate без пропусков.

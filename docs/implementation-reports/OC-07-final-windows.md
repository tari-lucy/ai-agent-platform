# OC-07: финальный native UI на Windows, main 9e7f268

2026-10-24, Codex. Ветка `codex/oc-07-final-windows`, отдельный checkout
от принятого `origin/main 9e7f268`. По запросу Claude повторена Windows/Docker
приёмка **восьми** патчей. Production-код, контракты и права не менялись.

## Сборка

- Upstream v1.19.3 `e3c4eb242585dcd77be1a867391b7c25fd47ce52`, Bun 1.3.14.
  К ранее проверенному исходнику применён патч 8. Независимый Git index
  с нуля от тега + вся серия 1–8 дал точно то же дерево, что собранный исходник
  `fc008121fe1f79c964660fb628e421b0466a6afc`.
- Vite `build --base /opencode/`, `OPENCODE_CHANNEL=prod` — exit 0.
  После штатного удаления media/map/`_headers` из собственного нового каталога
  `prepare/load` приняли manifest: **938 файлов / 34 352 238 байт** без manifest.
  Лимит 1024 / 48 МиБ соблюдён. Новых загрузок/установок и изменений lockfile нет.
- Панель: Node 24.19.0, `npm run build` с `tsc --noEmit` — exit 0.
  Предупреждения Vite о размере chunks не являются ошибками сборки.

## Затронутые проверки

`tests/panel/test_native_opencode_sessions.py` — **1 passed, 67.12 с**.
Настоящие Edge, Docker Desktop server 29.4.0 и OpenCode-runner; production
owners временного host, синтетический HTTP-провайдер. Ровно один вызов модели,
платных вызовов нет. Отправка → reload в STOPPED → явный START → первый допуск
через панель → ответ → ACK только точного видимого ответа → character в истории
→ reload без повторного вызова → STOP. Скрытая вкладка/обрезанный ответ не ACK.

`tests/panel/test_native_opencode_personas.py` — **1 passed, 48.72 с**.
Edge, production owners и синтетический harness/модель. B добавлена настоящей
offline-командой на временном host, без подмены scope. Обе личности видны;
сообщение B сохраняется и ждёт её запуска, пока после START отвечает только A.
Запрос A содержит её marker, но не marker B. Возврат к B и reload сохраняют
разговор, ответ A там не появляется; B не становится «в очереди» из-за запуска A.
Чтение чужой сессии через другой directory отклонено, browser storage пустое.
Оба теста прошли с первого прогона; исправления не потребовались.

Не повторялись Linux/Bun/typecheck upstream, полный suite и проверенный ранее
семипатчевый сценарий обращений: патч 8 меняет только текст ожидания;
новый backend-стык проверяется отдельно сценарием A/B.
Реальная платная модель и ChatGPT OAuth не проверялись.

Воспроизведение — [README native UI](../../deploy/opencode-ui/README.md):
Python 3.14.8, SOURCES/HELPERS из `check_combined.py` + `tests/panel` в PYTHONPATH,
`AI_PANEL_BROWSER_CHANNEL=msedge`, абсолютные `AW_NATIVE_UI_DIR` и `AW_PANEL_DIST`.
Первому тесту передан `AW_OPENCODE_IMAGE_ID` установленного образа, второму — нет
(синтетический harness). Тесты запускались последовательно и на разных временных
host, не с рабочей памятью оператора.

## Подготовка deployment, без активации

Собственный неактивный deployment-кандидат сведён с main обычным merge
`1322dcb`: **дерево точно совпадает с `9e7f268`**. Fast-forward был невозможен
из-за истории предыдущего preview; reset/force и чужие checkout не использовались.
Новая сборка помещена в отдельный artifact; предыдущие сборки сохранены.

Кандидат launcher обновлён, новый локальный receipt
`runtime/deployment/opencode-final-staging.json` хранит ревизии и хеши сборки,
панели, активного launcher, результаты preflight и оставшиеся gates.
Прежний семипатчевый receipt помечен заменённым. Cutover plan актуализирован,
но **не исполнен**. Эти машинные данные и сборки остаются вне Git.

Read-only preflight — exit 0: `LAUNCHER_OK`, `PYTHON_OK`, `ENV_FILE_OK`,
`PANEL_OK`, `RUNNER_OK`, `OPENCODE_UI_OK`. Host/provider/env совпадают с активным
launcher; `.env` не открывался. Хеш активного launcher не изменён.
Личный host, вторая личность на нём, backup/restore, serve и START не затрагивались.

## Дальше

Windows/Docker-результат относится к **main 9e7f268 и серии 1–8**.
Остаются OC-05 Claude, независимые GLM-review и отдельное окно backup/cutover.
Успешные smoke/preflight сами по себе не означают, что личный запуск уже обновлён.

# OC-07: Windows-проверка семи патчей и кандидат запуска

2026-10-24, Codex. Отдельная ветка `codex/oc-07-seven-patch`:
Claude `69225a7` + принятый main `87ae40f`, объединённая ревизия `8f8d2e4`.
Задача — подготовка запуска параллельно OC-02/05 Claude и проверкам GLM.
Личный host, активный launcher и его права не изменялись.

## Сборка и воспроизводимость

- OpenCode v1.19.3, точный upstream
  `e3c4eb242585dcd77be1a867391b7c25fd47ce52`; локальный Bun 1.3.14.
  Все семь патчей из `deploy/opencode-ui/patches/` проверены независимым Git
  index поверх тега: полученное дерево точно совпало с собранным исходником.
  Dependency lockfile не менялся; новых установок не потребовалось.
- `OPENCODE_CHANNEL=prod`, Vite `build --base /opencode/`;
  удалены только media/map/`_headers` из нового собственного build-каталога.
  `assets.prepare` и `assets.load` приняли manifest: **938 файлов,
  34 352 204 байта** без manifest; граница 1024 файла / 48 МиБ соблюдена.
  В итоговом JS нет прежних диагностических hooks
  `__awFetch`, `__awGateStates`, `AW_ENTRY`, `AW_SELECTION`, `AW_SUBMIT`.
- Панель пересобрана на той же объединённой ревизии: Node 24.19.0,
  `npm run build` (включая `tsc --noEmit`) — exit 0.
  Предупреждение о размере JS chunk осталось, сборку не блокирует.

## Проверки Windows

`tests/panel/test_native_opencode_sessions.py` — **1 passed, 68.39 с**.
Edge, Docker Desktop server 29.4.0, настоящий установленный OpenCode-runner;
провайдер HTTP синтетический, один вызов модели, платных вызовов нет.
Проверены: отправка в родном окне, reload в STOPPED, явный START,
первый допуск через панель временного host, ответ, отсутствие ACK в скрытой
вкладке/при обрезанном ответе, точный ACK видимого ответа, запись character,
reload без повторной оплаты, пустое browser storage и STOP.

`tests/panel/test_native_opencode_journey.py` — **1 passed, 75.69 с**:
Edge, синтетический harness и модель, настоящий временный host/owners.
Обращение из хода → точная карточка панели → ответ оператора → продолжение
того же разговора без новой реплики; два ответа character не затираются.
Проверены также отзыв сообщения после STOP, отсутствие его исполнения,
закрытые native API/PTY, отказ чужому directory, ограниченные сетевые пути
и пустое browser storage.

Первый прогон этого сценария упал на ожидании ACK: открытая вкладка панели
оставляла OpenCode в фоне. В тест добавлен явный `bring_to_front()` при возврате
из панели после START и ответа на обращение, с проверкой `visibilityState`.
Повторён только упавший сценарий; после правки он прошёл. Production-код ACK
и семь патчей не менялись. Ruff изменённого теста и `git diff --check` чистые.

Команды воспроизведения — [README поставки](../../deploy/opencode-ui/README.md).
Для первого сценария заданы `AI_PANEL_BROWSER_CHANNEL=msedge`, абсолютные
`AW_NATIVE_UI_DIR`, `AW_PANEL_DIST` и `AW_OPENCODE_IMAGE_ID` установленного образа;
для второго переменная образа снята: он использует синтетический harness.
Python 3.14.8; `PYTHONPATH` содержит SOURCES/HELPERS из `check_combined.py`
и `tests/panel`. Полный suite, Linux typecheck и платные пробы не повторялись.

## Неактивная подготовка запуска

Отдельный deployment-кандидат обновлён до `8f8d2e4`; новая сборка запечатана
в отдельном runtime artifact. Кандидат launcher ссылается на неё; прежний
четырёхпатчевый artifact сохранён. Новый локальный receipt
`runtime/deployment/opencode-seven-patch-staging.json` хранит SHA-256 manifest,
панели и активного launcher, ревизии, результаты и оставшиеся gates.
Предыдущий receipt помечен заменённым, локальный cutover plan актуализирован.
Эти машинные файлы/сборки в Git не входят.

Read-only preflight — **exit 0**: `LAUNCHER_OK`, `PYTHON_OK`, `ENV_FILE_OK`,
`PANEL_OK`, `RUNNER_OK`, `OPENCODE_UI_OK`. Проверено совпадение host/provider/env
с действующим launcher, без чтения `.env`. Хеш активного launcher не изменился.
Ни backup/restore, ни переключение личного запуска, ни serve/START личного host
не выполнялись. Вторая личность на нём не создавалась.

## Граница сдачи

Это проверенный Windows-кандидат, **не принятый в main клиентский пакет**.
Остаются OC-02/05 Claude, независимые проверки GLM, согласование итоговой ревизии
и отдельное окно backup/cutover. Реальная модель и OAuth ChatGPT не проверялись;
проба доказывает маршрут, доставку и UI, а не качество ответов модели.
Ветка с клиентским preview не вливается в main до завершения его приёмки.

Для Claude: backend `add-persona` уже в main `87ae40f`; Windows-проверка
его семипатчевого клиента завершена на `8f8d2e4`. При сведении забрать небольшой
test-only commit `5347278` (возврат фокуса в сценарии обращений), не дублировать
полные Windows-прогоны. Этот этап не закрывает его OC-02/05 или обзор GLM.

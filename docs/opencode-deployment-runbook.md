# Подготовка обновления OpenCode на Windows

Этот порядок относится к OC-04/07. Подготовленный backend и установленный
OpenCode в Docker не означают, что родной Web UI уже подключён.
Требуются клиентская поставка OC-03, общий сценарий OC-06 и узкий cross-review
изменённых границ по doc27 §8. Личный launcher переключается последним.

## Проверка без запуска

Из checkout с установленным locked-окружением:

```powershell
python scripts/check_opencode_deployment.py --config <absolute-launcher.json> --checkout <absolute-checkout>
```

Команда печатает только фиксированные коды, не открывает `.env`, БД или секреты,
не запускает host, контейнер, модель или агента. Docker только опрашивается.
Проверяется именно Python из launcher с `-I`: старый editable checkout нельзя
скрыть новым `PYTHONPATH`. Коды `PYTHON_VERSION_OR_CHECKOUT_MISMATCH` и
`PYTHON_UNAVAILABLE` означают, что указано чужое либо отсутствующее окружение.
Панель и родной dist проверяются теми же валидаторами, что при serve.

`OPENCODE_UI_NOT_CONFIGURED` ожидаем до поставки OC-03. Exit 78 не означает
поломку старой панели: полный новый вход ещё не готов. Exit 0 — только технический
preflight, не одобрение прав, cross-review, проверка API-ключа или гарантия качества
модели. Отчёт не проверяет login, живую модель и frontend SDK.

## Кандидат обновления

1. Создать отдельный чистый deployment checkout на принятом main. Не переносить
   `.venv` из другого checkout: editable-пути останутся привязаны к старому коду.
   Старый deployment и действующий launcher сохранить до окончания проверки.
2. Подготовить собственное окружение кандидата по `uv.lock`, uv **0.12.23**,
   Python **3.14**. Воспроизводимая команда — `uv sync --locked --offline`.
   Если кэш неполон, остановиться и назвать недостающую установку; не подменять
   версии и не запускать незапрошенную загрузку. Эти команды не выполняются
   самим preflight.
3. Получить принятую сборку панели PUI и **настоящего** OpenCode adapter OC-03,
   с commit/build provenance. Не использовать тестовую страницу вместо него.
   Файлы сборок держать вне каталога личного host. Пакетировать native dist:

   ```powershell
   python scripts/package_opencode_ui.py --build-dir <absolute-native-dist>
   ```

   Manifest не доказывает происхождение клиента: нужны поставка Claude и её
   acceptance. Не подставлять standalone `opencode web`: у него другое состояние.
4. В отдельном **неактивном** launcher JSON указать Python кандидата, принятую
   панель и `opencode_ui_dir` готового dist. Остальные ссылки на host/provider/env
   сохранить, секреты не копировать. Провести preflight на этом JSON.
5. Проверить временный вымышленный host: login → выбор профиля/сессии → сообщение
   → MCP/файл → ответ → точный ACK → STOP → restart STOPPED. Отдельно проверить
   отказ доступа и отзыв основания. Это дополнение OC-06 к backend-пробам,
   а не повтор полного suite.

## Переключение личного запуска

После готовности кандидата и cross-review согласовать короткое окно остановки:
STOP, дождаться известного исхода заданий, закрыть старый serve. Не запускать
два сервера с одной личной памятью.

Сделать штатную backup-копию с **существующим** recovery ledger и проверить её;
не копировать SQLite вручную и не создавать новый ledger взамен старого:

```powershell
python -m ai_agent_core.local_backup create --root <host> --store <configured-store> --ledger <configured-ledger>
python -m ai_agent_core.local_backup verify --root <host> --store <configured-store> --backup-id <returned-id>
```

Сохранить прежний launcher и переключить его на проверенный кандидат. Открывать
`/opencode/`, диагностику и обращения — `/`. После serve проверить STOPPED;
START остаётся отдельным действием оператора. Настройка нового UI не запускает
агента, не выдаёт Permission и не меняет контур модели/API.

Откат к прежнему launcher допустим только при совместимых данных и отсутствии
миграций/изменения формата. Иначе использовать штатный staged restore с preview,
решением и тем же актуальным ledger; не оживлять старую копию БД напрямую.

Текущие фактические пробы и ограничения:
[OC-04 runtime readiness](implementation-reports/OC-04-runtime-readiness.md).

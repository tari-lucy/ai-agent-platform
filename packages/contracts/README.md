# Контракты текстового MVP (M1-01)

Python namespace: `ai_agent_contracts`. Семантическая версия схемы `1.0`;
версия внешнего API отдельно `/api/v1`. Владельцы и данные — docs 17–19.
Контракты не импортируют core, domain, application, adapters, Redis или LLM SDK.

- `common`: строгие ID/UTC/версии/области, ссылки, зарегистрированные payload.
- `states`: независимые словари состояний doc17.
- `persona/memory/control/llm/workspace`: DTO логической модели doc18/19.
- `api`: discriminated panel/service команды, квитанции, планы, страницы,
  минимальные события, предварительные chunks и терминальные результаты.
- `codec`: полный JSON ↔ DTO, безопасные ошибки и чистые проверки
  версии/повтора/актуального результата.
- `catalogue/export`: явный каталог типов, JSON Schema 2020-12,
  OpenAPI 3.1 и TypeScript из одной серверной схемы.

Из корня своего checkout с настроенным `PYTHONPATH=packages/contracts/src`:

```text
python -m ai_agent_contracts.export --output packages/contracts/generated
python -m ai_agent_contracts.export --output packages/contracts/generated --check
python -m pytest tests/contracts
```

Все недоверенные входы проходят `from_wire(TypeAdapter(...), bytes, extensions=registry)`.
Прямой `model_construct` не является входным converter. `to_wire` повторно проверяет
вложенные изменяемые коллекции и размер ответа. Максимум JSON — 1 МиБ UTF-8,
текст — 16 000 символов, список — 200, вложенность — 32, extension — 64 КиБ.
Неизвестная версия/схема, extra поля, дубли ключей, NaN и частичный JSON отклоняются.
У первой версии нет молчаливого converter старых/будущих схем.

Расширение регистрирует доверенный владелец через `ExtensionRegistry.register`
и Pydantic-модель наследника `Contract` с закрытым набором полей. Регистрация
не загружает плагины из входа и не выдаёт права. Схемы полей характера,
аргументов и результатов добавляют их владельцы; произвольный JSON без
регистрации не принимается. Общий TS тип payload описывает JSON-конверт;
потребитель не получает доказательство регистрации/допуска из TS.

Ключ повтора — partition workspace/persona/memory space/work-test + request ID.
Fingerprint включает тип, полный scope, target, arguments и expected version.
Другой тип с тем же ID конфликтует; чужая область не открывает квитанцию.
`check_replay` проверяет идентичность, повторный допуск отдельно проверяет владелец.
`check_version` применяется внутри транзакции владельца, а не после отдельного
несогласованного чтения. `check_result_context` получает актуальные scope,
freshness, lease и UTC-время; не читает БД и не разрешает воздействие самостоятельно.
Поздний usage/сверка уже случившегося не проходят этим business-result predicate.

OpenAPI описывает чтение семейств записей, `POST /api/v1/commands`,
отдельный прямой STOP, recall и ограниченный внутренний job protocol.
Семейства команд имеют явные type/target/схемы. WebSocket описан через
`x-websocket`, поскольку стандартные HTTP paths не кодируют его сообщения.
`x-runtime-implemented=false`: генератор не открывает listener/эндпоинты.
Security schemes — требование будущих handlers M1-05, не готовая авторизация.
Path ID должен совпадать с body target; текущие права, план/сроки/версии,
источники и state переходы проверяются владельцем перед исполнением.
Наличие DTO moderation check не является действующим публичным допуском.

Готовые файлы `generated/*` не править вручную. TS описывает форму JSON;
пределы/UTC/decimal strings/права проверяются сервером. Test consumer проверяет
положительные и отрицательные типы без изменений UI client.

# M3-01..04: независимое review и интеграционные исправления

Дата: 2026-10-14. Координатор: Codex. База: 6755f43; отдельная ветка
`codex/m3-review-integration`. Исходные отчёты второго исполнителя сохранены.
Это дополнительная проверка модулей, а не приёмка работающего runtime.

## Результат

Ранее полный M3 координатором не проверялся. Исходные 117 тестов M3 прошли
на Windows, но сопровождались 15 предупреждениями незакрытых aiosqlite threads.
Семь независимых проверок воспроизвели реальные ошибки исходной реализации:
запись GM после STOP внутри classifier; принятие предложения при паузе линии;
невозможность завершить истёкшее предложение; применение изменения после STOP;
сохранение поздней рефлексии; секретоподобный текст в JSON отчёта; автономный
вызов extractor без явно подключённого допуска.

Исправления:

- Контекстный guard Storage проверяется перед owner write и завершением UoW;
  утрата допуска откатывает транзакцию этого владельца. Поздний технический
  учёт LLM другого владельца остаётся разрешён при его собственных grants.
  Вложенный guard не снимает внешний.
  Автономные extractor/consolidator/GM по умолчанию закрыты.
- `PinnedM3Control` связывает callbacks с конкретным существующим Admission
  Supervisor. Возобновление работы не выдаёт старому заданию новый допуск.
  Helper не создаёт jobs, grants или фоновых циклов.
- Ограниченное lifecycle-чтение только GM/Opportunity допускает собственный
  истёкший offer для терминального перехода. Права, доступность и источники
  по-прежнему проверяются; обычное чтение TTL не обходит.
- У StoryArc/Opportunity есть версия правил, у Opportunity — family/topic/reason
  и ограниченные типизированные броски seed/options/weights/result. Канонические
  экспорты обновлены. Текущие правила повторно проверяются перед accept/start.
  Пауза/закрытие линии закрывает её незавершённые предложения.
- Development атомарно пишет предложение и результат валидации, связывает
  idempotency с полным входом и проверяет версию состояния после rollback preview.
  Пересказы/связанные источники объединяются транзитивно в один опыт.
- Social требует доверенные aspect limits, подтверждённые независимые эпизоды,
  проверяет шаг и расход опыта; Relationship и StateChange(SOCIAL) пишутся
  атомарно. Расход переживает создание нового service instance. Неизвестные
  настройки закрывают применение. Лабораторные лимиты doc22 — данные профиля.
- Memory распространяет уже переданный composition ACL на Episode; Claim сохраняет отдельный private ACL.
  Данные для моделей читаются с `api_transfer`, включая существующие Claim,
  Profile/CharacterState/Episode/StoryArc; private-доступ не разрешает передачу.
  Права реальных областей автоматически не расширяются.
- Лаборатория удаляет секретоподобные log/finding тексты из возвращаемого отчёта,
  ограничивает seed, RAM-списки отчётов и очередь виртуальных callbacks.
  Перенос сверяет ревизию источника и версию целевого профиля непосредственно
  перед применением; значения простых настроек валидируются.
- Общий offline runner включает каталоги M2/M3 и независимые регрессии;
  M3/dialogue/moderation test engines закрываются до завершения короткого asyncio.run.

## Ответы на запросы координатору

| Запрос | Решение и остаток |
| --- | --- |
| M3-01: роли extractor/consolidator, Supervisor, post-session jobs | Безопасные callbacks и привязка Admission готовы. Role assignment, BudgetLease, scheduler/JobOwner и post-session dispatch в runtime **не подключены**. Нельзя заменять это постоянным admitted=True. |
| M3-01: corroboration/update Claim | Для M3 остаётся отдельное утверждение с явными sources/conflicts; автоматическое слияние не требуется критерием. Будущий merge — только у MEMORY с версией и правами каждого источника. |
| M3-02: lifecycle expired Opportunity | Закрыто узким owner port; проверено отсутствие общего TTL-bypass. |
| M3-02: contracts для family/topic/reason и dice | Закрыто additive canonical fields + StoryRoll; сохранение и чтение проверяются отдельно от возвращаемого результата. |
| M3-02: role, GM pause, layered rules/classifier/capability registry | Перед созданием runtime GM нужны доверенные настройки, RoleAssignment, classifier и реестр capabilities; HTTP/UI wiring отсутствует. Смысловое качество classifier не проверено. |
| M3-02: интенсивность и активные минуты | Интенсивность пока RAM; cooldown/window используют время владельца, а не накопленное активное время. Нужны сохраняемый SettingsHolder и активные часы; приёмка этого свойства остаётся открытой. |
| M3-03: extension registration, principal/grants/ACL/control/jobs | Registration API и необходимые grant/ACL условия проверены synthetic fixtures; исправлена потеря Episode ACL. Реальная композиция обязана регистрировать расширения до чтения, передавать scope/purpose и конкретный допуск задания. Post-session wiring остаётся открытым. |
| M3-03: StateChange owner=None в каталоге | Это полиморфная запись: UoW проверяет точное совпадение record.owner и owner принципала. Статический PERSONA сломал бы законную историю SOCIAL. Второй каталог/таблица не нужны. |
| M3-03: отдельное поле причины отказа | Не требуется: текущие checks и типизированный Outcome сохраняют результат проверки. Не меняем контракт ради дублирования. |
| M3-03: history/rollback UI | Методы проверены на уровне сервисов; отдельная работа M4-05, экран здесь не создаётся. |
| M3-04: relationship rate validator | Закрыто расширением M3-03; прежняя мягкая находка сценария заменена обязательным passing invariant. |
| M3-04: lab grants/mock/settings/jobs/UI | Изоляция и mock проверены; реальный SettingsHolder, job execution, auth endpoint и экран M4-06 ещё нужны. MemorySettings только для лаборатории/тестов. Многокомпонентный перенос настроек не является общей транзакцией; нужен CAS/receipt/reconciliation у composition. |
| M3-04: полный отчёт ArtifactRef | Открыто: LaboratoryReport хранит summary, полный JSON возвращается вызывающему; artifact adapter/lifecycle не подключены. Не считать сохранённым полный отчёт после restart. |
| Все M3: зона/статус | Зона review закреплена в doc24. integrated означает наличие модулей в main; runtime wiring и качество реальных моделей отдельно не приняты. |

## Проверки и воспроизведение

Подготовленный интерпретатор с dev dependencies из принятого lock, без чтения
.env, установки пакетов, загрузки моделей или реальных API:

```text
python scripts/check_combined.py
```

Runner задаёт PYTHONPATH только текущего checkout и использует importlib
для совпадающих имён исторических test modules.

Windows, CPython 3.14.8: полный runner **907 passed, 5 skipped**, 367.88 s.
Четыре пропуска — isolated Redis не задан; один — FIFO отсутствует в Windows.
Ruff, strict mypy (134 source files), import boundaries, canonical export
совпали. Остались шесть предупреждений deprecated WebSocket API сторонних
библиотек; aiosqlite thread warnings устранены.

После этого guard уточнён по owner (поздний технический расход нельзя терять).
Новый независимый port test: 4 passed, strict mypy passed; повтор affected
Windows suites и полный Linux runner приведены ниже.

## Остаточные ограничения и следующие работы

Реальные модели, качество RU extraction/сюжетных запретов/рефлексии, 20 сценариев
по 3 повтора с операторской оценкой не проверены. Нагрузка, полный worker/core
runtime, живой streaming и каналы не запускались.

До runtime-приёмки нужен отдельный composition пакет работ: подключить
принятые owners к одному registry/storage/JobOwner/scheduler, контекстный
допуск и отмену, pre-call budget/usage, роли, ACL с purpose и актуальными версиями;
сохранить настройки и активные часы, результаты/триггеры рефлексии; добавить
проверки восстановления без автозапуска. Полный отчёт должен получить artifact
storage и retention. Операторские apply/rollback/transfer должны вызываться
только после проверки auth/Origin/CSRF в транспортном слое.

Это конкретные незакрытые интеграционные требования; создаваемая здесь защита
не подменяет их вторым scheduler, владельцем БД или обходной архитектурой.

## Совместимость с параллельной веткой

Во время review origin/main продвинулся до 263430e (M5-01). Все четыре его
коммита включены merge-коммитом 0486b29 без конфликтов; retention включён
в общий runner. Это проверка совместимости, не независимый аудит M5-01.
Запросы M5 к общей миграции, lifecycle API/контрактам и backup сохраняются;
узкий lifecycle порт GM не выдаёт retention общий обход TTL.

## Поздние compatibility проверки и открытый Windows gate

После owner-scoped guard: Windows affected suites storage/M3/usage/retention
**240 passed** (125.51 s); после фикса частичного GM resolve — независимые
регрессии и весь gamemaster: **42 passed** (16.77 s).
Повтор GM после записи Episode и до завершения Opportunity обязан совпадать
с сохранёнными фактами; противоположный исход даёт IDEMPOTENCY_CONFLICT,
согласованный повтор завершает тот же эпизод. Новая регрессия сначала упала
на принятой реализации, после исправления прошла.

Первый Linux runner: 906 passed, 4 skipped, 5 failed. Все пять ошибок —
не смонтированные проверяющей командой deploy/compose.yaml, core.py и worker.py;
это ошибка harness, не приложения. Эти read-only mounts добавлены.

Во время проверки также поступил origin/main 2d0c811 (M5-02), включён
merge-коммитом 4672f45. Запросы автора M5-02 сохранены; независимый аудит backup
и retention не заявляется. В compatibility проверке native Windows M5-02
исправлены два подтверждённых случая: fsync требует writable handle (r+b);
close_live в synthetic composition должен закрывать recall index до замены
файла, а не только authority. На Windows теперь 36 backup tests passed,
**1 failed**: POSIX chmod/stat тест не проверяет Windows ACL.
Нативный Windows backup **не принят** до реализации/проверки ACL.
Тест не пропущен и не ослаблен. Окончательный полный Windows runner с M5-02 повторно не запускался:
отдельный backup suite уже воспроизводит этот блокер, и тест не скрыт
из общего runner. Это не ошибка M3.

Минимальная отдельная работа владельца M5: private-directory ACL adapter и
Windows ACL integration test для root/новых копий/manifest/replaced/staging,
либо явно ограничить backup deployment Linux-контейнером и проверить права
его реальных томов. Фактическая утечка данных этим synthetic тестом не доказана.

## Финальная проверка и вывод

- Текущий Linux runner с M3, M5-01 и M5-02: **948 passed, 4 skipped**,
  219.75 s. Пропущены только четыре реальных Redis теста: isolated host не задан.
  FIFO проверен в Linux. Шесть warnings — deprecated WebSocket API зависимостей.
- Ruff passed; strict mypy **144 source files**; статические import boundaries
  и canonical contracts export --check passed. Двадцать независимых M3-review
  regression tests входят в общий runner.
- Windows: общий набор до поступления M5-02 — 907 passed/5 skipped;
  affected storage/M3/usage/retention — 240 passed; финальные GM/review —
  42 passed. После поступления M5-02 native backup — 36 passed/1 failed
  (неподтверждённый Windows ACL, описан выше). Числа пересекаются, не суммируются.
- Linux проверен в уже имевшемся Python 3.14.8 образе
  ai-agent-platform:m0-02-check (SQLite 3.40.1), без сети, root filesystem
  read-only, user 10001, 2 CPU/1536 MiB; временные БД только в /tmp.
  Смонтированы текущие apps/core/src, apps/worker/src, packages, tests,
  scripts, migrations, config, pyproject.toml, uv.lock, .python-version
  и три перечисленных deploy файла read-only. .env не монтируется.
  Для транспортных тестов использован ранее проверенный universal websockets
  17.2 из подготовленной dev среды; пакеты и модели не устанавливались.
- Финальный diff просмотрен, credential/private-key patterns и личные пути
  в изменениях/отчёте не найдены; .env/runtime/private data в Git не добавлены.

M3-01..04 проверены на уровне модулей и mock integration, подтверждённые
ошибки исправлены. **Полная runtime-приёмка M3 не завершена**:
конкретные composition/active-clock/settings/artifact/real-model требования
перечислены в таблице. Автозапуска, платных API, игры/каналов/эфира не было.

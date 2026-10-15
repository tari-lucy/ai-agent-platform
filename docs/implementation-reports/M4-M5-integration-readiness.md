Актуализация координатора 2026-10-15: ручные PERSONA/GM controls, canonical
rollback plan и owner receipts подключены: [Integration-development-controls](Integration-development-controls.md).
Панель использует версии реальных ресурсов, читает GM settings/rules; применение,
коррекция и компенсирующий откат проверяются через настоящий owner HTTP/SQLite.
Host grants и live API остаются отдельной интеграцией. Ниже сохранена история
прежних UI-only запросов, а не перечень всех актуальных blockers.

Актуализация координатора 2026-10-15: laboratory events/per-job cancel
подключены к JobOwner и обычному TEST MEMORY owner:
[Integration-laboratory-control](Integration-laboratory-control.md).
Чтение запусков/Job CAS, bounded observations, factual cancellation и reload
проверяются отдельно от прежних UI-only допущений ниже. Host provisioning
и остальные M4/M5 действия ещё не закрыты этой приёмкой.

Актуализация координатора 2026-10-15: selected settings TEST→WORK подключены
через canonical preview/apply/status/history, явное подтверждение и partial
reconciliation: [Integration-settings-transfer](Integration-settings-transfer.md).
Профиль применяется владельцем PERSONA, состояние/биография не копируются;
GM intensity/rules — владельцем GM. План ограничен пятью минутами и текущими
правами/версиями. Host provisioning, laboratory events/per-job stop и остальные
M4/M5 формы этой приёмкой не закрыты. Старые записи ниже — история запросов.

Актуализация координатора 2026-10-15: download button, canonical catalogue/start,
report ACL и durable reservation подготовленного TEST pool подключены:
[Integration-laboratory-panel](Integration-laboratory-panel.md).
Ниже — предыдущая приёмка; новый host provisioning/events/per-job stop остаются отдельно.

Обновление координатора 2026-10-15: полный mock lab JSON доступен через
authenticated content route и shared PanelApi; [приёмка](Integration-lab-report.md).
Автовыделение TEST scopes, feature button и прочие owner requests остаются отдельно.

# M4 / M5-03 — сверка запросов и порядок интеграции

Обновление координатора: canonical public схемы/чтения и private mock HTTP путь —
[Integration-contracts](Integration-contracts.md), [Integration-dialogue](Integration-dialogue.md).
Ниже сохранён исходный аудит; остальные feature формы остаются UI-only.

Дата: 2026-10-14. Координатор: Codex. Сверка кода combined branch
M3-runtime-integration + origin/main d407263 (включая M5-04 prep).
Это аудит стыков и назначение следующей работы; экраны и M5 повторно целиком
не принимались. Сообщения второго агента проверены по коду и его task reports.

## Подтверждённая готовность

- M4-02..08 находятся в main; их отчёты прямо ограничивают приёмку UI/mock.
- M5-03 содержит lab/SQLite synthetic cycle и отдельно настоящий auth/STOP ASGI.
  Браузерный бизнес-путь использует mock-проекции. Это разные доказательства;
  их нельзя складывать в утверждение о полном настоящем HTTP цикле.
- В M1 уже есть dispatch agent.start/pause/resume/session.end, login/logout,
  STOP, scoped generic reads и WS. Закрытый default bootstrap не означает,
  что маршруты управления отсутствуют: без подготовленной readiness start закрыт.
- Новая M3 composition соединяет background jobs, GM/reflection/usage и mock lab.
  Она явно подключается через create_m3_app; deploy/core.py по-прежнему выбирает
  closed scaffold. Это ещё не сборка всех M2/M4/M5 владельцев.

## Найденные конкретные несовместимости

| Стык | Что есть сейчас | Минимальная следующая правка |
| --- | --- | --- |
| M4-07 session.end | workspace/model.ts направляет команду в session_id; Supervisor требует agent_id для ControlCommand | Использовать canonical target agent_id; один настоящий ASGI UI-contract тест |
| M4-03 Profile | UI отправляет schema persona.validate/apply/create с canon/style; PersonaService уже имеет persona.*-args с Markdown и revision digest | Согласовать единый payload и публичный validation report; адаптировать форму к существующему Markdown owner пути, сохраняя остальные свойства/границы |
| M4-05 GM | UI schema gm.configure, intensity low/normal/high, expected_version RunSession; M3 использует m3.gm-configure, число 0..1 и GmRuntimeState.version | Зафиксировать публичную schema, отображение уровня и собственную версию настроек; читать реальные settings/rules до CAS |
| M4-06 lab.start | UI ожидает laboratory.start, scenario_version/real_api и версию последнего report; M3 TaskInput закрепляет mock job, fresh lab_scope, trigger_id и версию Supervisor | Отделить создание тестовой области, запуск job и версию scenario. Убрать предположение report.version == версия управления |
| Каталог сценариев | UI дублирует статический CATALOGUE; backend catalogue() уже имеет ID/title/version/tags | Один authenticated read DTO/route + клиент из генерируемого контракта |
| GM rules/settings read | GmRuntimeState сохранён, но READ_MODELS его не публикует; rules payload живёт в core registry | Публичный projection DTO, owner ACL, числовая intensity, rules_version и layered rules |
| Полный lab report | Есть ArtifactRef, checksum/TTL и Reports.read; generic panel API отдаёт только metadata и api_transfer-фильтр | Отдельный ограниченный artifact read/download, проверка session/scope/TTL/source rights, без произвольного pathname |
| Settings transfer | SettingsTransfer.preview и durable progress готовы; UI пока только описывает ограничения | Читаемый preview с diff/blockers/digest + canonical execute; partial reconciliation показать отдельно |
| M4-04 планы удаления/экспорта | UI имена payload — предположения; RetentionService существует отдельно | Canonical preview/execute + ConsequencePlan dependencies/CAS и фактическая операция очистки/экспорта |
| M4-05 decisions/rollback | DevelopmentController имеет owner методы; HTTP mutation dispatch пока только узкий M3 набор | Typed schemas → owner handlers; общий CAS, digest rollback, причины и история |
| M4-08 usage | Generic reads и durable LLMAttempt есть; тарифы/пересчёт/агрегаты не подключены | Вызовы UsageLedger и серверные projections; unknown не заменять нулём |
| session_id usage | У LLMAttempt есть Scope.session_id; новая M3 запись сохраняет полный run scope | Сначала использовать существующее поле; исторические null показывать unknown, не добавлять дублирующую колонку без причины |
| M5-03 настоящий цикл | dialogue.message, private stream и доставку backend всё ещё не обслуживает | Подключить M2 owner composition и заменить mock-перехваты в одном вертикальном E2E сценарии |
| CI панели | check_combined включает backend и tests/e2e, но не tests/panel | Отдельный Node/Playwright gate при UI изменениях и перед интеграцией UI-contract; не запускать весь browser suite на каждой правке Python |

## Что не требуется писать заново

- Supervisor/control endpoints, JobOwner, scheduler, SQLite owner UoW.
- PersonaService, MemoryService, SocialService, DevelopmentController, GameMaster.
- UsageLedger, RetentionService, BackupService и сценарии catalogue().
- StateChange.owner=None в physical catalogue намеренен: настоящий owner
  проверяется UoW; Persona и Social сохраняют свои истории. Фиксированный
  общий owner сломает уже проверенную историю отношений.
- GM StoryRoll/family/topic/reason и relationship rate limits закрыты M3 review.
- begin_attempt и durable running-before-provider закрыты M3 runtime.
- Не ослаблять общий storage visibility после удаления одного из оснований.
  Возможность сохранить содержание решает owner по оставшимся доказательствам;
  источник с удалённым текстом не должен ожить из производной записи.

## Дополнение M5-04 prep

Новый evaluation harness/20 сценариев/budget guard присутствует в d407263.
Это подготовка/dry-run, без принятого качества реальной модели и без разрешения
на платные вызовы. Новый LabRunner.providers/resolve не должен автоматически
попасть в M3 mock entry: добавлен явный отказ до IO и regression.
После merge: 102 affected tests passed (lab/evaluation/e2e/M3 HTTP),
mypy167/ruff/imports/exports passed. Дополнительно исправлены Windows resource
import и no-network fixture (event-loop socketpair); Windows mypy167 и один
evaluation no-network smoke passed. Калибровка/живой provider остаются отдельными.

## Предлагаемые следующие небольшие задачи

1. **Integration-contracts** — один владелец contracts/API/client:
   матрица command type/schema/target/CAS/result/read DTO. Экспорт JSON/TS/OpenAPI
   из единого источника. Приёмка: настоящие server/client contract проверки для
   управления, persona, GM и lab; без новых экранов и реальных моделей.
2. **Integration-dialogue** — один минимальный рабочий путь:
   явная подготовка trusted scope/grants/profile/roles → ASGI login/start →
   dialogue.message → private streamed/final response → Source/Episode/usage →
   bounded post-session reflection → read projections → STOP. Только mock и
   приватный выход; публичный publisher остаётся закрыт.
3. **Integration-owner-actions** — сначала изменения профиля/GM/lab и чтения
   rules/catalogue; затем memory.delete через настоящий plan/RetentionService,
   decisions/rollback и полный report. UI-only снимается для доказанных действий
   по отдельности. Server draft/merge людей не подменять упрощённой записью.
4. **Integration-operations** — M5 storage migrations/aggregates, расписание
   retention и report purger; BackupService/restore ledger/jobs reconciliation;
   usage dashboard/limits. Windows backup ACL и crash/restore остаются gate.
5. Перевести M5-03 на настоящие маршруты и подготовленный entrypoint, затем
   отдельно обсуждать M5-04 с реальными моделями/бюджетом.

Пункты 1–2 — рекомендуемый ближайший связный этап. Остальные не объявлены
зависимостями, которые уже закрыты. Настройку прав выполнять явным минимальным
набором; автоматически открывать оператору всю private/work память нельзя.

## Разделение работы и экономные проверки

- Первый агент: public contracts, core bootstrap/mutation/read ports, storage
  migrations и права. Эти shared файлы имеют одного владельца.
- Второй агент после принятого contract commit: адаптация M4 feature modules
  и browser journey. Не менять общие schemas/client одновременно с первым.
- До contract commit UI сохраняет UI-only и честный отказ.
- На правках — affected unit/contract сценарии; один backend combined перед
  merge; targeted браузерный путь при изменении стыка. Полный browser suite
  перед приёмкой UI batch. Платные API и скачивания моделей сюда не включены.

# Integration-host-controls — явные права, TEST настройки и настоящий owner путь

Дата: 2026-10-15. Координатор Codex. Base main 8f79796,
codex/integration-host-controls, отдельный checkout. Назначение в doc24:
по поручению оператора закрыть host grants для ручных решений и settings transfer.

## Результат

Init --operator-controls готовит минимальные дополнительные grants и отдельную
TEST settings partition: Profile/новый CharacterState через PersonaService,
GM intensity/rules. Переносятся только данные исходного профиля, память/отношения
и накопленное состояние не копируются. Пул lab TEST остаётся пустым; общий лимит
восемь scopes соблюдается (при opt-in максимум шесть lab scopes). Неверные IDs/
port/scope layout проверяются до создания каталога. Старый config без новых полей
читается как прежний узкий режим. Serve никогда не провижинит grants.

WORK получает WORKSPACE write для планов/proof/OwnerOperation/transfer journal,
operator WORKSPACE read+api_transfer и PERSONA/GM MEMORY read. TEST settings
содержит только metadata owner writes PERSONA/GM/WORKSPACE, без job/model/channel
capabilities; оператор только читает. Permission/public disclosure/модерация через
HTTP не расширяются. Ограничение текущего storage: grants owner-level, поэтому
trusted WORKSPACE owner с write технически может писать Permission. Это явно
включаемый режим, а не утверждение о per-kind sandbox. POST permission.edit
остался закрытым и проверен. Реальные существующие runtime данные не менялись.

Runtime отличает ручные данные подготовленной TEST partition от execution run.
Для неактивной области требуются текущая operator authority над host, явно
разрешённый scope в authority/runtime, тот же workspace и отсутствие agent/run/
session. Mutations разрешает только перечисленные manual data commands;
лаборатория/диалог не запускаются от имени TEST settings/pool. Прежний Supervisor
execution/STOP boundary не менялся. GM pause TEST не управляет рабочим run;
применение TEST profile и GM rules не fault-ит другой активный рабочий run.

Development controls используют scope/version ресурса без фиктивной сессии;
интенсивность TEST редактируется при STOPPED. Редактор существующего профиля
переведён с прежних UI-only canon/style payloads на canonical Markdown validation
и apply по digest. Accepted definitions/initial values/bounds сохраняются;
canonical wrappers корректно показывают начальные/живые значения. Различие
значений само по себе не объявляется доказательством пережитого опыта.

## Проверки

- Windows, production subprocess init/serve, без fixture grant widening:
  явный режим/лимит/backward config; actual owner evidence → решение → rollback;
  TEST GM/profile edit → preview и три выбранных ключа TEST→WORK → replay →
  restart STOPPED. Permission неизменны; память не копируется. Отдельная проба:
  изменение config flag не создаёт grants (readonly snapshot до/после serve).
- Native affected batch: 38 passed, 46.43 s. Три новых host cases, настоящий
  Edge TEST editor/intensity/selected transfer, затронутые development/persona
  intercepted cases. После whitelist неактивных commands дополнительно один
  production HTTP case: 1 passed, 13.45 s.
- Node client: 159 passed, 0 skipped; TypeScript/ESLint/build прошли. Размер
  UI bundle >500 KB — прежнее предупреждение. Ruff/mypy186/import boundaries и
  canonical exports --check прошли; новых зависимостей/миграций/Compose нет.
- Linux affected gate: tests/local_host, tests/m3_runtime/{test_development_controls,
  test_transfer_http,test_http_laboratory}, tests/integration_dialogue/test_http,
  tests/control — 98 passed, 2 Windows ACL skips, 272.54 s, exit 0. Изолированный
  cached offline container, 2 CPU/1536 MiB, readonly sources; собственный временный
  venv со source/cache .pth для настоящих subprocess -I (как в host review).
- После уточнения whitelist неактивных mutation types перепроверен только новый
  затронутый Linux test_controls: 3 passed, 38.46 s, exit 0. В финальном case
  валидный forged TEST run/agent/session отклонён; laboratory.create из пустого
  TEST pool и TEST dialogue не имеют эффектов. Source initial openness=0.2
  переносится в профиль, при этом WORK current value=0.5 сохраняется.
- После уточнения подписи различия начальных/живых значений повторён только
  затронутый browser case: 1 passed, 2.66 s. Production build после подписи прошёл.
  Положительные проверки и реальные HTTP/mock/intercepted свидетельства разделены;
  полный backend/browser suite не повторялся после каждого патча.
- Diff --check и privacy scan перед commit; runtime/secrets/venv/build artifacts
  остаются вне Git.

## Ограничения

Mock/вымышленные данные, никаких live/paid API, .env значений или пользовательских
профилей в Git. Пользователь ещё подготовит описание персонажа и локальную
API конфигурацию. Full MVP/live/usage setup и Compose не закрываются этим этапом.
Узкий старый runtime не обновляется автоматически: opt-in относится к новому
каталогу; отдельная административная миграция существующих прав не добавлена.
Создание других persona scopes из старой UI формы остаётся отдельным стыком;
эта приёмка относится к редактору уже принятого профиля. Ручной rules editor и
permission renewal также не добавлены. WORK run остаётся один.

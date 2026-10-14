# Integration-contracts — канонические стыки панели

Дата: 2026-10-14. База: 7797236. Владелец shared schemas/API/client: Codex.
Приёмка ограничена перечисленными командами и чтениями, не всеми экранами M4.

## Единый источник

Pydantic: packages/contracts/src/ai_agent_contracts/panel.py и api.py.
JSON Schema, OpenAPI, TypeScript — детерминированные generated exports.
PersonaService импортирует те же классы аргументов; копий wire schema нет.
Core-only m3.gm-configure снята; новый публичный ID gm.configure-args.
m3.task остаётся внутренней схемой фоновых задач, не public lab.start payload.

| Команда | schema/data | target | CAS | Результат |
| --- | --- | --- | --- | --- |
| agent.start/pause/resume/session.end | ControlArguments | agent_id, всегда | RunSession.version | Receipt |
| dialogue.message | MessageArguments, text до 4000 | session_id | действующий run scope/допуск | accepted operation_id; не факт доставки |
| dialogue.acknowledge | AcknowledgeArguments, revision | сохранённый Utterance.id | Utterance.version + revision | Delivery receipt; Episode доступен отдельным чтением |
| session.reflect | ReflectArguments, до 20 episode_ids | session_id | RunSession.version, профиль/правила/источники дополнительно закреплены job | accepted operation_id |
| persona.create/copy | persona.create-args / persona.copy-args | scope.persona_id | новый профиль; source_version для copy | owner Receipt |
| persona.validate | persona.validate-args, полный markdown | scope.persona_id | Profile.version | ProfileValidation по отдельному POST validate |
| persona.apply | persona.apply-args, markdown + revision_digest | scope.persona_id | Profile.version + digest точной редакции | owner Receipt; текущий запуск отзывается |
| gm.configure | gm.configure-args | gm-runtime | GmRuntimeState.version | owner Receipt |
| laboratory.start | laboratory.start-args | laboratory | RunSession.version; отдельно scenario_version | accepted operation_id |

GM intensity — число 0..1; рекомендуемое отображение low=0.25,
normal=0.5, high=0.8. Запрос всегда несёт число; промежуточные значения
сохраняются без округления. gm_paused читается из RunSession отдельно.
Lab payload включает заранее подготовленный свежий TEST lab_scope, seed,
scenario_id/version, mode=mock. Свежая область не выделяется из UI произвольно:
её проверяют session authority, runtime scopes и trusted allocator.
Версия последнего отчёта не является CAS запуска.

## Чтение

- GET /api/v1/gm-settings — scoped page GmRuntimeState, включая rules и rules_version.
- GET /api/v1/laboratory-catalogue — LaboratoryCatalogue из backend catalogue(),
  ID/version/title/tags; только mock в этом entrypoint.
- GET /api/v1/persona-markdown/{persona_id} — ProfileMarkdown с текущей версией.
  Это полный документ с определениями/initial values/bounds, не только canon/style.
- POST /api/v1/personas/{persona_id}/validate — EditCommand persona.validate;
  ProfileValidation содержит безопасные issues/blockers/diff/digest/ready.
  Digest не прячется в resource_id.
- Существующие utterances/deliveries/sources/episodes/llm-attempts/operations
  читаются с текущими ACL, TTL и api_transfer, до пагинации.

Существующий HTTP/session/CSRF/Origin boundary применяется ко всем маршрутам.
Настройки текущей личности допускают partition-only scope; конкретный foreign
agent/run не подменяется. Ручной dialogue требует полного текущего run scope.

## Клиент и граница второго агента

apps/panel/src/shared/owner-commands.ts — готовые typed builders;
PanelApi имеет profileMarkdown/validateProfile/gmSettings/laboratoryCatalogue.
session.end исправлен на agent_id. Приватный dialogue/receipt подключён отдельно.
Старые feature forms persona/development/laboratory пока используют свои
UI-only заготовки: второму агенту адаптировать их к этим public портам,
не создавать новые schemas или clients. Не переносить UI-only целиком в integrated.
Preview/execute memory deletion, rollback, полный lab report, settings transfer
и usage aggregates остаются отдельной owner integration.

Проверки и итог combined revision — Integration-dialogue.md.

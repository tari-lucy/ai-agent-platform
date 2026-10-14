# Integration-dialogue — private mock HTTP composition

Дата: 2026-10-14. База: 7797236. Исполнитель/координатор: Codex.
Отдельная ветка: codex/integration-contracts-dialogue.
Задача связана с Integration-contracts; публичная матрица — в соседнем отчёте.
Это техническая приёмка приватного пути на подготовленной синтетической БД,
не качество живой модели и не готовность всех экранов M4/M5.

## Что соединено

create_m3_app(..., dialogue_index=...) подключает существующие M2 owners
к одному Supervisor, JobOwner, ResourceScheduler и SQLiteStore.
Нет второй БД владельцев, альтернативного scheduler, дублирования schemas,
новых зависимостей, lock/Compose/migration изменений или автоматических grants.

Путь: login → явный start → dialogue.message → durable job →
DialogueController/ContextBuilder + profile/recall → mock LLM →
preliminary chunks → сохранённый private Utterance →
dialogue.acknowledge → Source сказанного + Episode + Delivery →
явный session.reflect → session.end/STOP → restart без автозапуска.

- Source входа сохраняется до постановки задачи. Raw text не копируется
  в ExecutionCommand: только ID/SourceRef, авторитетный TTL остаётся у Memory.
- HTTP не удерживается до модели. Повтор ID/того же содержимого возвращает
  известный job; другое содержание конфликтует. Повтор известного результата
  после STOP не исполняет модель; новая команда в STOPPED закрыта.
- Контекст и private read/stream ограничены правами оператора, текущими
  source versions/TTL/api_transfer. Перед коммитом — повторная проверка
  прав/профиля/rules/bindings в той же транзакции.
- Ручной WorkKind dialogue работает при observation, требует ACTIVE/OPEN,
  отзывается pause/STOP и не открывает автономные инструменты.
- Private final остаётся draft, audience/channel operator-panel.
  Публичного одобрения, publisher, игры и внешних каналов здесь нет.
- Только подтверждение панелью точной редакции создаёт доставленный Episode.
  Это подтверждение получения интерфейсом, не доказательство прочтения человеком.
  Повтор ACK не удваивает эпизод. Старый run не получает новый допуск.
- Рефлексия конечна, по явным episode_ids, перед session.end.
  «Не менять состояние» принят как полноценный результат. STOP не запускает LLM.
- Source и Permission revoke во время модели, пауза и STOP закрывают final.
  Технические attempts/usage при отмене сохраняются, неизвестное не становится нулём.
- Job/OperationProjection сохраняются атомарно одним владельцем.
  Поздний factual ACK сохраняет уже принятый result_id и историю попытки.
- Attempt operation ID связан с job, поэтому max_calls учитывает все его
  генерации и lease retries; session_id берётся из существующего Scope.

## Критерии и доказательства

| Критерий | Результат |
| --- | --- |
| Настоящие login/start/message/reads/receipt/reflection/end/STOP | Windows 6 новых ASGI/SQLite cases passed; использован lifespan executor |
| Idempotency, source text только у Memory, version/digest CAS | Проверены в тех же тестах |
| STOP, pause, Permission и source API revoke во время модели | Поздний Utterance/Episode не сохраняется; usage завершается |
| Restart/no autostart | Сохранённый Episode остаётся; модель повторно не вызывается |
| Private stream/chunks/final | Backend scope fan-out проверен; клиент проверяет scope/sequence/attempt/gaps и конечный saved result |
| Реальный браузерный маршрут | Edge 1 passed: настоящие HTTP/WS/SQLite, ввод → final → ACK → STOP → reload; без перехвата проекций |
| Persona/GM/catalogue public стыки | Настоящие ASGI validation/apply/digest/CAS/settings/catalogue и отрицательные lab payload/scope cases |
| Canonical lab.start | Существующая mock-lab проверка переведена на authenticated POST с новой схемой; отдельного повторного lab runtime нет |
| TS клиент | 146 pure client cases passed; lint/typecheck/build passed |
| Статика | Ruff, mypy170, import boundaries, canonical exports passed |

Один общий Linux gate, cached Python3.14/SQLite, CPU2/RAM1536MiB/network=none:
1040 passed / 2 failed / 4 Redis skipped, 355.82s.
Оба сбоя — одна причина в новой проекции: factual ACK после SUCCEEDED
терял result_id. Исправлено сохранением уже принятой ссылки; два Windows
regressions passed (1.60s). Полный suite повторно не запускался.
После исправления: Linux affected jobs/M3/Integration-dialogue — 66 passed /
4 Redis skipped, 85.39s; Ruff/mypy170/imports/exports на финальной версии passed.
Этот affected набор включает положительный canonical HTTP lab.start и тот же
реальный mock report, а не дополнительный повторный лабораторный runtime.

Windows backup ACL в этом этапе не принимался; прежний M5-02 blocker сохраняется.
Нативные Windows FIFO cases исключены платформой, Redis probe требует отдельного
изолированного broker. Внешняя сеть/платные API не использовались.
Edge дал 4 уже известных legacy websocket/uvicorn deprecation warnings,
Linux full — 6 таких предупреждений. Обновление transport не входило в задачу.
Vite сообщает bundle >500kB; сборка успешна, разделение бандла — отдельная UI работа.

## Что остаётся и кому продолжать

- deploy/core.py по умолчанию закрыт. Host должен явно подготовить отдельную
  БД/профиль/rules/role assignments/Capability/Permission/grants/ACL и индекс.
  Factory не копирует synthetic fixture grants в пользовательскую среду.
- Старые feature формы persona/development/laboratory сохраняют UI-only.
  Второй агент адаптирует их к shared/owner-commands.ts и PanelApi:
  полный Markdown + digest, GM own CAS, backend catalogue и заранее
  выделенный TEST scope. Не дублировать schemas/shared client.
- Автовыделение lab scopes и полный lab artifact download, settings-transfer
  preview/UI, decisions/rollback, memory delete/export plans и usage aggregates
  принадлежат следующим owner-action задачам.
- Автоматическое retention/report cleanup scheduling, backup/restore wiring,
  Windows ACL и backfill исторических jobs projections — operations integration.
- Этот minimal operator путь не доказывает реальную публичную доставку,
  viewer accounts/social диалог через внешний адаптер или качество LLM.
  Они имеют свои M2/M5/future gates.
- M5-04 live quality/budget калибровка не выполнялась. Модели/цены не выбирались,
  реальные профили, ключи и чужие runtime данные не читались.

Повторный широкий прогон нужен при новых shared изменениях/непокрытом риске;
после этого узкого исправления проверяется только затронутый jobs/M3/HTTP набор.

# Persona: профиль, импорт Markdown и снимки (M2-01)

Модуль **не подключён к runtime**: нет HTTP, supervisor wiring, jobs, LLM и UI.
Его вызывает доверенная композиция ядра с `Principal(core_owner, persona)`.

## Композиция

1. Один общий `ExtensionRegistry` для store и сервиса; перед открытием store
   вызвать `register_persona_extensions(registry)` один раз (плюс регистрации других владельцев).
2. Администратор выдаёт persona-владельцу `Grant(read, write)` на `Owner.PERSONA`
   в каждой области (`SQLiteStore.provision`). Читателям (оператор) нужна запись
   `RecordAccess`; передаётся как `reader_access`, применяется к каждой записи.
3. `PersonaService(store, principal, registry, clock=..., reader_access=...)`.

## Операции

| Команда | Метод | Результат |
| --- | --- | --- |
| `persona.create` | `create` | Profile v1 + CharacterState v1 из начальных значений, одна транзакция |
| `persona.copy` | `copy` | Новый независимый профиль в явной области из принятой версии источника |
| `persona.validate` | `validate` | `ValidationReport` (проблемы, diff, конфликт версии, блокеры состояния); ничего не пишет |
| `persona.apply` | `apply` | Следующая версия Profile + перепривязка state; требует `expected_version` и `revision_digest` |
| чтение | `get_profile(scope, version)`, `snapshots`, `get_state`, `export_markdown` | Точные сохранённые версии |

`persona.edit` не реализован: черновик остаётся у оператора в Markdown-файле
(см. отчёт M2-01, запрос координатору).

## Инварианты

- **Снимок** — неизменяемая ревизия `Profile` версии *n*; `accepted_revision_id = rev-<sha256>`
  от нормализованного содержимого (форматирование, CRLF, BOM, NFC не влияют).
- **Неверный импорт ничего не пишет**: разбор и проверки идут до первого `put`; отказ даёт
  `ProfileImportRejected` (INVALID_ARGUMENTS + список `Issue`, без эха содержимого).
  Квитанция при отказе не сохраняется, `request_id` можно использовать повторно.
- **Конфликты**: `expected_version` ≠ текущей → `VERSION_CONFLICT` с текущей версией; параллельные
  правки принимают ровно одну. Тот же `request_id` с другим телом → `IDEMPOTENCY_CONFLICT`.
- **Принимается проверенная редакция**: `revision_digest` из validate должен совпасть с
  содержимым, иначе отказ.
- **Канон и состояние раздельны**. Apply сохраняет накопленные значения, добавляет только новые
  свойства с их начальными значениями; удаление/сужение свойства с накопленным значением —
  `STATE_MIGRATION_REQUIRED`, ничего не меняется. Слой `relationships` описывает схему/умолчания
  отношений социальной памяти и в CharacterState не попадает.
- **Изоляция**: запись и чтение идут в одной области (workspace/persona/memory space/work|test).
  Copy переносит только принятый канон; память, состояние, отношения не читаются и не копируются.
- Содержимое — данные: в формате нет полей прав/секретов; неизвестные разделы отклоняются;
  строки, похожие на секреты, отклоняются.

## Формат Markdown `ai-agent-persona-profile/1`

См. docstring `markdown.py`. Первая строка `<!-- ai-agent-persona-profile/1 -->`, затем
разделы `## Канон`, `## Стиль` (текст), `## Свойства`, `## Начальные значения`,
`## Границы развития` (по одному блоку ```json с закрытой схемой). Лимит: 30 000 символов /
60 000 байт. Неизвестные разделы, повторы, текст вне блоков отклоняются.
Фикстура: `tests/persona/fixtures/reference_persona.md` (вымышленная «Искра», doc22).

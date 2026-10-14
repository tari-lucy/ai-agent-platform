# Memory owner: источники, эпизоды, утверждения и recall (M2-02)

Модуль **не подключён к runtime**: нет HTTP, jobs, LLM, диалога. Вызывается доверенной
композицией ядра с `Principal(core_owner, memory)`; `RecallService` принимает любого
`core_owner`/`operator` (не worker), права определяют grants и ACL записей.

## Границы данных

- **Авторитет** — транзакционное хранилище M1-02 (`Source`, `Episode`, `Claim`).
- **Индекс** — производный FTS5 (+ sqlite-vec) в *отдельном* SQLite-файле
  (`ai_agent_adapters.recall.SQLiteRecallIndex`); таблицы не входят в миграции и
  перестраиваются из авторитета. Основная БД индекса не знает.
- Индекс только предлагает кандидатов. Каждый фрагмент перечитывается через repository
  для запрошенной цели, сверяется по версии и по хешу байт источника.

## Композиция

```python
registry = ExtensionRegistry()  # общий для store и сервисов
store = SQLiteStore(db_path, extensions=registry)  # + upgrade/provision (админ)
index = SQLiteRecallIndex(index_path, dimension=embedder.dimension)  # None → только FTS
health = IndexHealth()
memory = MemoryService(
    store,
    memory_principal,
    registry,
    index=index,
    embedder=embedder,
    health=health,
    reader_access=(RecordAccess(reader_id),),
)
recall = RecallService(store, memory_principal, index, embedder=embedder, health=health)
```

`embedder` — порт `Embedder` (`signature`, `dimension`, `async embed`). Реальный
FastEmbed-адаптер в M2-02 **не входит** (нужны pinned model cache и проверка манифеста M0-03).

## Жизненный цикл (MemoryService)

| Операция | Правило |
| --- | --- |
| `register_source` | Все три права (`read`/`api_transfer`/`public_disclosure`) задаются явно. ID источника детерминирован: внешний (`source_type`, `adapter_id`, `external_id`) или `request_id`; повтор → `duplicate`, изменённый текст с тем же внешним ID → `VERSION_CONFLICT`. TTL = `occurred_at` + 30 дней (не от регистрации и не от пересказа); уже просроченный → `EXPIRED`; `protected` без срока. |
| `set_rights` / `expire_source` / `delete_source` | Новая версия `Source` по `expected_version`; expire/delete убирают текст и запись из индекса; удалённая identity не импортируется повторно (`ACCESS_DENIED`). |
| `record_episode` | Один подтверждённый `operation_id` — один эпизод (повтор = duplicate). Источники — группа «один исходный опыт». |
| `record_claim` | Конфликтующие позиции сохраняются обе; связи пишутся с обеих сторон в одной транзакции. |
| `reindex_scope` / `reconcile` / `purge_index` | Атомарная пересборка области из авторитета; сверка индекса; удаление просроченных записей индекса. |

Индекс обновляется **после** коммита; сбой индекса не откатывает авторитет и виден как
`IndexHealth.pending` → `index_state="lagging"`.

## Recall (RecallService)

scope-фильтр в самом индексе → FTS5 и/или векторы → RRF → **перечитывание источника с правами
цели** (`private_context`/`api_transfer`/`public_disclosure`) → проверка версии и хеша →
дедуп по группе опыта → лимиты (по умолчанию 8 фрагментов, 2000 токенов, оценка символов/3).

- **Дедуп по исходному опыту**: источники одного эпизода дают один фрагмент. Совпадение
  текста у разных внешних событий НЕ объединяет их.
- **FTS fallback**: без embedder, при ошибке/таймауте, при другой модели или неполных векторах
  результат лексический, `index_state="degraded"`, `complete=False`.
- **Состояния**: `ready` / `lagging` (устаревшие или ожидающие записи) / `degraded` /
  `unavailable` (индекс не отвечает; пустой список без этого флага не выдаётся).
- **Происхождение**: `attribution` = provenance, тип, автор, уверенность идентификации, дата.
  **Конфликты**: ID `Claim`, видимые читателю по тем же правилам доступа.
- `missing_source_ids` — ID кандидатов индекса, недоступных читателю (удалены/просрочены/
  права/ACL); причина не раскрывается.

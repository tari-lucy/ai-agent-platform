# Долговременный поиск и retention (этап 1 пула памяти)

Дата: 2026-10-19. Исполнитель: Claude Code. Ветка `codex/long-term-memory-retention`,
отдельный worktree от `origin/main` `638162c` (проверенная база из передачи). HEAD — коммит
этой сдачи (см. `git log`). В `main` не вливалось, рабочий host не обновлялся, платных
вызовов, ключей, `.env` и рабочей биографии не было.

## Результат

Старый допустимый claim или сводка, которые вышли за окна «40 последних claims /
5 последних сводок», теперь могут попасть в следующий запрос модели, если
относятся к вопросу. Десятки более новых нерелевантных записей им не мешают.
Контекст по-прежнему не больше 3 фрагментов и 650 токенов. Дополнительных вызовов
модели, embeddings и reranker нет.

После истечения сырого разговора (30 дней от `occurred_at`) старый claim/summary
**не** доступен: так устроена действующая модель данных, её не обходил (см. «TTL»).
Удаление, отзыв передачи в API и новая версия источника убирают старую запись из
следующего запроса.

## Путь данных и что найдено

`Source` (сырой текст, TTL 30 дней) → `Episode` → `Claim` / `Summary` (ссылки
`sources`, авторитетная таблица `source_links`) → выборка `m3_composition/long_term.py`
→ блок `<memory by=… at=…>` в последнем сообщении → retention: `sweep`/удаление через
ledger/`apply_ledger` после restore.

Доказанные дефекты:

1. **Потеря старых записей.** `include_memory` рассматривала только
   `recent(limit=40)` claims и `recent(limit=5)` сводок. `PrivateRecall`/FTS
   ищет по сырым **источникам**, а не по структурированным записям. Поэтому claim
   41-й по новизне в контекст не попадал никогда. Воспроизведено на production-host:
   до правки (`archived` отключён) запрос модели не содержит старого факта, после — содержит.
2. **Связи claim→source в индексе почти пусты на production-host** (найдено чтением
   кода, тестом не воспроизводилось). `Runtime._perform_body` создаёт `MemoryService`
   без `index`, поэтому `record_claim` извлечения не вызывает `put_claim`. Таблица
   `claim_sources` заполняется только `reindex_scope` (finish-restore/backup).
   Следствие: `RecallFragment.conflicts` у обычного recall пуст для извлечённых
   claims. Правку не делал: этот этап обходится без индекса связей (см. ниже), а
   изменение пишущего пути job — не нужная здесь проводка. Предложение — в конце.

Ограничения (не дефекты):

- Консервативный TTL: производное не живёт дольше сырого источника.
- Поиск лексический: стемминг по префиксу, а не морфология. Синоним без общего корня
  («обожаю» ↔ «любит») не найдётся, если его нет в последних окнах.
- Выбираются 24 самых релевантных по bm25 фрагмента источников, затем до 20 claims и
  5 сводок, которые на них ссылаются (самые новые среди старших за окном). Если
  слово встречается в сотнях источников, очень старая запись может не войти в эту
  выборку.

## Что изменено

Зона (перечислена до правки и не расширялась):

- `apps/core/src/ai_agent_core/m3_composition/long_term.py` — новая функция
  `archived()`: FTS индекса этого scope (`prefix=True`, TTL на уровне индекса)
  предлагает только **id источников**. Claims и сводки, которые их цитируют, читаются
  из репозитория `recent(cites=…, before=<конец окна>)` с `purpose="api_transfer"`.
  Затем — прежнее ранжирование `score()` (claim без совпадения со словами вопроса не
  показывается). Дальше прежняя проверка каждого фрагмента: права, версия, доставка
  ответа персонажа, `disputed`, `partial`, бюджет. «Последняя сводка без совпадения»
  берётся только из окна, не из архива. В журнал добавлен счётчик
  `archive=<число>|unavailable` без текста и id.
- `packages/adapters/src/ai_agent_adapters/storage/database.py` и порт
  `packages/application/src/ai_agent_application/storage.py` — необязательный
  `recent(cites=…)`: 1..200 id источников, не для `Source`. Остаются записи, у которых
  **текущая** ревизия ссылается на один из них в `source_links`. Это ограниченная
  выборка в существующем адаптере репозитория. Все прежние фильтры `visible()`
  (гранты, ACL, права/TTL/удаление источников) применяются в том же SQL до `LIMIT`.
  Для существующих вызовов поведение не меняется. Миграции нет.
- `packages/adapters/src/ai_agent_adapters/recall/index.py`, порт
  `memory/ports.py` — `search(..., prefix=False)`: `match_expression(prefix=True)`
  строит `"зонтик"*` (≥4 букв, примерно 60 % слова), без операторов из текста. Обычный
  recall использует прежний точный режим.

Почему не индекс `claim_sources`: он производный, на рабочем host почти пуст
(дефект 2) и не содержит сводок. `source_links` авторитетна, уже заполнена для
всех записей, удаляется вместе с ними и проверяется той же read policy.

Какие старые факты теперь доступны: claim или сводка рабочего scope, которые
(а) ссылаются на источник, чей текст лексически (с учётом окончаний) совпадает с
вопросом; (б) содержат слово вопроса; (в) все их источники доступны для
`api_transfer`, не истекли, не удалены и той же версии; (г) у ответа персонажа есть
подтверждённая доставка. TEST-scope, другая персона и закрытые источники
не попадают: проверено.

## TTL, удаление, версии

Виртуальное время, настоящий retention owner, независимый ledger
(`tests/memory_quality/test_long_term_lifecycle.py`).

| Запись | Хранится | Доступна в контексте/API | До какого срока | Основание |
| --- | --- | --- | --- | --- |
| Source (сырой разговор) | текст до `sweep`, затем stub без текста (`expired`) | до `occurred_at + 30 д` | `RetentionPolicy.conversation`, `MemoryService.DEFAULT_TTL` | storage скрывает сразу по `expires_at`; `sweep` стирает текст, пишет ledger, удаляет из FTS |
| Claim / Summary (Episode — по коду, тестом не проверялось) | запись с текстом остаётся после `sweep` (`durable_derivatives` = 2 в тесте) | пока доступны **все** источники | минимум TTL источников | `policy.visible()`: ссылка на истёкший источник делает запись невидимой для любой цели; исключение — только Person/Topic/Promise/Relationship и только `private` |
| FTS-индекс | удаляется при `sweep`/удалении | никогда после TTL | `expires_at` в индексе | `search` фильтрует `expires_at > now`; запись всё равно перепроверяется |
| Удалённый источник | stub `deleted`; claim/сводка с единственным основанием стираются (по коду `dependents`; тест проверяет только отсутствие в запросе и запись в ledger) | нет | сразу | план preview/execute → ledger → tombstone; при restore — `apply_ledger` |
| Отзыв `api_transfer` | всё хранится | нет, ни запись, ни производные | пока не вернут право | `visible()` по текущим правам источника |
| Новая версия источника | всё хранится | старые claims/сводки на v1 — нет | — | `long_term.grounded()`: `source.version != ref.version` |

Проверено тестом: до TTL старый claim и сводка в запросе; через 25 минут после TTL
(до `sweep`) их нет, репозиторий их не выдаёт. `sweep` помечает источник `expired`, но
не `unconsolidated`. Индекс его больше не предлагает, следующий запрос без него.
Удаление через HTTP-план (в STOPPED), отзыв передачи и смена версии — каждое
убирает свой факт из следующего запроса. Restore-проверку не добавлял: lifecycle не
менялся.

**Незакрыто: решение retention-политики, нужен владелец.** Значимое
воспоминание сейчас **не** может жить дольше сырого разговора. Правило storage прямо
это запрещает, и обходить его я не стал. Есть расхождение: README retention считает
claims/сводки «долговременным следом» (`unconsolidated` = «следа нет»). Фактически
след после 30 дней не выдаётся, но **текст производной записи физически хранится
без срока**. Минимальные варианты для владельца storage/retention:

1. Если след должен жить дольше: в `policy.visible()` разрешить Claim/Summary
   ссылку на **истёкший** (не удалённый, не отозванный) источник. Права брать из
   состояния источника на момент истечения. В `long_term.grounded()` принимать
   такую ссылку с пометкой `raw="expired"`. Нужны собственный срок Claim/Summary
   (поле или политика, сейчас его нет) и явное решение о согласии на передачу в API
   после истечения сырого текста. Самовольно продлевать согласие нельзя.
2. Если не должен: `sweep` стирает текст Claim/Summary, у которых все основания
   истекли, по аналогии с `erase_records` при удалении.

## Проверки

Linux, Python 3.14 из `../venv`. Venv указывает на чужой worktree, поэтому src этого
checkout передавался через `PYTHONPATH`. Ничего не устанавливалось.

- Новые: production-host `test_long_term_archive.py` — 1 passed, 115 с;
  `test_long_term_lifecycle.py` — 2 passed; storage `cites` и префиксный FTS — в
  `tests/storage`, `tests/memory`.
- Целевой набор (memory_quality, history/memory_cycles/memory_lifecycle/owner_correction,
  retention, local_host memory/live, panel memory search, storage, memory):
  155 passed, 1 failed, 1 error за 1139 с одним прогоном. Failed — известное
  падение базы (ниже). Error — браузерный `tests/panel/test_memory_search.py`: в
  новом checkout нет собранной панели (`local-host refused: PANEL_DIR_MISSING`), а
  сборка требует `npm ci`, которую не выполнял. Правка панель и её маршруты не
  затрагивает; этот тест не прогнан.
- Ruff format/check на изменённых файлах — чисто. Strict mypy по конфигу — 220 файлов,
  без ошибок.
- Известное падение не от этой правки: `tests/memory/test_recall.py::
  test_conflict_ids_follow_the_readers_access_to_the_claims` падает и на чистой базе
  `638162c` (проверено через временный stash). Не исследовал: это вне зоны. Может
  быть связано с ACL claims в recall, поэтому стоит посмотреть владельцу.
- `ruff format --check` показывает расхождение в `development_controls.py` на базе — не
  мой файл, не трогал.

Не проверялось: Windows (FTS5 prefix — стандартный SQLite, путей и ACL в правке нет;
Linux не доказывает поведение Windows); качество свободного ответа реальной модели;
большой объём (сотни тысяч записей) — оценка: подзапрос `EXISTS` идёт по префиксу
первичного ключа `source_links`.

## Запросы владельцам

- Retention/storage: решение из раздела «Незакрыто».
- Composition (по желанию): передавать `index=self.dialogue_index` в `MemoryService`
  в `Runtime._perform_body`. Тогда `claim_sources`/`conflicts` для recall
  заполняются при извлечении, а не только после restore. Проверка: после цикла
  памяти `index.conflicts_for` возвращает противоречащий claim.

## Команды координатору

```text
git fetch origin
git log --oneline origin/main..origin/codex/long-term-memory-retention
PYTHONPATH=<src checkout>;tests/integration_dialogue;tests/memory_quality;tests/extraction;
  tests/operations;tests/access;tests/m3_runtime;tests/development;tests/local_host;
  tests/memory;tests/retention;tests/storage;tests/usage
python -m pytest tests/memory_quality/test_long_term_archive.py
  tests/memory_quality/test_long_term_lifecycle.py tests/storage/test_storage.py tests/memory
```

Следующий этап из пула — 2 «Постоянный собеседник и обещания»
(`codex/private-social-continuity`) от свежего `origin/main` после принятия этой сдачи.

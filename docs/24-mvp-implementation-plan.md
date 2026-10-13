# Список задач и порядок реализации

Дата: 2026-10-13. Планирование завершено; реализация начинается по явному назначению задач.
Текущее состояние и файловые зоны назначенных задач приведены ниже.
Остальные задачи не разрешены одним фактом наличия этого каталога.
Как назначать/объединять задачи — [руководство](27-coding-agents-workflow.md).

Каждая строка — самостоятельный brief: ID, зависимости, результат/зона файлов,
приёмка. При назначении добавляются исполнитель, base commit, branch/worktree
и исключённые файлы. «Зависит от» означает объединённый и проверенный результат,
а не только наличие незавершённой ветки другого агента.

## Назначения и интеграция

Обновлено координатором 2026-10-13 по явному согласованию оператора.
Неперечисленные задачи сохраняют статус `planned`.

| ID | Статус | Исполнитель / ветка | База / результат |
| --- | --- | --- | --- |
| M0-01 | integrated | Codex / task/m0-01-environment-audit | 4872a17; отчёт принят оператором, fast-forward в main, combined diff/check пройдены |
| M0-02 | in_progress | Codex / task/m0-02-project-scaffold | Проверенный main после интеграции M0-01 и этой записи назначения |

M0-02 владеет: root pyproject.toml/uv.lock/.python-version, apps/core,
apps/worker, apps/panel (manifest/lock/build/tooling/bootstrap),
packages (только каркас, без contract schemas), scripts, tests/tooling,
LICENSE/THIRD_PARTY_NOTICES.md и своим implementation report.
Отдельный worktree; manifest/lock owner один — исполнитель M0-02.
Исключены: Compose/deploy, migrations, schema/API/business contracts,
реализация памяти/LLM/STOP/UI экранов. Пробы M0-03 и последующие задачи
не назначены. M0-02 проверяет установку/минимальную сборку, без моделей,
платных API и запуска полного runtime. Общие статусы обновляет координатор.

## M0. Воспроизводимая основа и технические пробы

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M0-01 | Разрешение начать реализацию | Инвентарь Docker/WSL, runtime, лицензий; безопасные локальные настройки | Проверены целевые версии/ресурс; нет секретов в отчёте; никаких игровых/публичных воздействий |
| M0-02 | M0-01 | Каркас каталогов, uv/npm manifests и lockfiles, базовый lint/types/test; packages/apps/scripts | Чистая установка из lockfile и минимальная сборка на целевой среде; единственный владелец manifest |
| M0-03 | M0-02 | Проба SQLite/FTS5/sqlite-vec/FastEmbed; tests/probes, отчёт версий | Load/restart/delete/rebuild проходят; MiniLM на RU; измерены RAM/latency, recorded image/model digests |
| M0-04 | M0-02 | Три Compose-сервиса, named volumes, health/ресурс; deploy | Перезапуск сохраняет данные; published ports только loopback core, Redis не открыт |
| M0-05 | M0-03 | Recall-корпус/разметка; tests/fixtures/recall | 60 запросов, 1k/10k chunks, provenance/scope/deletion; baseline соответствует doc20 либо оформлена замена |
| M0-06 | M0-04 | Safe startup/config validation и редактируемый пример env; config/apps | Нет dump secret; невалидный профиль объяснён; startup всегда остановлен |

M0-03 и M0-04 могут идти параллельно после M0-02 с разными файлами/временными
ресурсами. M0-05 не требует реальной LLM для измерения retrieval; генерационное
качество будет проверено позднее. Необязательный MemPalace/E5-пилот отдельный
task, если baseline прошёл; обязательный, если требуется замена.

## M1. Контракты, владельцы и устойчивость

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M1-01 | M0-06 | Contract schemas/converters/OpenAPI, command/result/errors/scope; packages/contracts | doc17–19 реализованы; version/idempotency/size проверены; type export стабилен |
| M1-02 | M1-01, M0-03 | DB repositories и линейная миграция logical model; migrations/storage | Транзакционные версии/уникальности/права; empty→current и previous→current; нет shared DB writes worker |
| M1-03 | M1-01, M1-02 | Supervisor/state owner, direct STOP, separate pause/resume; application/control | STOP при зависшем LLM/Redis/DB; stale epochs не коммитят воздействия; restart без autorun |
| M1-04 | M1-02, M0-04 | Job/outbox dispatcher, lease/retry/reconciliation; adapters/broker/application/jobs | Crash до/после commit/ACK, broker loss, reclaim, duplicate и bounded retries проходят |
| M1-05 | M1-01, M0-06 | Operator auth/session/CSRF/Origin и service identity; apps/core/access | Чужое чтение/команды/ws запрещены; STOP независим от DB; credentials не в API/logs |
| M1-06 | M1-03, M1-04 | Bounded telemetry/health/trace, resource scheduler | Очередь/буферы конечны; known/unknown результат различим; секреты/полные prompt не логируются |

После M1-02 возможно параллельное выполнение M1-03/M1-04/M1-05 при заранее
выданных interfaces. Supervisor и job owner не создаются в двух альтернативных
реализациях. Изменения общей схемы проходят владельца контрактов/миграций.

## M2. Личность, память и модельный диалог

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M2-01 | M1-02 | Persona/profile lifecycle, Markdown import/snapshots; persona adapter | Новый профиль не копирует память; invalid edits/version conflict; public reference profile заменяем |
| M2-02 | M1-02, M0-05 | Source/episode/archive lifecycle + recall adapter | Access перед контекстом, дедуп по исходному опыту, FTS fallback, происхождение/конфликты |
| M2-03 | M1-02 | People/encounters/relationships/topics/promises | Раздельные platform IDs, резервы вопросов, повторная встреча; точные обязательства без top-k |
| M2-04 | M1-03, M1-04, M1-06 | LLM provider adapter, roles, private streaming/mock; adapters/llm | Full/broken stream, cancellation, finite retry, malformed result, configurable endpoint/model |
| M2-05 | M2-04, M1-02 | Usage/attempt ledger, tariffs and safe projections | Duplicate receipt ≠ новая попытка; missing usage/rate ≠ 0; model/rate versions, cached/reasoning не удвоены |
| M2-06 | M2-01, M2-02, M2-03, M2-04, M1-03 | Dialogue/context/attention task controller | Multi-session память, обязательства/инициатива, partial JSON не выполняет tool, STOP closes output |
| M2-07 | M2-04, M1-03 | Independent moderation model/policies, exact-revision public draft gate | Недоступность/отказ блокируют simulated public output; исправленная редакция проверяется заново |

M2-01/02/03/04 идут независимо после M1 gate, если файловые зоны разделены.
Реальные модельные пробы требуют локального агрегатора и выбранных оператором
role models; до этого mock показывает только корректность протокола.

## M3. Опыт, развитие и лаборатория

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M3-01 | M2-06, M2-02 | Extraction/evidence proposals and consolidation | Чужое сообщение/наблюдение/вывод разделены, источник проверен, self-summary не даёт независимого доказательства |
| M3-02 | M2-06, M2-07 | Internal GM/arcs/opportunities/seed policy | Нет своего голоса/forced outcomes; cooldown/границы/отказы, опыт только по фактическому исходу |
| M3-03 | M3-01, M2-01, M2-03 | Reflection/development validator/history/rollback | Bounded changes и expected_version; identity требует оператора; pause/STOP, preview compensating rollback |
| M3-04 | M3-02, M3-03, M1-04 | Test runner/virtual clock/scenario report | Изоляция work/test, seed replay, сценарии doc22; перенос только выбранных настроек |

## M4. Семь областей панели

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M4-01 | M1-01, M1-05 | Panel shell/login/API client/ws/query cache; apps/panel/shared | Constant STOP, selected vs active persona, test label, origin/session, reconnect без повторного model call |
| M4-02 | M4-01, M2-06 | Dialogue/current task/inputs | Send/save/stream/final/error; версионные вводные, STOP во время генерации |
| M4-03 | M4-01, M2-01, M2-03 | Persona + people/encounters/topics | Form validation/diff/conflict, identities не смешиваются, state отдельно от канона |
| M4-04 | M4-01, M2-02, M5-01 | Memory/search/source/correct/delete/export | Provenance/TTL/deleted состояния, preview области, нет hidden raw archive exposure |
| M4-05 | M4-01, M3-03, M3-02 | Development four views/change card | Основания/manifestation/границы/откат, pause GM независимо |
| M4-06 | M4-01, M3-04 | Laboratory | Каталог/steps/clock/report, explicit real API, isolated biography |
| M4-07 | M4-01, M1-06, M5-02 | Workspace/operations/rights/backup restore | Partial failure/STOPPING/OUTCOME_UNKNOWN, explicit resume, recovery preview |
| M4-08 | M4-01, M2-05 | LLM dashboard | Roles/rates/tokens/currencies/attempts, missing data, historical repricing preview |

M4-01 можно начать после M1, пока backend M2/M3 готовится, со стабильными
fixtures. Экранная задача не меняет API по своей инициативе. До готового
backend приёмка отмечается как UI-only, задача не объявляется интегрированной.

## M5. Данные, восстановление и приёмка

| ID | Зависит от | Результат / зона | Критерий завершения |
| --- | --- | --- | --- |
| M5-01 | M2-02, M2-03, M2-05 | Retention/delete/tombstone/caches/metadata lifecycle | 30/14/90-day policy virtual time; удалённое не выдаётся до физической очистки; версии не хранят raw forever |
| M5-02 | M5-01, M1-04 | Backup/restore/migration recovery tools | 7 copies/pre-migration; consistent snapshot; restore с последующим deletion ledger; agent stopped |
| M5-03 | M1–M4, M5-02 | CI synthetic suite/browser scenarios | Вход→диалог→restart→повторная встреча→развитие→удаление→STOP; no secrets/external publication |
| M5-04 | M5-03 | Local real-model quality/usage/resource report | 20 сценариев × 3, doc22 rubric; recall и resource goals отдельно; budget/unknown usage reported |
| M5-05 | M5-04 | Operator acceptance + release notes/runbook | Нет открытых критичных scope/STOP/data failures; ограничения модели/железа названы; MVP text ready |

M5-01 начинается после M2, параллельно M3 и части M4. M5-03 зависит от всех
перечисленных задач M1–M4, не от названия этапа как условного события.

## Контрольные ворота

| Gate | Доказательство | До чего нельзя переходить |
| --- | --- | --- |
| G0 / M0 | Exact environment + successful probes + locks/digests | Нельзя объявлять стек воспроизводимым по документации upstream |
| G1 / M1 | Contracts/access/STOP/jobs fail scenarios | Нельзя давать реальные публичные/физические полномочия |
| G2 / M2–M3 | Source/social/multi-session/development isolation | Нельзя называть память/развитие устойчивыми по одному чату |
| G3 / M4–M5 | Full panel/recovery/quality/resource acceptance | Нельзя объявлять текстовый MVP готовым без интеграционного цикла |

Task report: изменённые файлы, принятые решения, команды/результаты проверок,
невыполненные проверки/причина, риск/следующий шаг. Правка плана допустима
при новых данных; крупная смена архитектуры получает запись D в doc07.
Само выполнение задачи не разрешает публикации, реальный эфир или новые права.

## Последующие эпики

Они запланированы, но не входят в приёмку первого текстового MVP.
Подробные границы/проверки — [интеграционная спецификация](26-future-integrations.md).

| ID | Зависит от | Результат | Проверка готовности |
| --- | --- | --- | --- |
| F1-01 | G3 | Audio capture/playback/router/watchdog Windows | Stable IDs, mute/STOP, no echo self-loop, VB-CABLE route |
| F1-02 | F1-01 | Local/API STT adapters | RU/noise/VAD, finite buffering, attribution/cancel, no raw audio retained by default |
| F1-03 | F1-01 | Local/API TTS adapters/voice profile | Voice rights, playback timing, pre-output moderation, all 4 STT/TTS combinations |
| F2-01 | G3, exact game versions | Fabric bridge observation/input/resource ownership | Local/private world first; client data provenance, disconnect/watchdog/manual takeover |
| F2-02 | F2-01 | Navigation/follow/exploration/stuck recovery | Obstacles/recovery cap/moving target, no per-tick LLM, not loop jumping |
| F2-03 | F2-02, F1-02, F1-03 | Minecraft chat/voice encounters | Stable server/world IDs, platform rules, topic dedup, voice/chat one utterance |
| F3-01 | F1-03, actual rig | VTS mapping/lip sync/emotion controller | Auth/reconnect, actual playback sync, defaults on cancel, expression arbitration |
| F4-01 | G3, channel facts | Twitch/YouTube inputs and social memory | Verified platform IDs, edits/deletes/dedup/rate limits, actor not answering every message |
| F4-02 | F1–F3, F4-01, public-release gate | OBS/stream coordinator/public safety path | Private rehearsal→explicit live; foreign audio/video policy, emergency scene/mute, two-platform route proven |
| F5-01 | F2-01, G3 | Screenshot/capture/catalog/post drafts | Interesting-place consent/rules, factual caption tied to source, privacy/media metadata |
| F5-02 | F5-01, public-release gate | Telegram manual release + comments | Exact draft/media moderation and owner approval; receipt/unknown result; same commenter memory |
| F6-01 | F5-01, recordings enabled | Media ingest/markers/EDL/project | Protected source references/timebases, no auto-edit biography, isolated render resources |
| F6-02 | F6-01 | Editing UI + FFmpeg export worker | Non-destructive trim/sequence/subtitles/audio levels/preview/export; cancel/error/quota; no public upload implied |
| F7-01 | G3, explicit need | Multiple active agents/resource arbitration/shared knowledge | Isolation, ownership leases/fairness; not confused with multiple developer agents |

Только координатор обновляет общий статус task list на integration branch.
Неперечисленные в таблице назначений задачи остаются `planned`.

# Integration-live-api — ограниченный приватный API pilot

Дата: 2026-10-15. База: main `7d4d5a7`, ветка `codex/integration-live-api`.
Область: coordinator composition, local host, затронутые проверки и инструкция.

## Поведение

Явный init с точным ID модели подготавливает Connection и LLMAssignment роли
character. Init не читает credentials и не вызывает сеть; существующий host не
перезаписывается. Serve принимает абсолютный --env-file, загружает его штатным
sanitised configuration loader без ambient env overrides, использует существующий
OpenAI-compatible streaming адаптер. Значения endpoint/ключа не записываются
в Profile, host.json, БД, отчёт, prompt или сообщения ошибок.

Сохраняются loopback-only listener, авторизация оператора, STOPPED после старта/
restart, права и версии источников, pinned assignments/connections/permissions,
независимый STOP, whole-document JSON validation, приватный draft output и
явное подтверждение доставки. Состав grants не расширялся. Старый mock host
не приобретает live-возможностей через serve.

Реальный binding ограничен подготовленными role/model/connection/setting refs,
output tokens, timeout и размером prompt. В composition real dialogue допускается
лишь при явном adapter allow-list и PrivateAPIBudget. Перед вызовом существующий
UsageLedger сохраняет резерв попытки. Лимит 10 по умолчанию (1..100 при init)
считает все прежние попытки области, включая running/unknown/mock, поэтому не
сбрасывается новым сообщением/сессией/рестартом. Проверка и резерв сериализованы
внутри единственного runtime. Второй процесс с той же БД не поддерживается.
Live gateway не выполняет автоматические retry/fallback.

Лабораторный mock fence сохранён. В live host лабораторный вход с real character
binding остаётся закрыт; остальные роли не подключены к live, не имитируют
полноценное развитие/GM. Публичная модерация и внешние каналы не включались.

## Совместимость

Пользователь выбрал claude-sonnet-5. Это параметр подготовки, не системный default.
Официальный контракт выбранного пользователем сервиса
[API Token](https://api.api-token.ru/docs/ai.md) указывает Bearer authorization,
Chat Completions и claude-sonnet-5 в публичном каталоге. Поддержка конкретной
операции зависит от модели, доступ предъявленному ключу определяется /models;
реальная совместимость streaming/usage не выводится только из этой страницы.
Endpoint берётся исключительно из явно указанного локального файла, не из
найденного в интернете адреса. Native Messages/Responses не добавлялись.

## Проверки

Статические ruff и mypy (186 source files) прошли. Новый synthetic HTTPX сценарий
проверяет весь host init → login → STOPPED → start → private JSON reply → ack →
usage ledger; дополнительно duplicate replay без повторного HTTP, AUTH/429,
невалидный JSON, truncation, размер prompt, STOP в потоке, отсутствие позднего
final и сохранение общего лимита после restart. Foreign roles/models/credential
refs и превышение generation parameters закрываются до HTTP. Реальная сеть в
этих проверках не используется, grants в фикстурах не расширяются.

Native Windows combined: **116 passed, 1 skipped** (POSIX-only mode), 105.08 s.
Набор: tests/local_host/test_live.py, test_smoke.py, test_guards.py,
tests/integration_dialogue/test_http.py и tests/llm. Это один затронутый набор,
без повторного полного backend/browser suite. После первой реальной пробы
добавлен диагностический лог только с фиксированным ProviderError enum и явным
закрытием underlying stream; после изменения повторены только новые проверки: **3 passed**, 33.87 s.
Повторные ruff/mypy чистые; полный набор повторно не запускался.

## Совместимость JSON-обёртки

Реальная модель настойчиво оборачивала JSON в Markdown, в том числе после явного
напоминания о wire-формате. Это было диагностировано по приватному локальному
синтетическому ответу, без публикации текста. На уже полученном ответе offline
проверено, что внутренний объект проходит действующую DialogueDecision-схему.

В private character wire adapter добавлено распознавание **одного полного**
Markdown JSON блока (json или без метки) исключительно после успешной проверки
внутреннего объекта существующим strict parser/Pydantic. Общий gateway parser,
контракты, права, role dispatch и M2-модули не менялись. Ошибочные поля, дубликаты
ключей, неполный/многоблочный ответ и произвольный внешний текст остаются invalid.
Недокументированный response_format не отправляется и не считается поддержанным.

Здесь текст буферизуется до конца upstream, максимум 200000 символов; streaming
transport и usage сохраняются, но progressive text preview этой веткой пока не
обещается. На ошибке/STOP полного принятого ответа нет, underlying iterator
закрывается. После финального изменения **4 live tests passed**, 30.85 s,
включая real-adapter synthetic HTTP/ack/errors/STOP/restart и строгие отрицательные
варианты обёрток; ruff и mypy (186 source files) чистые перед commit.
Отдельно уточнён negative binding check: 1 passed за 1.00 s. Вне этой узкой ветки
прежняя семантика gateway не расширялась.

## Реальная ограниченная проба

По поручению использовать API подготовлен отдельный новый локальный runtime
с опубликованным примером профиля и моделью claude-sonnet-5; существующие среды
не менялись. Первый запрос со старым ключом был отклонён; после обновления
пользователем ключа документированный /models вернул 200 и подтвердил модель.
Локальный base URL изначально совпадал с документацией выбранного сервиса.

Всего в отдельном стенде записано **7 реальных attempts**: одна ошибка со старым
ключом; четыре завершения provider без принятой реплики из-за JSON-обёртки
(первый smoke также слишком рано отправлял STOP); один truncated output при 512
токенах; одна успешная сквозная генерация после исправления. Эти попытки не
скрывались и не сбрасывались, общий предел остаётся 10, на стенде осталось 3.
Увеличение output до 1024 сделано только для выбранного pilot через существующий
Owner.LLM/CAS и nonsecret host config, в пределах уже действующего Permission
max_output_tokens=1024; grants, права и лимит calls не расширены.

**Финальная реальная приёмка:** login → STOPPED → явный start → модель
claude-sonnet-5 → сохранённая Utterance (144 символа, private draft) → явный ack →
STOP → STOPPED. Provider сообщил complete usage: input 5408, output 578;
стоимость unknown, тарифа нет. Панель отдаётся на том же loopback origin,
сервер оставлен работающим с агентом STOPPED. Качество персонажа не оценивается
по одному техническому вопросу. Для воспроизведения этого Sonnet pilot при init
передайте --live-max-output-tokens 1024. Браузерный сценарий с реальной моделью
отдельно не прогонялся; это реальный HTTP, не UI interception.

 Отдельные
GET каталога не являются генерациями. Endpoint/key, реальные request IDs,
тексты synthetic диалогов и raw replies не включены в отчёт или Git. В первом
smoke-клиенте также исправлено раннее завершение ожидания: окончание attempt
предшествует записи Utterance; STOP теперь отправляется после ожидания записи.

## Ограничения и следующие стыки

Это private pilot, не приёмка всего MVP. Нет dollar budget enforcement:
ограничены calls/output/context/timeout, а тариф не выдуман. Usage и стоимость
при отсутствии данных остаются unknown. Внешний лимит ключа задаёт владелец
агрегатора. STOP не отменяет возможное начисление удалённого провайдера.

Linux-переносимость новых изменений и browser/live качество не объявлены
проверенными. Не подключены live reflection/extract/consolidate/GM/story-screen,
лаборатория, Compose deployment и независимый публичный модератор. Источники,
профиль и контекст могут передаваться выбранному API только с текущими правами;
последующие реальные разговоры не включаются в Git.


Запрос следующему владельцу usage: для фактической цены API Token требуется
связать provider request_id с /v1/key/info recent_requests[].cost_rub. Текущий
общий ledger не хранит этот provider ID, тариф и фактическую RUB charge этой
пробе не назначали. Отсутствующие цены не заменяются нулём или прайсом со страницы.

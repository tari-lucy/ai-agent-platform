# Soniox: транспорт синтеза и потокового распознавания (doc34)

Дата: 2026-10-29. Ветка `codex/voice-soniox`, база `origin/main` `0a4bd58`.
Статус: реализация адаптера и проверка на поддельном сервере; не подключён к
платформе, MCP, модератору или аудиоустройствам. В main не вливался.

## Проверка документации до реализации

Проверены официальные страницы Soniox, без ключа и без вызова API провайдера.

- **Русский синтез есть**, код `ru`. По документации голоса многоязычные:
  выбор голоса не привязан к единственному языку.
  [Supported languages](https://soniox.com/docs/tts/concepts/supported-languages),
  [Voices](https://soniox.com/docs/tts/concepts/voices).
- В документированном примере каталога женские голоса: Maya, Nina, Emma,
  Claire, Grace, Mina, Lucia, Sofia, Isla, Victoria, Ruby, Elise, Priya, Meera.
  Примеры мужских: Daniel, Noah, Adrian. Это **пример документации, не ответ
  текущего аккаунта**. Актуальный каталог: `GET /v1/tts-models` или shared voices
  выбранной модели; голос выбирают оператор и персонаж, в коде он не закреплён.
  [Get TTS models](https://soniox.com/docs/api-reference/tts/get_tts_models).
- Текущая модель TTS на странице моделей — `tts-rt-v2`; `tts-rt-v1` устарела.
  Старый идентификатор в примере каталога не использован как default.
  [Models](https://soniox.com/docs/tts/models).
- **Цена токенная, не фиксированная поминутная.** TTS: входной текст
  $4 / 1 млн токенов, выходное аудио $21,50 / 1 млн; ориентир около $0,70/час.
  Realtime STT: входное аудио $2 / 1 млн, контекст и выходной текст по
  $4 / 1 млн; ориентир около $0,12/час. Async STT около $0,10/час, но этот
  адаптер его не вызывает. Это справочная цена на дату проверки, не курс RUB
  и не расчёт списания. [Pricing](https://soniox.com/pricing).
- TTS принимает `opus` и `pcm_s16le`; частота и bitrate задаются настройками.
  Отдельного формата `ogg` в списке TTS нет. Для `synthesize_ogg` запрашивается
  `opus`, а результат проверяется на Ogg/Opus headers; сырой Opus вместо Ogg
  не выдаётся за подходящий файл. Реальный контейнер провайдера этим offline
  тестом **не доказан**; если окажется иным, потребуется отдельное согласованное
  преобразование. [Audio formats](https://soniox.com/docs/tts/concepts/audio-formats).
- WS TTS завершён только после `audio_end` **и** `terminated`. STT завершён
  после `finished`; EOF для него — пустой текстовый кадр. Используется Bearer
  в заголовке handshake, без deprecated `api_key` в сообщениях.
  [TTS API](https://soniox.com/docs/api-reference/tts/websocket-api),
  [STT API](https://soniox.com/docs/api-reference/stt/websocket-api),
  [Authentication](https://soniox.com/docs/guides/websocket-authentication).

## Поставка и интерфейс для Claude

Новый пакет `ai_agent_adapters.voice`, основной модуль `soniox.py`.
Новые зависимости и общие контракты не добавлены: используется существующий
`websockets.asyncio`, pydantic SecretStr и стандартная библиотека.

```python
from contextlib import aclosing
from ai_agent_adapters.voice.soniox import SonioxAdapter, SonioxConfig

# trusted_env — явно загруженный host .env; в контейнер агента не передавать.
speech = SonioxAdapter.from_env(SonioxConfig(), environ=trusted_env)
try:
    ogg_bytes = await speech.synthesize_ogg(approved_text, selected_voice, language="ru")
    async with aclosing(speech.stream_pcm(approved_text, selected_voice)) as stream:
        async for pcm_chunk in stream:
            ...  # host audio sink: mono s16le, config.sample_rate
    async with aclosing(speech.transcribe(audio_chunks, sample_rate=16000)) as stream:
        async for update in stream:
            ...  # final tokens append, provisional hypothesis replaces the previous one
finally:
    await speech.aclose()
```

- Единственное имя ключа — `AI_PLATFORM_SONIOX_API_KEY`. `from_env()` не ищет
  файлы и не читает ключ при импорте; composition передаёт явно загруженное
  окружение. Диагностикой значение реального ключа не проверялось.
- Настройки: модели, endpoints/регион, sample rate, Opus bitrate, timeouts,
  максимальный объём входа/выхода. Голос обязателен на вызове. По умолчанию
  TTS — 24 кГц, Opus 32 кбит/с; STT — mono s16le 16 кГц с подсказкой `ru`.
- Методы используют отдельный WS на операцию. Нет скрытого повторного платного
  запроса, воспроизведения, записи файла или автоматической отправки в Telegram.
  Неполный Ogg не возвращается успешно. PCM выдаётся по мере получения, поэтому
  уже проигранные байты нельзя отозвать после поздней ошибки — sink обязан
  остановиться и сбросить оставшийся буфер.
- STT читает async-итератор, а не микрофон. Передаёт бинарное аудио, шлёт
  keepalive при паузах; различает provisional/final tokens, сохраняет служебные
  endpoint-токены как данные. Само распознавание не даёт команд и не вызывает LLM.
- `aclose()` закрывает WS и отменяет фоновые задачи, включая handshake и
  отправителя аудио; входной async-генератор явно закрывается. После close
  adapter не переиспользуется; на следующий разрешённый запуск нужен новый.
  Caller должен закрывать прекращённые до EOF stream-генераторы через `aclosing`.
- Ошибки содержат фиксированный код и необязательный status, без provider
  error_message, текста, аудио, URL и ключа. Wire-logger websocket отключён,
  чтобы DEBUG не печатал Authorization. Обычный удалённый plaintext `ws://`
  не принимается; явное исключение — только loopback для тестов.
- Неизвестная стоимость не превращается в 0: WS-ответ не предоставляет здесь
  полного invoice usage. Процессированные миллисекунды STT — не сумма списания.
  Денежный ledger, лимит и корректная оценка расхода остаются у composition.

## Проверки и предел доказательств

- `pytest -q tests/voice`: **16 passed**, около 0,4 с.
- Ruff пакета и тестов — чисто; strict mypy двух новых модулей с
  `--follow-imports=silent` и исходниками своего checkout — чисто.
- Реальный локальный WebSocket-сервер, вымышленные текст/ключ/аудиобайты:
  auth отдельно от JSON, Ogg header guard, PCM alignment, terminal handshake,
  provider error/402, неверный stream/base64, лимит, STT finality и text EOF,
  disconnect без finished, input failure и отсутствие скрытого retry.
- STOP-регрессии: buffered terminal после close не даёт TTS success,
  незавершённый handshake отменяется, suspended input iterator закрывается,
  задачи/сокеты освобождаются, включая отмену waiter сразу после успешного connect.
  Узкое независимое ревью выявило эти места;
  исправления покрыты тестами.
- Первый targeted прогон: 11 passed, но Ruff нашёл длинный комментарий, mypy —
  две неточные аннотации async-generator для `aclosing`. Исправлены; повторён
  только затронутый набор с добавленными регрессиями.

**Не проверено:** настоящий Soniox, голос/произношение, latency, воспроизведение
и декодирование настоящего Ogg, тариф аккаунта, расход, устройства Windows,
Telegram/OBS/Minecraft, публикация с модератором, сквозной STOP платформы.
Фикстура Ogg проверяет только framing/signatures и намеренно не является
настоящей звуковой записью. Реальные ключи, микрофон и платные API не использовались.
Полного gate не было. Интеграция и решения D84–D87 остаются за Claude/оператором.

# OC-03 рабочие заметки (черновик, не отчёт)

Исторический снимок WIP Claude. Исправления загрузки/рендера, проверки ACK и итог
Windows-сборки: [продолжение Codex](OC-03-render-diagnosis.md). Список «Открыто»
ниже сохранён как исходное состояние исследования, не как текущий статус.

Исходники: `~/projects/ai-pers2/opencode-src`, тег v1.19.3 (`e3c4eb2`), ветка патча `aw-bridge`.
Сборка: bun 1.3.14 (`~/.bun/bin`, sha256 проверен), `bun install --filter '@opencode-ai/app'
--ignore-scripts`, `bun x vite build --base /opencode/` в `packages/app` (~1 м 40 с, ~2.3 ГБ RSS).

## Точки патча (packages/app)

- `src/entry.tsx`: `platform.fetch = awFetch` (`src/aw/bridge.ts`), сервер — виртуальный
  `${origin}/opencode-native`, `router` с `base="/opencode"`.
- `src/context/server-sdk.tsx` `eventFetch`: для loopback берёт глобальный fetch (`/api/event`
  уходит в сеть) — патч: всегда `platform.fetch`.
- Хранение: `utils/persist.ts` (localStorage), `context/language.tsx`, `entry.tsx`
  (defaultServerUrl), `utils/draft-store.ts` (IndexedDB) → память страницы.
- `vite.js`: inline theme preload (CSP запрещает inline) → внешний файл/убрать.
- Размер: 952 файлов/34 MiB без map (лимит 512/32): 836 js-чанков (shiki-языки), ghostty-web
  (терминал, PTY запрещён), шрифты ttf/woff, 26 aac, mp4 — урезать.

## Протокол

Проба: `/global/health` (v1) затем `/api/health` (`pid:number` ⇒ v2). Эмулируем v2 (`/api/*`).
Маршруты клиента: `@opencode-ai/client` (vendor 1.17.13-v2, `dist/contract.d.ts`).
Старт UI сейчас вызывает: GET /api/session, /api/event (SSE), /global/health.

Соответствие bridge `aw-opencode/1` (`/api/v1/opencode`):
project ↔ agents[directory], session ↔ SessionBook (directory, session_id),
messages ↔ transcript entries (operator=user, character=assistant), pending_reply ⇒ ACK
`dialogue.acknowledge` только после видимого показа; prompt ⇒ `work.session.message`
(expected_version=session_version), new session ⇒ `work.session.open`; events ⇒
`/api/v1/opencode/agents/{directory}/events` (connected/refetch/closed) ⇒ перечитать.
Модели/провайдеры/PTY/permission/auth — пусто или недоступно; STOP — прямой endpoint.

## Состояние на ветке aw-bridge (WIP)

Работает против настоящего host (стенд: scratchpad `stand.py`/`ocbuild.sh`/`ocstand.sh`,
лимиты временно 1024/48 в отдельном worktree `ocdev`):
- v2-протокол (`/api/health` с pid), виртуальный сервер = origin, все запросы через адаптер;
- каталог агентов = проекты (только work-память; тест скрыт, данные не трогаются), имя = persona_id;
- классическая раскладка (адрес `/opencode/<base64(aw_…)>/session/<id>` — reload работает);
- новая сессия → `work.session.open`, prompt → `work.session.message` (id `msg-<seq>`);
- одна «личность»-агент и «Модель платформы» (gateway), не Build/Plan;
- SSE bridge refetch → `session.input.promoted` (клиент сам догружает сообщение по id),
  `session.execution.started/succeeded`; событие сессии в v1-форме;
- хранение: localStorage/IndexedDB → память страницы (проверено: 0 записей).

Открыто:
- ответ ассистента (draft `reply-<seq>`) догружается, но не отрисовывается; с ним в списке
  страница после reload пустая (без исключения). Разобрать рендер ходов session page.
- статусы хода: тип `system` клиент не рисует — нужен другой носитель.
- ACK по видимости реализован, не проверен (зависит от отрисовки).
- шрифты `data:` блокируются CSP `font-src 'self'` (вопрос Codex или вынести в файлы).
- лимиты поставки, подписи «~aw_…»/«56 лет назад», DEBUG-хуки убрать.

## Продолжение после OC-03-render-diagnosis (Claude, 2026-10-24)

Кандидат Codex (`bda569a`, патчи 1–4) на Linux: сборка 938 файлов / 35 МиБ в пределах
1024/48; `tests/panel/test_native_opencode_sessions.py` (headless Chromium, синтетический
harness) — 1 passed; Bun-тесты адаптера 77 passed. «Пустая страница» прежнего стенда —
экран загрузки на медленном VPS (снимок через 6 с), через 8 с сессия и ответ на месте;
ACK после показа: история `operator, character`, черновика нет, один вызов модели.

Патч 5: статусы хода на месте ответа (тип `system` клиент не рисует), id клиента для
отправленного сообщения (иначе дубль до reload), имя личности вместо `aw_…`/git-блока.
Серия 1–5 накладывается на чистый тег, дерево совпадает с проверенным.
Linux typecheck и повтор «пустой страницы» — пакет GLM-OC-C, не дублируется.

### OC-06 (Linux, родной UI) и найденные пробелы

`tests/panel/test_native_opencode_journey.py` (синтетический harness, production owners,
headless Chromium): сообщение в родном окне → «ждёт START» → START → ответ, ACK → личность
спрашивает оператора тикетом → ответ в панели обычным текстом → тот же разговор продолжается
в родном окне без новой реплики оператора (ответы `reply-1`, `reply-1-2`) → STOP →
сообщение с отозванным основанием не исполняется, родное окно говорит об остановке →
`/opencode/api/session`, `/opencode/pty`, `/opencode/global/health` не отдаются, чужой
directory отклонён, страница ходит только к своим ассетам/панели/`/api/v1/`, хранилище пусто.
Вместе с тестом Codex — 2 passed. В обоих тестах START теперь ждёт подтверждения ядра перед
закрытием страницы управления (на медленной машине команда терялась).

Пробелы (запросы владельцам, не обходятся в клиенте):
1. Хост знает одну личность (`persona_id`, одна рабочая область в `host.json`/`auth.scopes`):
   каталог bridge не может показать вторую. Нужен owner-путь добавления личности (local_host).
2. ~~Открытые обращения разговора~~ — сделано (окно передано Codex): `WorkSessionTranscript.
   open_ticket_ids` (пусто по умолчанию; только обращения этой сессии из `waiting_ticket_ids`,
   в её scope, по текущему состоянию владельца тикетов, ждущие оператора), generated, панель
   открывает `#/tickets?ticket=<id>`, старое окно разговора и родное окно ссылаются на карточку.
   Проверки: contracts/workplace_contracts/sessions/conversation 129 passed; панель tsc/eslint/
   209 node tests/build; браузер: оба нативных теста, обращения, ACK-разговор — 7 passed.

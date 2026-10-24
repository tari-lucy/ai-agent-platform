# TS2741 `preconnect`: патч 9 к native-клиенту

Claude Code, 2026-10-24. Ветка `claude/ts2741-preconnect` от main `56e1b41`. Закрывает
блокер из отчётов GLM (`GLM-opencode-acceptance.md`, `GLM-final-candidate-acceptance.md`,
`c03a108`): `bun run typecheck` серии 1–8 падал двумя `TS2741 preconnect`
(`src/aw/bridge.ts:358`, `src/aw/bridge.test.ts:10`).

## Причина и исправление

`Platform.fetch` в upstream объявлен как `typeof fetch`; под типами Bun это включает
`fetch.preconnect`. Адаптер `awFetch` и заглушка `globalThis.fetch` в его тесте были
голыми функциями. Патч 9 (`0009-…`): обработчик стал функцией `bridgeFetch` с явной сигнатурой
`(input: RequestInfo | URL, init?: RequestInit) => Promise<Response>`, а экспорт
`awFetch: typeof fetch = Object.assign(bridgeFetch, { preconnect: () => {} })`; заглушка
теста — так же. `preconnect` — намеренный no-op: адаптер сам соединений не открывает.
Upstream-тип `Platform.fetch`, tsconfig и зависимости не менялись, приведений `as` нет;
маршрутизация и поведение адаптера прежние.

## Проверки (Linux, Bun 1.3.14, локальный клон upstream)

- `packages/app`: `bun run typecheck` (`tsgo -b`) — без ошибок (до патча — те же 2 × TS2741).
- `bun test src/aw` — 2 pass. Полный `test:unit` — 721 pass / 6 fail; те же 6
  (`persist localStorage resilience`) падают и без патча 9: серия делает хранилище страничным,
  к fetch они не относятся.
- Серия 1–9 через `git am` на чистом `e3c4eb2` даёт дерево `35e7690…`, совпадающее с
  проверенным исходником. `dev/bridge.ts.snapshot` обновлён.

Не выполнялись (по поручению): полный gate, Vite-сборка/упаковка, браузерные и
Windows/Docker прогоны. Патч меняет только типы, поэтому собранный bundle по логике не
меняется, но заново это не проверялось. Старые формы панели сохранены.

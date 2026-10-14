# Основа панели — M4-01

Русская React/Mantine оболочка с семью областями, входом/выходом,
выбором области просмотра, отдельной целью STOP и bounded WebSocket.
Приёмка **UI-only**: диалог, лаборатория, редактирование и остальные
бизнес-экраны не реализуются этой задачей и не имитируют успешную работу.

## Разработка и проверки

Node/npm — версии из package.json; зависимости только из package-lock.json:

```text
npm ci --prefix apps/panel
npm run lint --prefix apps/panel
npm run typecheck --prefix apps/panel
npm test --prefix apps/panel
npm run build --prefix apps/panel
```

Типы импортируются прямо из packages/contracts/generated/contracts.ts.
Генератор и контракты принадлежат backend owner; копию схемы не создавать.
Новых npm-зависимостей/версий M4-01 не добавляет.
Unit tests используют встроенный Node test runner/TypeScript stripping.

dist обслуживается core с тем же origin, что и /api/v1 и /api/v1/events.
HashRouter не требует fallback всех путей в index.html. npm run dev —
отдельный frontend server для разработки; proxy/CORS/автоматический вход
не включены. Для входа нужен same-origin core с локально настроенным auth
по apps/core/src/ai_agent_core/access/README.md. Docker panel stage сохраняет
repo layout и копирует canonical TS export; .dockerignore допускает только
этот generated файл. Compose/публикация порта этой задачей не меняются.

## Порты следующему владельцу экрана

- shared/contracts.ts — re-export канонических wire types.
- shared/api.ts — PanelApi: restore/login/logout/runs/profiles/stop/command.
  HTTP relative same-origin, cookie управляет браузер, CSRF только RAM.
  Команды не повторяются автоматически; ID команды принадлежит вызывающему.
  При неизвестном исходе сначала читать квитанцию/состояние владельца.
- shared/scope.ts — partition key, проверка scope, projectionKey.
  Проекция имеет ключ ['projection', scopeKey(scope), resource].
  Включены workspace/persona/memory_space/memory_mode; ссылки run/session
  проверяются отдельно. Выбор области не является agent.start.
- shared/cache.ts — QueryClient без retry/focus/reconnect refetch;
  gc60s/stale5s, только RAM, без persistence/devtools.
- shared/events.ts — одна подписка на выбранную область и при необходимости
  отдельная на область активного owner; не более двух одновременно.
  До5 reconnects за lifetime, handshake10s, metadata frame16KiB,
  дедуп/версии до128. after_event_id не отправляется: replay owner ещё не
  подключён. Соединение/событие инвалидирует проекции, не повторяет команду.
- shared/projections.ts — версия одного run не откатывается поздним чтением;
  новое owner-подтверждение заменяет прежний STOPPING.
- ui/Workspace.tsx — единственный shared shell/router. Будущий screen owner
  подключает компонент своего маршрута; не создаёт второй API client/cache.

RunSession берётся у Supervisor через sessions, Profile — через personas.
Показывается первая страница профилей с явной неполнотой, limit50.
Отсутствие owner/grant/проекции отображается ошибкой/пустым состоянием.
Profile ID — техническая подпись до принятого display-name projection.
Текст профиля отображается как plain text; HTML/Markdown не исполняется.

STOP берёт agent_id и scope фактического owner, включая run/session fencing;
никогда не берёт цель из просмотренного профиля. Он идёт отдельным HTTP
запросом6s, без WS/query queue/expected profile version. При отсутствии
проверяемого owner кнопка видна с объяснением, исполнение закрыто.
STOPPING/FAULTED и неподтверждённые ресурсы не выдаются за STOPPED.
Неопределённый ответ показывается явно и автоматически не повторяется.

Logout/401/absolute expiry очищают RAM auth/cache и закрывают подписки;
поздние ответы предыдущей сессии не возвращаются в новую.
Фоновый polling сессии не включён: открытая вкладка сама не продлевает idle.
Logout не означает STOP агента. Start/resume/паузы других владельцев,
генерация и публичный выпуск этой оболочкой не запускаются.

## Браузерная приёмка

См. tests/panel/README.md. Настоящие HTTP/WS M1 пути проверяются отдельно от
mock projections. Полный browser cycle M2/M3/M4 и реальный VPS остаются
интеграционной приёмке после подключения backend owners.

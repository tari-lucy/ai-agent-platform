# FAULTED / OUTCOME_UNKNOWN без причины: диагностика

Claude Code, 2026-10-27, по поручению Codex. База main `6562814`, ветка
`claude/faulted-diagnostics`. Окно: `bootstrap.py`, `control/supervisor.py`.

## Что было

- `bootstrap.runtime_watchdog` ловил любое исключение `Runtime.tick()` (включая
  re-raise задания через `_active.result()`, `jobs.reconcile`, heartbeat, prewarm) и
  вызывал `critical_failure()` без следа.
- `Supervisor.critical_failure()` и FAULTED по сроку STOP ничего не журналировали; 13 мест
  вызова (`poll`, `refresh`, `flush`, warm/runner без квитанции удаления, …).
- `Supervisor.poll()` делает FAULTED(STALE_CONTEXT), если между опросами прошло больше
  `sleep_gap_seconds` (5 с) монотонного времени — это защита от сна хоста, но так же
  срабатывает **заблокированный синхронной работой цикл событий**.

Сверка с отметками Codex: lease 20 с (`expires_at` 17:46:16 ≈ захват 17:45:56 + 20 с),
unknown 17:46:10 — раньше истечения UTC lease и deadline. Это совпадает с FAULTED из
`poll()` после блокировки цикла >5 с или с прямым `critical_failure`: оба отзывают
допуски, `reconcile` помечает попытку `unknown/OUTCOME_UNKNOWN`, задание — CANCELLED.
Какая из веток на личном host — покажет журнал ниже.

## Что добавлено (без текста, значений и секретов)

- `Run faulted: <код> at <файл>:<функция>:<строка> (was <состояние>)` — каждое
  `critical_failure`.
- `Run clock gap: monotonic=…s wall=…s limit=…s` — перед FAULTED из `poll`.
- `Run faulted: STOP deadline with N unconfirmed resources`.
- `Runtime tick failed: <тип> <код или -> at <места кода>` — сообщение исключения не
  пишется.
- Поток `loop-stall`: если цикл событий не работал ≥1 с, один раз пишет
  `Event loop blocked <сек> at <места кода главного потока>` — какая синхронная работа
  держит цикл. Только места в коде.

Лимиты, STOP, проверки допусков не менялись.

## Проверки

- `tests/local_host/test_fault_diagnostics.py` (синтетический host, контролируемая
  задержка): синхронная блокировка шага тика дольше лимита (снижен до 1 с в тесте) →
  FAULTED/STALE_CONTEXT, журнал называет блокирующую функцию, разрыв и место отказа;
  исключение в tick → тип и места кода, текст сообщения не попадает в журнал. 2 passed.
- `tests/control`, `tests/jobs`, `tests/local_host`: 145 passed; mypy чисто. Полный suite
  не запускался, платных вызовов нет.

## Что сделать на личном host

Запустить сервер с этой веткой, повторить ход и прислать строки `Run faulted`,
`Run clock gap`, `Runtime tick failed`, `Event loop blocked` из окна сервера.
`Event loop blocked … at …` назовёт код, который держит цикл (кандидаты для проверки:
синхронное чтение/хеширование крупных файлов при stage/verify, большие выборки SQLite).

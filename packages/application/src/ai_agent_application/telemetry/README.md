# Bounded telemetry / health / trace — M1-06

Одна синхронная проекция core/event loop, без IO, flush и logging callbacks.
TelemetryLimits ограничивает components/latest/traces/clients/client_events:
64 / 256 / 256 / 32 / 256. Active traces имеют backpressure, завершённая история
вытесняется по пределу. Эта RAM-проекция не заменяет durable job/usage/audit ledger.

Компоненты регистрирует доверенная композиция. HealthState: ready/degraded/unavailable,
TTL по monotonic (default20 s). Отсутствие свежей проверки — unavailable.
Необязательный компонент ограничивает свою capability; critical_failure callback
у обязательного компонента отзывает supervisor. Возврат ready не start/resume.

emit принимает только Metric, ограниченный integer, Outcome/ErrorCode.
Полей prompt/message/error_text/headers/endpoint/credential/payload нет.
Scope и request/operation/attempt correlation IDs хешируются; trace_id генерируется UUID.
begin/end сохраняют длительность и различают known outcomes и UNKNOWN.
end с тем же token/outcome идемпотентен; подмена token/outcome отвергается.
Не передавать секрет даже как identifier: IDs — это IDs, не произвольный контекст.

Последнее событие coalesces по scope/component/metric; вытеснение видно счётчиком.
Subscription имеет bounded deque, переполнение закрывает только этого клиента
с RESOURCE_BUSY и освобождает его буфер. Drain не запускает модель.
Другие клиенты продолжают получать события. Scope/run/session/work-test изолированы.
M1-05 обязан проверять session/Origin/scope перед subscribe и каждым сетевым доступом;
сам локальный объект Subscription не является сетевым допуском.

Bootstrap хранит telemetry/scheduler в app.state и poll timer50ms,
без регистрации действующих capabilities и без agent start.
Health control/resource_scheduler обозначает доступность этих локальных владельцев,
не готовность Redis/LLM/памяти: эти компоненты подключают и проверяют их владельцы.

# Resource scheduler — принятый внутренний порт M1-06

Один владелец бюджета на workspace/event loop core. Создавать со STOPPED supervisor
до recover/start; новая копия scheduler при активном supervisor запрещена.
Он распределяет ресурсы, а права/источники/модерацию и действующую попытку
проверяют Policy/JobOwner. BudgetLease — локальный core token, не сетевые credentials.

Доверенная композиция регистрирует Capability и точный набор Demand(pool_id, units)
по resource_ids этой версии. Lane задаёт registry, не arguments модели.
Компоненты health заранее регистрируются у Telemetry; capability version change
закрывает старые pending/active permits. Чужая partition не регистрируется.

Defaults: llm 4 (1 резерв модерации), cpu 2000 milliCPU (200 резерв), ram 1536 MiB
(128 резерв), background 1, embedding 1, foreground 1.
Bootstrap берёт worker RAM/pending из проверенной конфигурации.
Reserved lane должна помещаться в своих резервных CPU/RAM/LLM единицах.
Обычные запросы не занимают резерв даже при свободном llm слоте.
Это учёт заявленной стоимости capability, не измеритель RSS или замена Docker limits.

GM/reflection спецификации включают background; один смысловой ход — foreground;
embedder — embedding, threads 2 / batch 4 контролирует его адаптер.
Indexing использует background priority и отдаёт embedding между batch:
foreground recall получает следующий слот. Модерация имеет самый высокий приоритет;
normal calls не выбирают её lane. Ожидание конечное, по умолчанию 20 s.
При непрерывном foreground фон может истечь с видимым EXPIRED; вечной очереди нет.

Порядок вызовов доверенного исполнителя:
1. Получить актуальный JobOwner Claim; acquired=False не разрешает повтор computation.
2. request(command, attempt_id=claim.lease.attempt_id). Для локального пути без durable
   job fallback key — command.id; настоящие job retries всегда используют attempt_id.
3. acquire(request) → Allocation либо None. acquired=False — только status query.
   None оставляет bounded WAITING с reason/wake_event. Во время ожидания проверять
   отмену/срок; JobOwner heartbeat продлевает исходный cancellation_id, не заменяет его.
4. Непосредственно перед IO: JobOwner.verify(worker, claim.lease) и scheduler.verify(budget).
   После каждого await перепроверять допуски перед следующим эффектом/передачей результата.
5. JobOwner принимает технический result. complete(budget, Outcome.*) получает только
   фактическую аутентифицированную квитанцию завершения/отмены, после остановки compute/IO.

Попытка, завершившаяся подтверждённой FAILED, может получить новый budget по новому
JobOwner attempt_id. Живая/отменяемая/UNKNOWN попытка той же операции блокирует новый budget.
Решение о допустимости retry и максимум 2 реальные попытки остаются у JobOwner/Policy;
не добавлять вложенные provider retries, умножающие этот предел.

STOP/expiry/health loss закрывают verify, но сохраняют занятые единицы и ResourceTicket.
Без квитанции после 2 s cancellation grace результат UNKNOWN. Возврат health не оживляет
этот permit. complete(UNKNOWN) тоже удерживает ресурс; поздняя фактическая reconciliation
может освободить его, не возобновляет supervisor и не применяет ответ.
Освобождение budget не является отменой публикации или подтверждением доставки.

В памяти: pending ≤256, active ≤64 default (max200), capabilities ≤64 default,
pools ≤32 default, history ≤256. История эфемерна; полный job/result/retry truth — SQLite M1-04.
После потери процесса новый supervisor STOPPED; recover/reconcile старых executor resources
обязательны до start. Сброс RAM scheduler не считается такой сверкой.

Сеть, authenticated service routes, реальные worker loops/provider IO — назначенные
потребители M1-05/M2; M1-06 не даёт worker доступ к основной БД.

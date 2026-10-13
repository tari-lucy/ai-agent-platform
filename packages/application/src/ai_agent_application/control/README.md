# Supervisor: принятый внутренний интерфейс M1-03

Один экземпляр на workspace, один поток/event loop ядра. Constructor всегда
создаёт STOPPED с новым run/session ID. Существующий RunSession snapshot не
даёт допуск. Domain, модель и интеграции не определяют переходы управления.

## Композиция

Core bootstrap создаёт закрытого supervisor и локальный watchdog 50 мс.
Journal/readiness по умолчанию недоступны: startup не мигрирует БД, не выдаёт
права и не начинает работу. Нет HTTP handlers; access owner M1-05 должен
связать аутентифицированную сессию с ControlAuthority. Это объект доверенной
композиции, а не JSON credential: клиент не создаёт его из своих аргументов.
Проверки Origin/CSRF/service identity остаются у access owner.

SQLiteRunJournal получает core_owner(supervisor) и явно настроенные scope
partition/grants. Provision/migration — отдельные административные действия.
Readiness.check проверяет принятую версию профиля, текущие права, схему и
необходимые подключения; proof привязан к свежему полному scope и сроку.
Поиск/LLM/брокер не выполняются в STOP. В тестах readiness — synthetic fixture.

До первого start вызывается явная recover. Она находит занятую workspace
slot, включая другую личность/work-test partition. Readiness.reconcile
возвращает пустой список только после фактической сверки старых исполнителей.
Неизвестные ресурсы оставляют старый FAULTED и занятую slot; новый запуск
закрыт. Восстановление само не выполняет start. При работе в нескольких
процессах Python-порты не заменяют межпроцессный арбитраж; MVP core — один процесс.

## Управление

ControlCommand start/pause/resume/session.end использует expected_version
живой проекции supervisor.snapshot. STOP использует StopCommand без версии
профиля и возвращает StopReceipt непосредственно после отзыва в памяти.
Нельзя ждать flush, LLM, ACK или Redis перед возвратом STOP receipt.

STOP увеличивает epoch и отзывает все admissions. До подтверждения
зарегистрированных ресурсов состояние STOPPING; deadline 2 с переводит
неопределённость в FAULTED. Истечение admission lease не является cancel receipt.
acknowledge получает точный ResourceTicket через проверенный служебный канал.
Подтверждение фактически исполненного результата допустимо для технической
истории; отмена не откатывает уже случившееся.

Пауза прекращает новые автономные admissions, отзывает старые activity,
voice, gm и reflection admissions, но сохраняет флаги голоса/GM и разрешённое
наблюдение. PAUSING имеет deadline 2 с; неизвестность закрывает воздействия.
Resume заново проверяет контекст и права; старые tokens не восстанавливаются.
Наблюдение не блокирует resume. set_voice и set_gm — отдельные внутренние
операторские операции с проверкой версии; mute/GM pause не отменяют несвязанную
деятельность. Wire routes для них назначаются владельцу контрактов при wiring.

Watchdog/poll закрывает допуски при scheduler gap > 5 с, изменении разницы
UTC/monotonic > 5 с или истечении readiness proof. Это консервативная защита
сна/задержки, не распознавание причины паузы ОС. Если целиком завис процесс,
нужны отдельные локальная остановка/внешний watchdog будущих мостов.

## Финальный допуск

Job owner сохраняет admission.id в ExecutionCommand.cancellation_id.
После асинхронного вычисления он вызывает resolve с сохранённым ID, полным
scope и Freshness, затем finish перед локальным применением. Нельзя выдать
новый admission старому результату после pause/mute/STOP. Совпадения epoch
недостаточно при отмене отдельной возможности.

finish принимает только доверенный ограниченный синхронный owner handoff.
Между проверкой и применением нет await. Внутри callback запрещены блокирующий
IO/SQL, model call или coroutine; async callbacks отклоняются. Порт не является
sandbox произвольного Python и не даёт атомарности с внешним API/SQLite commit.
Права/источники/версии/модерацию/аренду попытки проверяют соответствующие владельцы
перед этим handoff; токен supervisor не заменяет эти проверки.

Admission имеет конечный monotonic deadline (default 20 с), максимум до
срока proof. Автоматического продления нет. M1-04 должен согласовать owner
port для проверенного heartbeat/refresh перед длинными заданиями; нельзя
восстановить просроченный ID через повторный admit. Technical usage и сверка
доставки проходят собственных владельцев независимо от допусков воздействия.

## Сохранение и пределы

SQLite journal атомарно пишет RunSession + Receipt + outbox через принятый
M1-02 UoW. CAS DB revision и revision живой control projection различаются:
coalesced snapshots получают очередную линейную DB version. API управления
читает supervisor.snapshot, а не версию отставшего DB snapshot.

flush — отдельная явная попытка. Ошибка закрывает ACTIVE/STARTING. STOP
отзывает память немедленно; snapshot догоняет позже (persistence_pending).
Зависшая запись старого ACTIVE может закончиться после STOP: это техническая
проекция, а не новый допуск. После неё нужен flush актуального состояния;
fresh boot/recover никогда не используют snapshot как право выполнения.
Outbox consumer обязан читать живого owner перед воздействием.

Буферы конечны: 200 ресурсов, 256 admissions, 256 cached requests. Full request
cache даёт backpressure обычным командам; STOP остаётся доступным. Cache живёт
в процессе и не обещает долгосрочную дедупликацию HTTP-команд. M1-05 должен
связать receipt retention/idempotency со storage и не блокировать emergency path.
Core constructor/readiness не предоставляют стороннему Python sandbox.
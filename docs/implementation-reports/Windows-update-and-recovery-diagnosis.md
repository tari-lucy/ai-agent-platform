# Windows: updater и диагностика восстановления после перезагрузки

Дата: 2026-10-28. База updater: `6562814`. Проверенный deployment:
`3664fb0`, ветка personal-diagnostics. Runtime платформы не менялся.

## Диагностика восстановления — только чтение

В текущих ревизиях RunSession личного host найдены 31 STOPPED и один FAULTED.
У FAULTED остаётся один `warm-aw-task-…` в unconfirmed_resource_ids, версия 71.
Поля last_error в прочитанных записях нет: SQL извлечение даёт null, это не
доказательство отсутствия ошибки в живом supervisor.

В Docker Desktop действительно остаются соответствующий task-контейнер и
egress-контейнер: оба Exited, exit code 255. Сеть aw-net существует, подключений 0.
Readiness.reconcile возвращает прежний список без обращения в Docker;
Supervisor.recover сохраняет FAULTED при неизвестных ресурсах и не выставляет
_recovered, handle отклоняет START с SERVICE_UNAVAILABLE. Этот путь подтверждён
кодом и сохранённым состоянием; состояние поля _recovered в живом процессе
не извлекалось. Наличие старого остановленного контейнера нельзя объявлять
отсутствием ресурса. Исправление владельцу recovery должно учитывать cleanup
остановленных точно сопоставленных ресурсов и повторную сверку.

БД, Docker-ресурсы, разрешения, сообщения и .env не менялись и не открывались
для диагностики. Читались только фиксированные поля RunSession в SQLite mode=ro.
Ручная разблокировка и START не выполнялись.

SSHD повторно зарегистрирован на установленный исполняемый файл, StartupType
Automatic, служба Running, порт 22 слушает. Проверка sshd -t успешна, pubkey
authentication включена, указанный оператором публичный ключ найден в обоих
authorized_keys. Сквозной вход с VPS не проверен: закрытый ключ находится у
другого исполнителя. Firewall и правила аутентификации не менялись.

## Один вход обновления

Добавлены Update-and-Start.cmd, PowerShell bootstrap и stdlib/httpx updater.
Пользовательская инструкция: [update-windows.md](../update-windows.md).
Отдельный кандидат сохраняет локальные commits, не перестраивает активный venv.
Перед переключением: preflight, штатный STOP, проверка ресурсов и текущих Job,
host lock, официальный backup create/verify с существующим ledger.
Launcher меняется атомарно; неудачный старт откатывает только launcher.
Нет START, миграций, восстановления БД, выдачи прав, чтения .env самим updater,
удаления старых checkout, reset/clean/force/push.

Узкий независимый просмотр выявил гонку блокировки updater, смену PID, чужой
listener при readiness и пропуск NULL статуса Job. Все четыре закрыты:
блокировка до readiness, повторная проверка PID после открытия process handle,
сопоставление listener с child.pid, COALESCE для неизвестного статуса.

Проверки: 13 focused tests passed на Windows (реальные временные Git repos и
SQLite, synthetic STOP/HTTP/handles); ruff и strict mypy по updater чистые.
Первый повтор прогона был блокирован Temp sandbox и отсутствующим родительским
каталогом basetemp; после назначения отдельного workspace basetemp набор прошёл.
Ранее read-only --check прошёл на чистом прежнем deployment. Текущий deployment
имеет удалённый tracked apps/panel/node_modules: проверка честно выдаёт DIRTY,
чужое изменение не восстанавливалось.

Не проверено: полное обновление личного host и настоящая смена процесса новым
updater, GitHub credential prompt и сборка нового кандидата через этот скрипт.
Не запускались полный suite, контейнеры и платные API. Updater не устраняет
блокировку recovery: до неё штатный STOP может отказать, переключения не будет.

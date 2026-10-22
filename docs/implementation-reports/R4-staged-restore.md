# R4 / AW-09: staged restore рабочего места

База: main `df9816c`. Исполнитель: Codex, отдельный checkout/ветка
`codex/r1-agent-session`. Этап ограничен backup/staging/switch/finish рабочего
места. Production launcher, перенос личного host и полный R1/R4/R5 не закрываются.

## Подключение

- Тот же BackupService и manifest: snapshot-extra `WorkplaceSnapshotFiles`
  добавляет bounded `workplace-blobs.zip` по всем связанным редакциям ManagedFile
  из уже согласованного SQLite-снимка. Orphans, секреты, OpenCode DB/cache и
  произвольные каталоги не копируются. Отсутствующий blob отменяет создание копии.
- Новый optional trusted hook `BackupService.stage_files(database, copy_dir)`
  готовит файлы до открытия RestoreTarget. Legacy service без hook не меняется.
  Bundle проверяется и разворачивается только в новый частный staging-каталог.
- RetentionService в staging получает существующий WorkplacePurger. Независимый
  ledger применяется до switch, в том числе к истории, скрытым редакциям и
  случаям уже очищенного/отсутствующего Source. Удаляются неразделимые
  производные: файлы, skills, tickets, WorkAttempt/WorkSession/сообщения/Utterance
  по существующему графу lineage. Второго ledger или особого delete API нет.
- Preview включает число затронутых записей и fingerprint плана/актуального
  ledger; изменение истории удалений делает ранее подтверждённый digest устаревшим.
  Выключение feature flags не разрешает восстановить некомплектную историю.
  HostCompatibility сравнивает также условия/лимиты Permission, области/network
  manifest и состояние binding: прежний более широкий доступ не возвращается.
- SessionBook.reset_context — внутренний fenced owner-port для offline restore,
  CAS/replay. История, processed_sequence, задачи и учтённые ответы на тикеты
  сохраняются; generation увеличивается, pending_reply/checkpoint/context refs
  сбрасываются. Он не доставляет ответ и не создаёт новую попытку.

## Переключение и закрытый запуск

После ledger/TTL/reindex проверяются digest/размер каждого blob и точный состав
каталога. Остаточный orphan не признаётся проверенным восстановлением.
GuardedSwitcher пишет RESTORE-PENDING, связанный с логическим fingerprint
выбранной staging-БД (устойчивым к WAL/checkpoint). Новый каталог подготавливается
рядом с настроенным blob root; это допускает внешний root на другом диске.
Старый root сохраняется как частный `.replaced-<id>`, затем переключается БД
существующим FileSwitcher. При штатном отказе старый blob root возвращается;
при любом незавершённом переключении marker оставляет serve закрытым.

finish-restore проверяет привязку БД, полный комплект blob, независимый ledger,
retention, derived indexes/reports и jobs inventory. Его отдельный preview
включает workplace impact; повтор требует решения и неизменившегося digest.
Только затем снимается marker. Старые outbox уже quarantined; JobOwner при
следующем open сверяет старые jobs до HTTP/worker. Restore/finish/serve не делают
START и не переисполняют неизвестные helper receipts.

Это защита от обнаруженного сбоя процесса/IO, не доказательство атомарности
группы файлов при power-cut. При RESTORE_SWITCH_UNPROVEN не удалять marker и
не запускать файлы вручную: нужна отдельная offline сверка выбранной копии и
сохранённых replaced-файлов. Самостоятельное присвоение исхода jobs исключено.
Replaced/backup-копии остаются приватными; немедленное физическое стирание всех
страховочных копий и форензическая невосстановимость на SSD не заявляются.

## Приёмка и границы

`test_staged_host_restore.py` использует настоящий init/build, SQLite/ledger,
SessionBook, рабочий bridge, SkillLibrary/ManagedFiles/TicketService и scripted
HTTP transport. Создаётся метод и тикет из рабочего разговора, snapshot, отказ
в staging без изменения live, restore/finish, новый serve STOPPED. Проверяются
сохранность метода/текста и независимой задачи, сброс контекста/черновика и
запрет старого ACK. Затем ledger блокирует основание после снимка: старый digest
отклоняется, новый restore удаляет текст и производные до допуска serve.
Marker отсутствует в активных БД/индексе/blob; ровно один модельный вызов до
снимка, после restore вызовов нет. Данные вымышленные, платных API нет.

`test_restore_switch.py` проверяет возврат старых blob при отказе переключения
и запрет finish для старой БД, даже если она читается; выбранная staging-БД
успешно проходит ту же проверку привязки.

Первый сквозной прогон обнаружил незакрытый SQLite handle тестового ASGI host
на Windows. Fixture теперь явно dispose-ит store перед offline restore;
production-проверки/OS lock не ослаблены. После этого два новых сценария —
2 passed (131.04 с). Итоговый affected gate: **63 passed (409.48 с)** —
`tests/workplace_lifecycle`, `tests/local_host/test_recovery.py`,
`tests/local_backup/test_workplace_gate.py`, `tests/backup/test_backup.py`,
`tests/workplace_sessions/test_sessions.py`. В финальном сценарии также проверен
старый ACK после восстановления. Ruff, strict mypy (**288 source files**),
import boundaries и generated exports check чистые. Полный suite и панельные
проверки не повторялись: API/TS/UI схемы этим этапом не меняются.

Личный host, профиль и `.env` не читались и не менялись. Новые права не выдаются.
Docker/OpenCode/browser и реальная модель здесь не запускались: восстанавливается
owner-состояние/файлы, а контейнерные caches не входят в копию. Linux результат
и включение restore в общий R5 journey поставляет Claude самостоятельно.

## Стык для Claude / R5

1. Взять свежий main с этой поставкой. Доступны прежние CLI create,
   preview-restore, restore и local_host finish-restore; второй backup-путь не нужен.
2. На disposable host с enable-recovery/workplace-files завершить все scripted
   вызовы перед снимком. Счётчик live API после снимка откатывать нельзя:
   API_ATTEMPTS_WOULD_ROLL_BACK остаётся намеренным отказом.
3. Завершить ASGI lifespan, `await rt.store.close()` и закрыть индексы перед
   offline CLI; в отдельном процессе serve сначала завершить этот процесс.
   Для теста можно использовать `lb_support.cli`, copy_id/digest_of.
4. create → preview-restore → restore с решением/digest → убедиться, что serve
   закрыт RESTORE-PENDING → finish-restore preview/apply → новый build/serve
   STOPPED. Старые черновики/ACK не возвращаются. Дополнить свой Linux journey
   этими шагами, не менять существующий файл Windows acceptance.
5. Для удаления после копии использовать тот же HostLedger/RetentionService.
   Подтвердить новый preview, проверить отсутствие старого текста/метода после
   восстановления. Расширение списка purgers в живом HostMaintenance и видимая
   причина заблокированного разговора остаются отдельными задачами.

Памятка самостоятельной проверки для оператора:
[manual-mvp-check](../manual-mvp-check.md). Интегрированный пользовательский
OpenCode launcher пока не опубликован, отдельное окно TUI/Web не обещается.

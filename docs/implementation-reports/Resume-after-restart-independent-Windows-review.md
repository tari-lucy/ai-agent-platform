# Независимая проверка автозапуска D88 — Windows

Дата: 2026-10-31. Кандидат Claude: `c578038`, база `70cbe94`.
Ветка ревью: `codex/review-resume-windows`.

**Вердикт: требуется исправление перед интеграцией и личным включением.**
Код кандидата не менялся. Добавлены только приёмочные регрессии и этот отчёт.
Личный host, его конфигурация, контейнеры, ОС и сервер не затрагивались.

## Подтверждённые замечания

### P1 — принятый STOP оставляет действующий resume при ошибке записи

`local_host/resume.py:102–105`: наблюдатель ловит OSError, только пишет предупреждение
и оставляет прежний файл. Реальное Windows-воспроизведение: CreateFileW открывает
временный resume.json на чтение без FILE_SHARE_DELETE. Это мешает os.replace,
но не чтению маркера. Настоящий Supervisor принимает STOP и становится STOPPED;
после закрытия handle `read(path)` по-прежнему возвращает True. Следующий процесс
использует именно этот старый True для нового START.

Регрессия: `test_stop_disarms_resume_even_if_windows_reader_prevents_atomic_replace`.
Это не поддельное исключение — штатная Windows sharing violation на тестовом файле.
Нужен отказ от автозапуска при неизвестном исходе disarm, а не только логирование.
Источник разрешения должен учитывать устойчивое последнее решение владельца.

### P1 — новый синхронный filesystem IO в критическом пути STOP

`local_host/resume.py:102` вызывает write из синхронного observe callback.
write создаёт каталог, пишет файл, делает chmod и os.replace. Supervisor.stop
синхронно вызывает этот наблюдатель через _revoke/_change/_notify.
Это нарушает явный контракт Supervisor.stop («no ... disk») и observe («no IO in
the STOP path»). Медленное хранилище/антивирус блокирует owner event loop и
обработку закрытия контейнеров. Проверка не использовала хрупкий порог времени:
перехват write зафиксировал именно вызов во время стека настоящего stop().

Регрессия: `test_real_supervisor_stop_path_has_no_filesystem_write`.
Исправление должно одновременно сохранить быстрый STOP и устранить первое
замечание; простой перенос записи в фон с подавлением ошибок недостаточен.

### P1 — restore начинает замену данных при ещё разрешённом автозапуске

`local_backup/cli.py:172–175`: clear вызывается после await service.restore.
При остановке процесса после фактического восстановления, но до clear, старый
True остаётся и следующий serve может запустить восстановленную личность.
Также пропускается clear, если restore изменил часть состояния и затем бросил
ошибку. Проверка границы владельца подтверждает: service.restore вызывается,
пока read(path) ещё True.

Регрессия: `test_restore_disarms_resume_before_calling_restore_owner`.
Снять разрешение необходимо под существующим offline lock до начала restore;
если это не удалось, восстановление не должно начинаться. Нужно также убедиться,
что сам backup не возвращает этот маркер.

### P2 — переход в observation сохраняет старое разрешение autonomous

`local_host/resume.py:93–100`: ACTIVE с autonomy=observation не подходит ни под
wanted, ни под stopped и игнорируется. Настоящий Supervisor: autonomous ACTIVE →
agent.pause → agent.resume(autonomy=observation). После этого маркер остаётся True.
При перезапуске run() принудительно отправит autonomy=autonomous, хотя последнее
решение оператора было «наблюдение».

Регрессия: `test_operator_switch_to_observation_disarms_autonomous_resume`.
При потере условий для autonomous возобновление должно разоружаться.

### P2 — на восстановлении нет проверки WORK и привязки маркера к partition

`local_host/resume.py:121–152`: файл содержит только bool; run проверяет STOPPED,
но не memory_mode и не исходную partition/личность. Настоящий Supervisor с TEST
scope и True-файлом запускается ACTIVE/autonomous. Это также делает собственный
положительный test_resume_real_supervisor кандидата: его fixture по умолчанию TEST.

Регрессия: `test_resume_does_not_create_autonomous_test_run`.
Текущий local_host обычно создаёт WORK supervisor, поэтому это не доказательство
живого запуска TEST на личном host. Это подтверждённая дыра входного порта и
неполнота тестов. Следует закрепить в маркере исходную partition/личность и
перепроверять WORK перед созданием внутреннего operator authority.

## Что подтверждено положительно

- Отдельной команды в обход Supervisor нет: используется обычный agent.start.
  expected_version, проверки authority/scope, readiness и epoch-token сохраняются.
  Внутренний ControlAuthority допустим как реализация доверенного opt-in D88;
  проблема не в самом конструкторе, а в надёжности основания для его выдачи.
- Priming observe игнорируется; существующие успешные тесты с реальным owner прошли.
- Новая независимая проверка настоящего Supervisor: STOP во время задержки отменяет
  возобновление, файл становится False и ACTIVE не возникает.
- Runtime.close сначала снимает running; wiring lifespan также снимает наблюдателя
  до закрытия runtime. Прямой ошибки этой последовательности при чтении не найдено.
  Убийство настоящего serve и перезагрузка ОС не проверялись.

## Результаты узкого прогона

14 тестов за 3.54 секунды: **8 passed, 6 failed**.

- Шесть новых регрессий: 1 passed, 5 failed — пять замечаний выше.
- Восемь тестов кандидата: 7 passed, 1 failed. Отдельный Windows failure:
  test_the_memory_is_private_and_atomic ожидает POSIX st_mode=0600, Windows
  возвращает 0666. Это не доказательство открытого NTFS ACL: его надо проверить
  отдельно, а тест сделать платформенным. Права исходного личного host не менялись.
- Ruff кандидата resume.py и новых тестов — чисто.
- Strict mypy resume.py с исходниками ветки в MYPYPATH — чисто.

Команда (PYTHONPATH включает исходники пакетов и tests/control):

```text
python -m pytest --import-mode=importlib --basetemp=<NEW_TEMP_ROOT> -q --tb=short
  tests/control/test_resume_review_regressions.py
  tests/control/test_resume_real_supervisor.py
  tests/local_host/test_resume.py
```

Красные регрессии сохранены намеренно; это ветка ревью, не пакет для merge в main.
После исправлений Claude адаптирует тесты при изменении порта и добивается зелёного
результата. Повторный полный gate сейчас не запускался.

Не проверено: настоящий serve subprocess/reboot, загрузка Windows и Docker,
длительная задержка 45 секунд/реальные минутные повторы, paid API, личный rollout.
Эти пробы пока не нужны для доказательства найденных нарушений. Заявление о
шести повторах при позднем Docker нельзя считать проверенным на основании
подставного supervisor: readiness failure настоящего owner может перевести его
в FAULTED, после чего run() уже не повторит START.

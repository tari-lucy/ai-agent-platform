# Windows: длинный рабочий ход и lease задания

Дата: 2026-10-28. Задание оператора: проверить гипотезу Windows-задержек продления
lease, собрать данные для Claude, платформу не исправлять.
База: `d7600ed113c9e702f9f3ec8ab24b3c8cc67e5b1e`, отдельный checkout и ветка
`codex/windows-long-turn-lease-audit`. Личный deployment не менялся и не останавливался.

## Стенд и воспроизведение

- Windows, Docker Desktop Linux Engine 29.4.0, 12 CPU / 8 171 761 664 байт памяти Docker.
- Hands image: `sha256:253e46306ddbf9ed0df4376c73f1355bb11224b360cc9685af4ae9ea374611cd`.
- Python 3.14 из существующего locked-окружения. PYTHONPATH: SOURCES и HELPERS из
  scripts/check_combined.py, с абсолютными путями нового checkout. Проверено,
  что ai_agent_core и ai_agent_adapters действительно загружены из d7600ed.
- `AW_HOME_IMAGE_ID=<image выше>`, `AW_EGRESS_LIVE=1`, `LONG_SLEEP=20`, `LONG_STEPS=14`.
- Команда: `python -X utf8 -m pytest -s -q --import-mode=importlib
  tests/windows_workspace/test_long_turn.py --basetemp=<отдельный временный каталог>`.
- Настоящие Docker/OpenCode/home/resident/egress; синтетическая модель и данные.
  `.env`, личная переписка и API-ключи не открывались; платных вызовов нет.

## Прогон A: без дополнительной нагрузки

```text
LONG result after 466.0 s; calls 15 state ACTIVE
LONG progress reply_ready None
1 passed in 509.09s (0:08:29)
```

Потеря lease **не воспроизвелась**. По реальному результату ответ готов,
а не просто pytest exit 0. В исходном тесте failed/unknown/cancelled также
завершают ожидание, но финальной проверки reply_ready нет — зелёный pytest
сам по себе успешный ход не доказывает. Тест не изменялся.

43 предупреждения Slow job lease renewal, все с attempt 1:
минимум 2.06 с, медиана 2.57 с, максимум 4.08 с. Примеры:

```text
LOG ai_agent_core.m3_composition.service Slow job lease renewal: 2.19s (attempt 1)
LOG ai_agent_core.m3_composition.service Slow job lease renewal: 3.71s (attempt 1)
LOG ai_agent_core.m3_composition.service Slow job lease renewal: 4.08s (attempt 1)
```

Нулевые количества: Slow runtime tick step, Job admission lost, Resource lease
cancelled, Run faulted, Event loop blocked. Тестовая SQLite после прогона:
1 310 720 байт, 293 record_revisions. Это существенно меньше личного host.

## Прогон B: лёгкая нагрузка Docker

Тот же тест и LONG_SLEEP=20, новый tmp_path. Два параллельных процесса опроса:
`docker ps --format '{{.ID}}'`, задержка 0.75 с после каждой команды,
таймаут команды 10 с. Вывод списка контейнеров отбрасывается. По завершении
теста нагрузка автоматически останавливается; её время и ошибки фиксируются.
Это общий Docker Engine, но операции только чтения; контейнеры личного host
не останавливаются и не перенастраиваются.

```text
LONG result after 417.0 s; calls 15 state ACTIVE
LONG progress reply_ready None
1 passed in 454.61s (0:07:34)
```

Потеря lease **не воспроизвелась**. 33 Slow job lease renewal: минимум 2.14 с,
максимум 4.05 с, все attempt 1. Нет Slow runtime tick step, Job admission lost,
Resource lease cancelled, Run faulted, Event loop blocked.

Нагрузка завершилась вместе с тестом: 456.39 с, 451 и 450 опросов, ошибок 0.
Средняя длительность docker ps в обоих workers 0.257 с, максимумы 0.447/0.451 с.
Более быстрый второй ход нельзя трактовать как ускорение от нагрузки:
прогоны не контролируют прогрев, память и все внешние процессы.

В прогоне A дополнительно прочитаны только timestamps синтетического Job:
91 различный срок lease, максимальный интервал updated_at — 7.002 с;
минимум previous lease expires_at минус следующий updated_at — 10.766 с.
Номинальный expires_at минус собственный updated_at — 17.769–20.000 с.
Это времена записей, а не отдельные измерения момента SQLite commit; их нельзя
выдавать за точный остаток аренды после завершения всей renew.

## Окружение: проверенные свойства и ограничения

Только размеры файлов личного host, без открытия его БД:

| Файл | Байты |
| --- | ---: |
| platform.sqlite | 22 704 128 |
| platform.sqlite-wal | 4 206 552 |
| platform.sqlite-shm | 32 768 |

Windows: физическая память 16 088 МиБ; свободно при замерах 2 169–2 336 МиБ.
Pagefile: выделено 15 360 МиБ, используется 2 919 МиБ. Это снимки ресурсов,
не доказательство причины отмены. Docker ps: 0.212 / 0.211 / 0.233 с;
docker image inspect указанного тестового образа: 0.209 / 0.210 / 0.234 с.
Индивидуальная команда Docker при этом замере не приближалась к 20 секундам.

Get-MpPreference возвращает общую ошибку; исключения Defender проверить не удалось.
WinDefend — Stopped, процесс MsMpEng при проверке не найден. Перечень сторонних
антивирусов через SecurityCenter2 не получен. Нельзя утверждать ни наличие
исключения, ни отсутствие любого сканирующего ПО. Настройки защиты не менялись.

## Вывод и передача Claude

На небольшом синтетическом Windows-host два длинных хода завершились. Задержки всего
JobOwner.renew на 2–4 с наблюдаются, но диагностика не разделяет стоимость его
SQLite/допуска/чтений. Приписывать их только записи SQLite или Defender нельзя.
Задержка более 20 с не наблюдалась. Масштаб личного host этим тестом не повторён.
Платформенный код, лимиты, STOP, настройки личного host и Defender не изменены.
Полный suite и live-модель не запускались.

Следующие пробы, если проблема остаётся только на личном live-host:
раздельные тайминги ожидания jobs-lock, proof/refresh, SQL save/commit и post-check;
синтетический host с историей сопоставимого объёма и фоновыми циклами. Текущий
тест использует маленькую новую БД и fast(rt), а не личную историю/нагрузку.
Оператор отдельно уточнил, что сбой наблюдается с платным API. Транспорт,
stream и поведение настоящего провайдера этими двумя прогонами не проверены.
Live-проба требует отдельного допуска и ограниченного числа запросов.

После отдельного согласия оператора выполнены настоящие API-пробы:
[результаты live long turn и отдельная находка STOP](Windows-live-long-turn-lease.md).
Указанное выше отсутствие live-проверок относится к первоначальным прогонам A/B.

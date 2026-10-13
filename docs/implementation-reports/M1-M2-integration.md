# Согласование M1-04 и опубликованных M2-01–M2-03

Дата: 2026-10-13. Ветка координатора: task/m1-m2-integration.
Исходная локальная main: a6e796b; опубликованная main сначала 094149e,
затем 899c687. Результат — обычные merge b0c2fdb и 8c1910d,
без переписывания истории и без force push.

## Что согласовано

- Сохранены все коммиты M1-04 и второго агента M2-01/02/03.
  Конфликты были только в README и docs/24/25/27; исходники объединились автоматически.
- Сохранены назначения и ограничения обеих сторон. M1-04 dependency для M2-04
  закрыта; M1-06 ещё требуется. Следующий порядок этого исполнителя по поручению
  оператора: M1-06 → публикация → M1-05.
- Единственный Alembic head — 0003 (jobs). Старое предложение использовать
  0003 для persona drafts исправлено в текущем каталоге; следующий номер
  назначает координатор. Исторические task reports исполнителей не переписаны.
- Contracts, manifests/lock, Compose и схемы M2 не менялись.
  Bootstrap остаётся STOPPED; runtime-композиция persona/memory/social не добавлялась.

## Найденная и исправленная несовместимость

На Windows существующий test_symlinks_are_not_followed падал: O_NOFOLLOW
отсутствует, и ProfileFiles.read открывал цель ссылки вне каталога профилей.
По отдельному поручению оператора на исправления совместимости добавлен
Windows-путь CreateFileW(FILE_FLAG_OPEN_REPARSE_POINT) + проверка атрибутов
того же открытого handle; reparse point отвергается, ownership handle
переходит в fd только после проверки. Отказы освобождают handle.
Проверка по pathname перед обычным open не использована из-за гонки.

Linux использует O_NOFOLLOW/O_NONBLOCK; платформенные импорты и типизация
Windows изолированы через sys.platform. На других POSIX без O_NOFOLLOW —
отказ, без небезопасного fallback.
Основание: [CreateFileW](https://learn.microsoft.com/en-us/windows/win32/api/fileapi/nf-fileapi-createfilew),
[reparse points](https://learn.microsoft.com/en-us/windows/win32/fileio/reparse-points-and-file-operations).

## Воспроизводимость общей проверки

Добавлены scripts/check_combined.py и tests/integration/README.md.
Runner использует текущий interpreter и исходники текущего checkout;
venv между Windows и Linux не переносится. Совпадение test_recall.py
в M0/M2 устранено для сборки suite через pytest importlib и явные helper paths.
Runner не устанавливает зависимости и не читает .env.

Первоначальная сборка pytest в prepend-режиме была неуспешна из-за совпавших
имён, следующая importlib-проба выявила отсутствующие helper paths.
Они заданы явно. До правки Windows: 381 passed, 1 failed, 5 skipped.
Первый Linux static check новой правки обнаружил Windows-only ctypes symbols;
платформенный guard исправлен, ошибки не подавлялись.

Финальные проверки на одних исходниках:
- Windows: **382 passed, 5 skipped**, 47.50 s.
  Пропуски: четыре real Redis cases (fixture host не задан), FIFO недоступен.
  Существующий реальный symlink test прошёл.
- Linux: **383 passed, 4 skipped**, 41.62 s.
  Пропуски только real Redis cases; symlink/FIFO прошли.
  Cached check image sha256:a35928bd28aab333eed5a98e165ff8402f3b6c7f7fcba776c0f1e459f8cd0d97,
  nonroot UID 10001, network none, readonly root, 2 CPU / 1536 MiB,
  tmpfs с отдельными synthetic SQLite, exit 0 / OOM false / ports {}.
- Обе платформы: ruff, strict mypy (65 source files), import boundaries,
  contract export --check прошли.
- Diff/check и просмотр на секреты/личные пути выполнены перед commit.
  Секреты, рабочие профили/диалоги, .env и чужие volumes не читались/не изменялись.

## Ограничения и дальнейшая совместная работа

Real Redis/restart/AOF этой интеграционной пробой не повторялись:
это отдельно проверено M1-04, её исходники и соответствующие тесты не изменились.
Никаких реальных LLM/API/игровых/публичных эффектов не выполнялось.
Linux-проба — контейнер на текущем хосте, а не venv/операционная система VPS.
Точная совместимость VPS требует запуска runner там после подготовки venv.

M2 runtime wiring, реальный embedder, storage TTL cleanup и запросы social
остаются по назначенным задачам/владельцам. Не выдавать модульные проверки
за завершённый работающий текстовый MVP.

Перед новой задачей брать актуальную origin/main в свой чистый checkout.
Готовые feature branches публиковать отдельно; main интегрировать последовательно.
Перед push main выполнять fetch и проверять ancestry; non-fast-forward —
повод включить новые коммиты и повторить затронутые проверки, не force push.

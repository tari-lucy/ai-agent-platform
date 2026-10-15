# Integration-local-host-review — совместная приёмка и native Windows

Дата: 2026-10-15. Координатор Codex. Base main 469cda3 (ручные development
controls), объединена ветка второго агента codex/integration-local-host d216ee8
в отдельной codex/integration-host-review. Поручение оператора — свести стыки и
подготовить пробный запуск, сохраняя связные этапы в GitHub.

## Что принято и исправлено

Явные production init/serve и optional panel-dir приняты из ветки второго агента;
это реальные owners/SQLite/HTTP, без production imports из tests. Только loopback,
явный mock, STOPPED после restart, недоступный live, отдельный новый synthetic
root. Serve не мигрирует, не расширяет grants и не меняет ACL.

Координатор исправил Windows stdout: вывод полного локального пути с кириллицей
ломал UTF-8 reader subprocess и раскрывал имя пользователя. Теперь выводится
нейтральное <root>/secrets/operator-password.txt. Добавлены native Windows проверки
секретного файла: protected DACL, владелец по SID и отсутствие allow для других
пользователей, без вывода identities/path/ACL. Проверяются hash, secrets и host
root; reparse points закрыты. Init назначает владельца новых путей: у повышенного
Windows token владельцем иначе могла стать группа Administrators. Штатный Windows
PowerShell вызывается скрыто с очищенным только дочерним PSModulePath (pwsh 7
может передать несовместимый module path). Ошибка/timeout закрывает чтение.

Неверные serve port 0/-1/>65535 отклоняются до открытия/слушания с фиксированным
INVALID_OPTION. Статический handler отвергает также скрытые каталоги на путях
без расширения: /.git/config не получает SPA index. Данные и секреты не входят
в каталог сборки; прежние traversal/symlink/API ограничения сохранены.

## Доказательства

- Свежий отдельный Windows venv: uv 0.12.23, Python 3.14.8, uv sync --locked,
  exit 0; никаких изменений lock/dependencies или чужого venv. Сначала offline
  не нашёл часть wheel cache; затем получены только зафиксированные Python
  dependencies. Модели/платные API/внешние каналы не использовались.
- Native Windows tests/local_host: 14 passed, 1 POSIX skip, 66.67 s, exit 0.
  Включены реальный subprocess init/serve, mock final/ACK, TEST lab/report,
  STOP/restart STOPPED и отказ при расширении file/parent Windows DACL.
- После отдельных port/static fixes повторены только затронутые cases:
  4 passed, 12.06 s, exit 0. Всего 17 Windows host cases проверены;
  Linux-only отметки на переносимых smoke убраны, POSIX проверка осталась отдельно.
- Настоящий Edge против production subprocess host + собранной панели:
  login → STOPPED → start → private mock final → explicit ACK → STOP →
  новый процесс → новый login → STOPPED + persisted диалог: 1 passed, 15.66 s.
  API/WS не перехватывались. Это отдельно от synthetic ASGI development fixture.
- TypeScript/build прошли; UI bundle имеет прежнее предупреждение >500 KB.
  Ruff/mypy (186 source files), import boundaries, canonical exports --check:
  exit 0. Новый tests/local_host добавлен в общий check_combined gate/HELPERS.
- Linux combined на общей версии: tests/local_host + 14 development controls
  cases — 30 passed, 2 Windows ACL skips, 184.43 s, exit 0. Network=none,
  cached locked dependencies, 2 CPU/1536 MiB; sources read-only. Для production
  subprocess -I создан собственный временный venv без pip и без копирования
  окружения, source/cache paths заданы через его .pth. Отдельный assertion
  подтвердил импорт core из /workspace, а не старого cached installed package.
  Первый запуск harness отказал из-за noexec /tmp с копией interpreter; повтор
  использовал стандартный symlink venv, права tmpfs не расширялись.
- Перед commit: diff --check, privacy scan новых/изменённых текстовых файлов,
  ignored venv/node_modules/dist; никаких значений локальной конфигурации в Git.

## Границы и следующие необходимые стыки

Не готов весь MVP и не включён live. Профиль пользователя/его качество, настоящие
агрегаторы/лимиты/расходы, роли extract/consolidate/gm и полный набор UI действий
этой приёмкой не проверяются. WS browser path используется, но отдельная новая
матрица WS failure cases не прогонялась. Compose по-прежнему scaffold: 127.0.0.1
в контейнере нельзя публиковать обычным port mapping как готовый host.

Minimal rights второго агента сознательно относятся к прежнему mock smoke:
WORKSPACE запрещён write после init, а оператор не имеет WORKSPACE API read;
PERSONA/GM не читают MEMORY. Поэтому development owner plans/receipts и settings
transfer fail closed. До их host приёмки нужен явный отдельный provisioning mode
или согласованная минимальная матрица. Не выдавать расширение внутри serve и не
представлять успешный fixture тест как доказательство production provisioning.

Permission остаются срочными, команды продления нет. Native Windows DACL пробы
относятся к новым synthetic каталогам и секретам; чужие существующие файлы/данные
не менялись. Live fences сохранены; снять только mock comparison недостаточно
без role binding/permission/epoch/cancel/usage/budget проверки. Реальный ключ и
endpoint не запрашивались и .env не читался.

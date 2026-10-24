# Родной OpenCode Web поверх bridge `aw-opencode/1`

Статус: **кандидат проверен в Edge с синтетической моделью; личный host не менялся.**
Исправления и оставшиеся проверки: [отчёт Codex](../../docs/implementation-reports/OC-03-render-diagnosis.md).
История исследования: [заметки Claude](../../docs/implementation-reports/OC-03-native-ui-notes.md).
Эта клиентская проверка не закрывает весь OC-03/OC-06 и не является разрешением
автоматически включить сборку на личном host.

## Состав

- `patches/` — серия патчей к upstream OpenCode **v1.19.3** (`e3c4eb2`), `git format-patch`.
  Новый файл адаптера: `packages/app/src/aw/bridge.ts` (подставлен как `platform.fetch`).
  Применять все **двенадцать** патчей по порядку. Третий исправляет загрузку отдельного
  сообщения, гонку проекции ответов и ACK, убирает временные debug-хуки.
  Четвёртый не позволяет кнопке отправки опережать загрузку личности/модели.
  Пятый (Claude): уведомления платформы о ходе на месте ответа (ждёт START, ошибка,
  отмена, неизвестный исход, сообщение недоступно; очередь/выполнение — родной busy),
  без дубля только что отправленного сообщения (id клиента на время страницы),
  без git-сведений и ключа `aw_…` в подписях личности.
  Шестой (Claude): нумерованные ответы на одно сообщение (продолжение после ответа на
  обращение не затирает прежний ответ), уведомление об остановленном разговоре без истории,
  одна безопасная повторная отправка при конфликте версии (без дубля сообщения).
  Седьмой (Claude): уведомление «агент ждёт вашего ответа в обращении» со ссылкой на точную
  карточку панели (`#/tickets?ticket=<id>`) по полю `open_ticket_ids` расшифровки.
  Восьмой (Claude): текст ожидания верен для любой личности.
  Девятый (Claude): TS2741 — `awFetch` и заглушка `fetch` в его тесте получают no-op
  `preconnect`, который под типами Bun требует `Platform.fetch: typeof fetch`; upstream-тип
  не ослаблен, приведений нет, поведение адаптера не меняется (`bun run typecheck` чистый).
  Десятый (Codex): вход без сессии ведёт на чистый `/`, без запрещённого `?next=`.
  После входа кнопка панели открывает OpenCode; серверный запрет query не ослаблен.
  Одиннадцатый (Codex): номера сообщений и уведомлений включают id разговора,
  чтобы общий native-кэш одной личности не подставлял части другого разговора.
  Двенадцатый (Codex): сохранённые даты разговоров вместо текущего времени при refetch;
  переименование через владельца сессий (без модели и расширения прав).
- `dev/` — отладочный стенд Claude (Linux): `ocbuild.sh` (сборка → удаление медиа/map →
  упаковка), `ocstand.sh` + `stand.py` (production host in-process с синтетической моделью,
  панель + `/opencode/`, Playwright: вход, действия, журнал запросов адаптера). Пути в скриптах
  указывают на scratchpad Claude — при запуске на другой машине поправить.
  Это исторические отладочные скрипты, не универсальный launcher.
  `bridge.ts.snapshot` — копия итогового адаптера для быстрого чтения.

## Воспроизвести сборку

```sh
git clone --config core.autocrlf=false --depth 1 --branch v1.19.3 https://github.com/anomalyco/opencode.git opencode-src
cd opencode-src && git checkout -b aw-bridge
git am <этот-репозиторий>/deploy/opencode-ui/patches/*.patch
# bun 1.3.14 (как в package.json upstream)
bun install --filter '@opencode-ai/app' --ignore-scripts --frozen-lockfile
cd packages/app
OPENCODE_CHANNEL=prod bun run node_modules/vite/bin/vite.js build --base /opencode/ --outDir <новый-абсолютный-dist>
# убрать не допускаемые поставкой файлы
find <абсолютный-dist> -type f \( -name '*.aac' -o -name '*.mp4' -o -name '_headers' -o -name '*.map' \) -delete
python scripts/package_opencode_ui.py --build-dir <абсолютный-dist>   # из этого репозитория
```

Точный upstream: `e3c4eb242585dcd77be1a867391b7c25fd47ce52`. Используйте отдельный
каталог исходников и новый каталог сборки вне host. Не меняйте upstream lockfile.
Для старого Windows checkout нормализуйте CRLF патчей в LF во временной копии перед
`git am`; новые checkout учитывают локальный `.gitattributes`.

На PowerShell задайте `$env:OPENCODE_CHANNEL='prod'`, затем выполните ту же команду
Bun. Вместо `find ... -delete` используйте блок ниже; он проверяет границы пути
каждого удаляемого файла. `$output` — новый каталог только что выполненной сборки,
`$python` — Python окружения платформы:

```powershell
@'
import sys
from pathlib import Path
root = Path(sys.argv[1]).resolve(strict=True)
for path in root.rglob('*'):
    if path.is_file() and (path.suffix in {'.aac', '.mp4', '.map'} or path.name == '_headers'):
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise SystemExit('Build contains an unsafe path')
        path.unlink()
'@ | & $python - $output
if ($LASTEXITCODE) { throw 'Asset cleanup failed' }
& $python scripts/package_opencode_ui.py --build-dir $output
if ($LASTEXITCODE) { throw 'Bundle validation failed' }
```

Допуск поставки в main: **1024 файла / 48 МиБ**. Проверенный кандидат: **938 файлов /
34 350 700 байт** без manifest. Упаковка создаёт `platform-ui-manifest.json` с размерами
и SHA-256. `dist`, зависимости и временные host в Git не входят.

## Проверки без платных вызовов

Из upstream `packages/app`:

```sh
bun test --conditions=solid --preload ./happydom.ts ./src/aw/bridge.test.ts ./src/context/aw-projection-race.test.ts ./src/context/server-session.test.ts
```

Из платформы, с проектным `PYTHONPATH` (SOURCES и HELPERS из `scripts/check_combined.py`,
дополнительно `tests/panel`) и собранной панелью:

```powershell
$env:AW_NATIVE_UI_DIR = 'C:\absolute\opencode-dist'
$env:AW_PANEL_DIST = 'C:\absolute\panel\dist'
$env:AI_PANEL_BROWSER_CHANNEL = 'msedge'
python -m pytest --import-mode=importlib tests/panel/test_native_opencode_sessions.py -q
```

Тест opt-in: без `AW_NATIVE_UI_DIR` пропускается. Проверяет ввод в родном OpenCode,
STOPPED → START → ответ → ACK → reload → STOP; скрытый/обрезанный ответ не
подтверждается, повторного вызова модели нет. Использует настоящий host/owners и
синтетический HTTP-провайдер.

Без дополнительных параметров harness синтетический. Для настоящего Docker/OpenCode
задайте `AW_OPENCODE_IMAGE_ID=sha256:<проверенный-ID-уже-установленного-образа>`:
обычный serve подключит изолированный runner; тест выдаст доступ через настоящую
форму обращения на своём временном host. Образ не скачивается, личные данные и права
не используются, платных вызовов нет. Этот режим требует готового Docker Desktop.

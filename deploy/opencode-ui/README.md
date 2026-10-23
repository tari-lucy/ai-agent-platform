# OC-03 WIP: родной OpenCode Web поверх bridge `aw-opencode/1`

Статус: **незавершённая работа, не интегрировать в main и не включать на личном host.**
Подробности, найденные стыки и открытые проблемы: [заметки](../../docs/implementation-reports/OC-03-native-ui-notes.md).

## Состав

- `patches/` — серия патчей к upstream OpenCode **v1.19.3** (`e3c4eb2`), `git format-patch`.
  Новый файл адаптера: `packages/app/src/aw/bridge.ts` (подставлен как `platform.fetch`).
  В патче временные отладочные хуки с пометкой `AW-DEBUG` — убрать перед сдачей.
- `dev/` — отладочный стенд Claude (Linux): `ocbuild.sh` (сборка → удаление медиа/map →
  упаковка), `ocstand.sh` + `stand.py` (production host in-process с синтетической моделью,
  панель + `/opencode/`, Playwright: вход, действия, журнал запросов адаптера). Пути в скриптах
  указывают на scratchpad Claude — при запуске на другой машине поправить.
  `bridge.ts.snapshot` — копия адаптера для быстрого чтения.

## Воспроизвести сборку

```sh
git clone --depth 1 --branch v1.19.3 https://github.com/anomalyco/opencode.git opencode-src
cd opencode-src && git checkout -b aw-bridge
git am <этот-репозиторий>/deploy/opencode-ui/patches/*.patch
# bun 1.3.14 (как в package.json upstream)
bun install --filter '@opencode-ai/app' --ignore-scripts
cd packages/app
OPENCODE_CHANNEL=prod bun x vite build --base /opencode/ --outDir <абсолютный-dist>
# убрать не допускаемые поставкой файлы
find <абсолютный-dist> -type f \( -name '*.aac' -o -name '*.mp4' -o -name '_headers' -o -name '*.map' \) -delete
python scripts/package_opencode_ui.py --build-dir <абсолютный-dist>   # из этого репозитория
```

Сейчас сборка ~924 файла / ~35 МиБ: больше текущего валидатора (512 / 32 МиБ). Согласовано
поднять до 1024 / 48 МиБ (Codex); локально для отладки лимиты подняты вручную, не в коммите.

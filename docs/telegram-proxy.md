# Telegram через закрытый SSH-прокси

Текст поста: OpenCode → MCP платформы → проверка публичного текста → Telegram.
Сетевой маршрут: платформа → SOCKS5 на `127.0.0.1` Windows → SSH → VPS →
`api.telegram.org:443`. TLS проверяется от платформы до Telegram; токен бота
остаётся в локальном `.env`. В контейнер и на VPS он не передаётся.

## Подготовка VPS

Новые пакеты не нужны: используется установленный OpenSSH. Кандидат Tinyproxy
из Ubuntu 24.04 — `1.11.1-3ubuntu0.1`; для CVE-2026-31842 у Ubuntu пока
`Needs evaluation`. Поэтому этот пакет не устанавливаем.

Администратор один раз запускает `deploy/telegram/install-ssh-proxy.sh <public-key>`.
Скрипт создаёт отдельного системного пользователя без shell и парольного входа;
разрешает только локальный TCP-forward к `api.telegram.org:443`. Remote-forward,
Unix-сокеты, agent-forwarding, TTY и X11 запрещены. Ключ и конфиг принадлежат root.
Проверяется синтаксис sshd и неизменность эффективной конфигурации пользователя
оператора; затем только reload SSH, без restart. n8n, Docker, firewall и веб-порты
не меняются. Существующие отличающиеся файлы скрипт не перезаписывает.

## Docker: основной запуск туннеля

Контейнер — общий сервис платформы, отдельно от контейнеров личностей. Его
автоперезапуск не запускает агентов; STOP продолжает запрещать публикации.
На VPS изменений не требуется: используются уже выданный узкий ключ и аккаунт.

Сборка (контекст содержит только Dockerfile и entrypoint, без ключей):

```powershell
docker build -t ai-agent-platform:telegram-tunnel deploy/telegram/tunnel
docker image inspect ai-agent-platform:telegram-tunnel --format '{{.Id}}'
```

В отдельном локальном `config/local.telegram-proxy.docker.env`:

```dotenv
AI_TG_IMAGE=sha256:<ID собранного образа>
AI_TG_VPS_HOST=<адрес VPS>
AI_TG_LOCAL_PORT=10880
AI_TG_IDENTITY=C:/absolute/private-key
AI_TG_KNOWN_HOSTS=C:/absolute/known_hosts
```

known_hosts содержит только заранее проверенный ключ нужного VPS. Не сканировать
и не принимать новый host key автоматически. Приватный ключ read-only, в образ
не копируется; рабочая копия с правами 0600 живёт только в tmpfs. Контейнер работает
как UID 65532 без capabilities; rootfs read-only, сеть отдельная, Docker socket
и папки агентов не монтируются. На хосте опубликован только loopback-порт.

```powershell
docker compose --env-file C:/absolute/local.telegram-proxy.docker.env -f deploy/telegram/tunnel/compose.yaml up -d --wait
```

Используется `unless-stopped`: после старта Docker контейнер восстанавливается,
если его не остановили вручную. После сна/обрыва SSH переподключается. До входа
пользователя и запуска Docker Desktop это не гарантирует готовность. Прежняя
задача Windows после переключения отключена, сохранена для отката.

## Windows: резервный запуск без Docker

Отдельный ключ хранится у пользователя Windows, вне Git и контейнеров. Локальный
`config/local.telegram-proxy.json` содержит `host`, `user=aw-telegram-proxy`,
`identity` (абсолютный путь) и свободный `port`. Запуск:

```powershell
powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File scripts/start_telegram_proxy.ps1 -Config C:\absolute\local.telegram-proxy.json
```

Для постоянной работы — отдельная задача Windows при входе пользователя. Скрипт
держит SSH keepalive и после разрыва повторяет соединение через 15 секунд.
При выключенном компьютере прокси не работает. Проверка host key обязательна;
неизвестный/изменившийся ключ автоматически не принимается.

В локальном `.env`:

```dotenv
AI_PLATFORM_TELEGRAM_PROXY=socks5://127.0.0.1:10880
```

Порт должен совпасть с локальным конфигом. Этот параметр влияет только на
Telegram, не на модель, n8n или интернет агента. При отказе прокси прямого обхода
нет; публикация автоматически не повторяется. Если подтверждение отправки
потеряно, `TELEGRAM_OUTCOME_UNKNOWN` означает: сначала проверить канал, чтобы
не создать дубль. Общая доступность Telegram не доказывает успешную публикацию.

При остановленном сервере: `configure-harness --root <host> --channel '@<channel>'`,
затем обычный serve. Агент остаётся STOPPED до явного START. Инструмент —
`platform_channel_post`. Привязка канала и прокси не отменяют модерацию и STOP.
Отправка уже принятого Telegram сообщения не может быть отменена последующим STOP.

## Отключение и откат

Для Docker — `docker compose ... down` только отдельного проекта туннеля. Для
возврата к Windows-задаче сначала освободить её loopback-порт остановкой контейнера,
затем включить и запустить только `AI Agent Platform - Telegram Proxy`.
Для резервного режима отключить выделенную задачу и завершить только её SSH-процесс. Удалить
`AI_PLATFORM_TELEGRAM_PROXY` для возврата к прямому соединению; `--remove-channel`
отключает публикации совсем. Требуется перезапуск платформы при STOP.
На VPS можно убрать только `80-aw-telegram-proxy.conf` и ключ выделенного
пользователя, затем `sshd -t` и reload SSH. Не удалять существующих пользователей,
конфигурацию SSH, Docker-сети, тома или данные n8n.

Источники: [OpenSSH](https://man.openbsd.org/ssh#D),
[CVE-2026-31842 Ubuntu](https://ubuntu.com/security/CVE-2026-31842),
[SOCKS в HTTPCore](https://www.encode.io/httpcore/proxies/).

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

## Windows

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

Отключить выделенную задачу Windows и завершить только её SSH-процесс. Удалить
`AI_PLATFORM_TELEGRAM_PROXY` для возврата к прямому соединению; `--remove-channel`
отключает публикации совсем. Требуется перезапуск платформы при STOP.
На VPS можно убрать только `80-aw-telegram-proxy.conf` и ключ выделенного
пользователя, затем `sshd -t` и reload SSH. Не удалять существующих пользователей,
конфигурацию SSH, Docker-сети, тома или данные n8n.

Источники: [OpenSSH](https://man.openbsd.org/ssh#D),
[CVE-2026-31842 Ubuntu](https://ubuntu.com/security/CVE-2026-31842),
[SOCKS в HTTPCore](https://www.encode.io/httpcore/proxies/).

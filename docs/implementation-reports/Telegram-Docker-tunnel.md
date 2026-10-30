# Telegram: перенос SSH-туннеля в Docker

Дата: 2026-10-30. Продолжение [закрытого прокси](Telegram-private-proxy.md) по
прямому поручению оператора. VPS, n8n, SSH-сервер и выделенный пользователь
прокси не перенастраиваются. Меняется только клиент туннеля на Windows.

## Поставка

Отдельный Compose-проект `deploy/telegram/tunnel/compose.yaml` со своим network.
Контейнер инфраструктуры общий для агентов платформы; токены ботов и правила
публикаций остаются у платформы. Персонаж не получает ключ или управление Docker.
Локальный `.env` проекта Compose содержит только параметры соединения, пути к
выделенному ключу/known_hosts и закреплённый ID образа; не bot-token и не `.env`
личного host. Ключ монтируется read-only, его копия 0600 хранится только в tmpfs.

Образ: официальный Alpine 3.24.3, digest закреплён в Dockerfile; ветка main
поддерживается до 2028-06-01. Установлен openssh-client 10.3_p1-r1. Новый образ
около 5,4 МБ. UID/GID 65532, read-only root, cap-drop ALL, no-new-privileges,
64 МиБ RAM, 0,25 CPU, 32 PID, ограниченные журналы. Нет Docker socket, папок
агента, токена Telegram или ключа LLM. Публикация только на 127.0.0.1:10880.

`unless-stopped` восстанавливает контейнер при запуске Docker, но сохраняет
ручную остановку. SSH keepalive/повтор внутри служебного контейнера восстанавливает
сеть. Ни контейнер, ни туннель не запускают личность: START остаётся явным.
Healthcheck проверяет SSH master и локальный SOCKS listener; это не обещание
постоянной доступности Telegram. При выключенном компьютере публикации невозможны.

## Проверено и ограничения

Сначала отдельный проект/порт 10881 при сохранённом Windows-туннеле: настоящий
HTTPS Telegram с проверкой TLS, отказ example.com, UID и права 0600, restart
своего контейнера. Проба сразу после restart попала до завершения SSH handshake;
после ожидания готовности HTTPS прошёл. Healthcheck уточнён до SSH master,
финальный кандидат принят через `compose up --wait`.

После успешного кандидата собственная задача Windows отключается; при неудаче
cutover предусмотрен её возврат. Результаты окончательной проверки — ниже.
Production-контейнер на 10880 — healthy. Через него getMe/getChat/getChatMember
подтвердили бота, нужный канал и право публикации. example.com и loopback VPS
отклонены. Docker inspect подтвердил non-root, read-only, restart policy,
только два read-only bind-mount и отсутствие TOKEN/LLM/PASSWORD в переменных.
Задача Windows — Disabled. Расход в коротком idle-замере: около 2 МиБ RAM,
0,00% CPU. Временный проверочный контейнер и только его сеть удалены.
Реальный reboot/сон компьютера и сутки работы не проверялись; Docker Desktop
или VPS целиком не перезапускались. Публикаций и платных вызовов нет.

Источники: [Alpine releases](https://alpinelinux.org/releases/),
[Docker restart policy](https://docs.docker.com/engine/containers/start-containers-automatically/),
[loopback publishing](https://docs.docker.com/engine/network/port-publishing/).

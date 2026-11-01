# Панель по своему домену (srv-213)

Панель платформы слушает только `127.0.0.1` сервера (так задумано). Снаружи её показывает
nginx-proxy-manager (NPM) по HTTPS; между ними маленький мост и явный публичный адрес.

```
браузер --https--> NPM (80/443) --http--> 172.18.0.1:8765 (мост) --> 127.0.0.1:8765 (панель)
```

## Что включено в платформе
`serve --public-origin https://<имя>`: ровно один адрес `https`, DNS-имя без порта и пути.
Host и Origin должны совпасть с ним точно (или с loopback), cookie получает `Secure`,
маршруты `/internal/` (рабочие процессы) по публичному имени закрыты. Заголовки
`X-Forwarded-*` ничего не расширяют. Без флага поведение прежнее (только loopback).

## Мост
`deploy/panel-domain/ai-panel-relay.service` (пользовательский systemd): слушает
`172.18.0.1:8765` (шлюз сети compose `n8n-setup_default`), источник только `172.18.0.0/16`.
Правило файрвола для этого интерфейса, один раз, с `sudo`:

    sudo ufw allow in on br-<id сети> to 172.18.0.1 port 8765 proto tcp

(`docker network inspect n8n-setup_default` → Id, имя моста `br-` и первые 12 знаков.)

## NPM (в браузере, через SSH-туннель к `127.0.0.1:81`)
1. Proxy Host: домен `agents.tarigpt.ru`, схема `http`, адрес `172.18.0.1`, порт `8765`,
   Websockets Support включить, Block Common Exploits включить.
2. SSL: Request a new certificate (Let's Encrypt), Force SSL, HTTP/2. HSTS после проверки.
3. Access List (рекомендуется): логин и пароль на входе, «Pass Auth to Host» выключено
   (панель отвергает запросы с заголовком Authorization). По желанию список IP.

## Откат
Убрать `--public-origin` из `ExecStart` службы и перезапустить; в NPM удалить Proxy Host;
`systemctl --user disable --now ai-panel-relay`.

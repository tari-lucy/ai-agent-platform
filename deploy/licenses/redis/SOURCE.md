# Redis 8.10.2 license inventory

Используется отдельный неизменённый official image Redis 8.10.2-trixie.
Выбран вариант AGPLv3 из upstream tri-license (LICENSE.txt); собственный
код AI Agent Platform сохраняет MIT. Файлы LICENSE.txt/REDISCONTRIBUTIONS.txt
получены без изменения из patch 8.10.2. Не переобъявлять Redis MIT.

Image index digest: sha256:2c2dff791878316e3b083188be0d578423b24813cdda5de0e80ea4f7b3e6bfd6
Linux/amd64 manifest: sha256:6e9101032a2126aeeb4b1e862ac77d2fcf26b831ae37e5ff2e69169f82ddd411

Sources:
- https://redis.io/legal/licenses/
- https://github.com/redis/redis/tree/8.10.2
- https://github.com/redis/docker-library-redis/tree/8104e63b5910fda751bf7c038fa297b6fb19ff43/debian

Image содержит Redis modules, но platform.conf их не загружает; используются
core Redis Streams. Upstream image/OS/third-party components имеют собственные
notices. Эта инвентаризация не заменяет полный release SBOM/notice/source bundle
при будущей распространённой поставке; сейчас image используется локально.

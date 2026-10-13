# Лицензии зависимостей

MIT в LICENSE относится только к собственному коду AI Agent Platform.
Точные Python/npm версии и hashes фиксируются в uv.lock и apps/panel/package-lock.json.
Лицензии компонентов сохраняются в устанавливаемых distributions/npm packages;
их полные тексты и notices необходимо включить в будущую распространяемую поставку.
Этот файл — инвентарь, не замена текстов лицензий и не полный release SBOM.

Исходные проверенные источники: [M0-01](docs/implementation-reports/M0-01.md), раздел 5.
Основные группы: API/storage/Pydantic — MIT; Uvicorn/HTTPX — BSD-3-Clause;
FastEmbed — Apache-2.0; ONNX Runtime — MIT; SQLite — public domain;
sqlite-vec — MIT-вариант upstream; React/Vite/Mantine/Router/Query — MIT;
TypeScript/Playwright — Apache-2.0. Транзитивные компоненты имеют собственные условия.

Модели не включены и не скачиваются установкой приложения. Их фактические ONNX
артефакты, model cards, revisions и checksums проверит M0-03 отдельно.

Redis server не устанавливается M0-02. Для будущей Redis 8 поставки выбирается
вариант AGPLv3 из upstream triple licensing; digest, полный license/notice и
проверка конкретного образа принадлежат M0-04. redis-py имеет отдельную MIT-лицензию.
Это не меняет лицензию приложения и не объявляет ещё не проверенный Redis image принятым.

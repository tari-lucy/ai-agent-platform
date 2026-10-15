# Автономный приватный GM: узкая приёмка

Production local host (`initialise` с публичным профилем, `configure_gm`, `build`), настоящие
owners/HTTP и настоящий live-адаптер поверх синтетического `httpx.MockTransport`. Ни одного
реального API-вызова. Scripted-ответы доказывают маршрут и правила приложения, а не качество
предложений модели.

Запуск: `PYTHONPATH` как у `tests/memory_quality` (там же `quality_support.py`) плюс этот каталог.

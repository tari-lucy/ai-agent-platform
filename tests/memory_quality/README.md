# Качество приватной памяти

Узкая приёмка пути «сообщение → память → следующий ответ». Все данные вымышленные,
провайдер — scripted: он проверяет **маршрут и правила приложения**, но не качество
свободного ответа настоящей модели (это делает отдельный opt-in probe
`scripts/probes/private_memory_quality.py`).

- `test_relevance.py`, `test_extraction_diagnosis.py` — быстрые проверки без host.
- `test_private_pipeline.py` — настоящие owners/HTTP на временном SQLite (медленные).

Запуск: `PYTHONPATH` как у `tests/integration_dialogue` плюс `tests/extraction`,
`tests/operations`, `tests/access`, `tests/m3_runtime`, `tests/development`.

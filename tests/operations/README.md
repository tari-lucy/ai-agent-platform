# M1-06 acceptance

Общая проверка: `python scripts/check_combined.py` из корня подготовленного checkout.
Она выполняет static/contract checks и все интегрированные M1/M2 плюс operations.

Только новый набор (нужны исходники и tests/operations + tests/jobs в PYTHONPATH):
`python -m pytest tests/operations --import-mode=importlib -q`.

47 случаев: LLM/CPU/RAM reserve, foreground embedding между batch, bounded очередь/
срок/история, original admission/epoch/version/work-test, STOP/поздние receipts,
UNKNOWN без освобождения/слепого retry, новый budget для подтверждённого retry attempt,
health TTL/critical failure/возврат ready без resume, safe trace correlation,
coalescing/slow client disconnect, owner thread/token identity, closed bootstrap/config.

test_job_budget использует настоящий SQLite JobOwner и общий supervisor:
success, STOP и retry проверяют независимые resource tickets обоих владельцев.
Остальные исполнители/readiness — synthetic. Provider APIs и реальные worker IO не вызываются.

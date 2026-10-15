# tests/local_backup

Narrow acceptance of the local host backup composition. Synthetic hosts are prepared by the real
`initialise`; records are written through owner paths (MemoryService, RetentionService,
UsageLedger). Real SQLite/index/ledger files and the real CLI; no network, no paid API, no
operator data. The retention ledger is a fixture standing in for the owner binding the host does
not have yet (see docs/implementation-reports/Integration-local-backup.md).

Run with this checkout's sources first on `PYTHONPATH` (the venv's editable install may point at
another worktree).

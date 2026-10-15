# tests/local_artifacts

Derived laboratory files (recall indexes, report bodies) on a temporary host prepared by the real
`initialise`. Lab runs use the real `LabRunner`, `Reports` and `SQLiteRecallIndex`; sources are
deleted through the real `RetentionService` and an independent ledger. Synthetic markers only; no
network, no paid API, no operator data. Run with this checkout's sources first on `PYTHONPATH`
(the venv's editable install may point at another worktree).

`FileHolds` in `la_support.py` stands in for `Reports.holds`, the public port that does not exist
yet; see docs/implementation-reports/Integration-derived-artifacts.md.

"""Open the independent deletion ledger only if it already exists and is a real ledger."""

import sqlite3

from ai_agent_adapters.retention import SQLiteDeletionLedger

from ai_agent_core.local_host.layout import HostError

from .layout import BackupLayout


def open_existing_ledger(place: BackupLayout) -> SQLiteDeletionLedger:
    """Never creates a ledger: an empty or foreign file would become a fresh, empty history."""
    path = place.ledger_path
    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise HostError("LEDGER_UNAVAILABLE")
        probe = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
        try:
            tables = {
                row[0] for row in probe.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            meta = probe.execute("SELECT COUNT(*) FROM ledger_meta").fetchone()[0]
        finally:
            probe.close()
    except sqlite3.Error, OSError:
        raise HostError("LEDGER_UNAVAILABLE") from None
    if not {"deletion_ledger", "ledger_meta"} <= tables or meta != 1:
        raise HostError("LEDGER_UNAVAILABLE")
    return SQLiteDeletionLedger(path)

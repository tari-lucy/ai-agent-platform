"""Recall adapters (M2-02): derived FTS5/sqlite-vec index. Not wired into runtime."""

from .index import SQLiteRecallIndex, match_expression

__all__ = ["SQLiteRecallIndex", "match_expression"]

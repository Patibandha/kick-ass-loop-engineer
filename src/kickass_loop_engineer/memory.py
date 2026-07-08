"""SQLite-backed shared memory store for cross-run fact persistence.

Provides a simple key/value store with run tagging, kind classification,
and lifecycle management (working → canonical, or forgotten). All operations
use parameterized queries; ``sqlite3.Error`` propagates to the caller unwrapped.
"""
from __future__ import annotations

import os
import sqlite3
from typing import Optional


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS facts (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT,
    kind    TEXT,
    key     TEXT,
    value   TEXT,
    status  TEXT DEFAULT 'working'
)
"""


class Memory:
    """SQLite shared memory store for kickass_loop_engineer agents.

    All database operations use parameterized queries to prevent SQL injection.
    The connection is held open for the instance lifetime; call :meth:`close` (or
    use the instance as a context manager) to release it.
    """

    def __init__(self, db_path: str) -> None:
        """Open (or create) the SQLite database at *db_path*.

        Creates parent directories as needed. The facts table is created on first
        use if it does not already exist.

        Args:
            db_path: Absolute or relative path to the SQLite file.
        """
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute(_CREATE_TABLE)
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection (idempotent)."""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def __enter__(self) -> "Memory":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def record(self, key: str, value: str, *, run_id: str, kind: str) -> None:
        """Insert a new fact row (status defaults to 'working').

        Multiple calls with the same key are all retained; :meth:`get` returns the
        most recently inserted value.

        Args:
            key: Logical name of the fact (e.g. ``"api.contract"``).
            value: String value to store.
            run_id: Identifier of the agent run that produced this fact.
            kind: Classification tag (e.g. ``"fact"``, ``"contract"``).
        """
        self._conn.execute(
            "INSERT INTO facts (run_id, kind, key, value) VALUES (?, ?, ?, ?)",
            (run_id, kind, key, value),
        )
        self._conn.commit()

    def forget(self, key: str) -> int:
        """Delete all rows whose key matches *key*.

        Args:
            key: The fact key to remove entirely.

        Returns:
            The number of rows deleted.
        """
        cursor = self._conn.execute("DELETE FROM facts WHERE key = ?", (key,))
        self._conn.commit()
        return cursor.rowcount

    def consolidate(self, run_id: str) -> int:
        """Promote all facts for *run_id* from 'working' to 'canonical'.

        Args:
            run_id: The run whose facts should be marked canonical.

        Returns:
            The number of rows updated.
        """
        cursor = self._conn.execute(
            "UPDATE facts SET status = 'canonical' WHERE run_id = ?",
            (run_id,),
        )
        self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, key: str) -> Optional[str]:
        """Return the most recently inserted value for *key*, or None.

        Args:
            key: The fact key to look up.

        Returns:
            The value string, or ``None`` if no matching row exists.
        """
        cursor = self._conn.execute(
            "SELECT value FROM facts WHERE key = ? ORDER BY id DESC LIMIT 1",
            (key,),
        )
        row = cursor.fetchone()
        return row["value"] if row is not None else None

    def query(self, *, kind: Optional[str] = None, status: Optional[str] = None) -> list:
        """Return rows optionally filtered by *kind* and/or *status*.

        Builds a parameterized WHERE clause from whichever filters are provided.
        Results are ordered by insertion id ascending.

        Args:
            kind: If given, only rows with this kind are returned.
            status: If given, only rows with this status are returned.

        Returns:
            A list of dicts with keys: id, run_id, kind, key, value, status.
        """
        conditions = []
        params = []
        if kind is not None:
            conditions.append("kind = ?")
            params.append(kind)
        if status is not None:
            conditions.append("status = ?")
            params.append(status)

        sql = "SELECT id, run_id, kind, key, value, status FROM facts"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY id ASC"

        cursor = self._conn.execute(sql, params)
        return [dict(row) for row in cursor.fetchall()]

"""Bounded sqlite spool for the langfuse-fleet plugin (stdlib only).

Durability boundary of the fleet transport: producers pay one fast insert and
never touch the network; a daemon worker claims due rows, delivers them and
only spool transitions driven by verified server acceptance move an event out
of ``pending``. Design: `.specs/langfuse-f1-recuperacion-y-diseno/02-design.md`.
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any

# Policy limits (02-design.md §3). Tests override these module globals.
MAX_ROWS = 4096
MAX_PENDING_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 512 * 1024
MAX_ATTEMPTS = 5
RETRY_BASE_S = 5.0
RETRY_CAP_S = 900.0
EXPIRY_AGE_S = 24 * 3600.0
RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

_COUNTER_KEYS = ("delivered", "lost_overflow", "lost_oversize", "lost_attempts",
                 "expired", "recovered", "batches_acked", "rejected")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id              TEXT PRIMARY KEY,
  type            TEXT NOT NULL,
  body_json       TEXT NOT NULL,
  bytes           INTEGER NOT NULL,
  created_at      REAL NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at REAL NOT NULL DEFAULT 0,
  state           TEXT NOT NULL DEFAULT 'pending',
  owner           TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_due ON events(state, next_attempt_at);
CREATE TABLE IF NOT EXISTS counters (
  key   TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);
"""


def _boot_id() -> str:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            return handle.read().strip()[:36]
    except OSError:
        return uuid.uuid4().hex[:12]


class FleetSpool:
    """One sqlite file per Hermes home; safe for concurrent writers (WAL + busy_timeout)."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self._path), timeout=1.0, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=1000")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self.recover()

    # -- producers ----------------------------------------------------------

    def enqueue(self, event: dict[str, Any], size: int, created_at: float | None = None) -> bool:
        """Persist one allowlisted envelope. False = rejected (counted, never blocking)."""
        with self._lock:
            if size > MAX_BATCH_BYTES:
                self._bump("lost_oversize")
                return False
            rows, bytes_pending = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM events WHERE state IN ('pending', 'in_flight')"
            ).fetchone()
            if rows >= MAX_ROWS or bytes_pending + size > MAX_PENDING_BYTES:
                self._bump("lost_overflow")
                return False
            self._conn.execute(
                "INSERT INTO events (id, type, body_json, bytes, created_at) VALUES (?, ?, ?, ?, ?)",
                (event["id"], event["type"],
                 json.dumps(event, separators=(",", ":"), ensure_ascii=True),
                 size, created_at if created_at is not None else time.time()),
            )
            self._conn.commit()
            return True

    # -- delivery worker ----------------------------------------------------

    def claim_batch(self, limit: int = 128, max_bytes: int = MAX_BATCH_BYTES,
                    now: float | None = None, owner: str | None = None,
                    dry_run: bool = False) -> list[dict[str, Any]]:
        """Due pending rows → ``in_flight`` under this lease. ``dry_run`` peeks without claiming."""
        now = time.time() if now is None else now
        owner = owner or self._lease_owner()
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, body_json, bytes, next_attempt_at FROM events"
                " WHERE state='pending' AND next_attempt_at <= ? ORDER BY created_at, rowid",
                (now,),
            ).fetchall()
            if dry_run:
                return [{"id": row[0], "next_attempt_at": row[3]} for row in rows[:limit]]
            chosen: list[tuple[str, str]] = []
            batch_bytes = 0
            for row in rows:
                if len(chosen) >= limit or (chosen and batch_bytes + row[2] > max_bytes):
                    break
                chosen.append((row[0], row[1]))
                batch_bytes += row[2]
            if not chosen:
                return []
            self._conn.executemany(
                "UPDATE events SET state='in_flight', owner=? WHERE id=? AND state='pending'",
                [(owner, event_id) for event_id, _ in chosen],
            )
            self._conn.commit()
            return [json.loads(body) for _, body in chosen]

    def ack(self, ids: list[str]) -> int:
        """Verified acceptance: the only transition to ``done`` with delivered credit."""
        if not ids:
            return 0
        with self._lock:
            cursor = self._conn.executemany(
                "UPDATE events SET state='done', owner=NULL WHERE id=? AND state='in_flight'",
                [(event_id,) for event_id in ids],
            )
            delivered = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            if delivered:
                self._bump("delivered", delivered)
            self._bump("batches_acked")
            self._conn.commit()
            return delivered

    def settle_batch(self, id_status: dict[str, int | None]) -> tuple[int, list[str]]:
        """Apply per-event outcomes: retryable (or transport failure ``None``) re-queues with
        backoff; exhausted attempts are lost; other errors settle as auditable rejections.
        Returns ``(requeued, terminal_ids)``."""
        now = time.time()
        requeued = 0
        terminal: list[str] = []
        with self._lock:
            for event_id, status in id_status.items():
                row = self._conn.execute(
                    "SELECT attempts, state FROM events WHERE id=? AND state='in_flight'",
                    (event_id,),
                ).fetchone()
                if row is None:
                    continue
                attempts, _ = row
                if status is not None and status not in RETRYABLE_STATUSES:
                    self._conn.execute(
                        "UPDATE events SET state='done', owner=NULL WHERE id=?", (event_id,))
                    self._bump("rejected")
                    terminal.append(event_id)
                    continue
                attempts += 1
                if attempts >= MAX_ATTEMPTS:
                    self._conn.execute(
                        "UPDATE events SET state='lost', owner=NULL, attempts=? WHERE id=?",
                        (attempts, event_id))
                    self._bump("lost_attempts")
                    terminal.append(event_id)
                    continue
                delay = min(RETRY_BASE_S * (2 ** attempts), RETRY_CAP_S)
                self._conn.execute(
                    "UPDATE events SET state='pending', owner=NULL, attempts=?, next_attempt_at=?"
                    " WHERE id=?", (attempts, now + delay, event_id))
                requeued += 1
            self._conn.commit()
        return requeued, terminal

    def has_due(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM events WHERE state='pending' AND next_attempt_at <= ? LIMIT 1",
                (now,)).fetchone()
        return row is not None

    def next_due_at(self) -> float | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT MIN(next_attempt_at) FROM events WHERE state='pending'").fetchone()
        return row[0] if row and row[0] is not None else None

    # -- lifecycle ----------------------------------------------------------

    def recover(self) -> int:
        """Orphaned ``in_flight`` leases (crash/SIGKILL) return to ``pending`` (AC1)."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE events SET state='pending', owner=NULL WHERE state='in_flight'")
            recovered = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            if recovered:
                self._bump("recovered", recovered)
            self._conn.commit()
            return recovered

    def expire(self, now: float | None = None) -> int:
        """Purge events older than the retention window (AC4)."""
        now = time.time() if now is None else now
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM events WHERE state IN ('pending', 'in_flight')"
                " AND created_at < ?", (now - EXPIRY_AGE_S,))
            expired = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            if expired:
                self._bump("expired", expired)
            self._conn.commit()
            return expired

    # -- inspection ---------------------------------------------------------

    def counters(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute("SELECT key, value FROM counters").fetchall()
        stored = dict(rows)
        return {key: stored.get(key, 0) for key in _COUNTER_KEYS}

    def pending_stats(self) -> tuple[int, int]:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*), COALESCE(SUM(bytes), 0) FROM events WHERE state IN ('pending', 'in_flight')"
            ).fetchone()
        return int(row[0]), int(row[1])

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- internals ----------------------------------------------------------

    def _lease_owner(self) -> str:
        return f"{os.getpid()}:{_boot_id()}"

    def _bump(self, key: str, amount: int = 1) -> None:
        self._conn.execute(
            "INSERT INTO counters (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = value + excluded.value",
            (key, amount))

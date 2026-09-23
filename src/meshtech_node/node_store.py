"""Disk memory for meshtech-node - the plugin's proven SQLite store.

ADOPTED CODE (Brett, 2026-09-21: "use the database we made for that
project"): the store from the meshtech answer-bot plugin
(meshtech-plugin/src/meshtech_answerbot/core/store.py), trimmed to the
two tables the node actually needs and credited in ATTRIBUTION.md.

SCOPE RULE (agreed with Brett the same day): nodes + repeaters ONLY.
Raw packets are NEVER stored - not on hilltop, and (hard rule for the
future companion phone) not on any battery device. The bot kept raw
packets for its analysis dashboards and to recover fields the old
openhop REST API stripped; hilltop hears the radio directly (nothing
stripped) and its map math consumes exactly the one-hour RAM window.
The RAM window keeps doing all minute-to-minute work unchanged.

What disk memory buys: a restart no longer forgets who exists.
Every node/repeater fact is written through as it is learned (no
accumulate-and-flush buffer, so RAM cannot bloat), and at boot the
database refills the RAM tables - the map is whole again in seconds.

Design points carried over from the plugin verbatim:
- WAL journal + synchronous=NORMAL: crash-safe without per-write fsync
  stalls (kind to the Pi's SD card, and later to a phone's flash).
- Numbered migrations (PRAGMA user_version) so future schema changes
  never lose existing rows.
- Upsert semantics: "existing non-empty values are kept" - a later
  packet can never blank a name or position we already know (the
  honesty rule: unknown never overwrites known).
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("meshtech-node.nodestore")

# Versioned migrations: (version, [sql statements]). Start at 1 with
# just the two tables; the plugin's packet/message tables are
# deliberately NOT here (scope rule above).
_MIGRATIONS: List[tuple] = [
    (1, [
        """
        CREATE TABLE IF NOT EXISTS nodes (
            prefix        INTEGER PRIMARY KEY,   -- first pubkey byte (the mesh id)
            pubkey        TEXT,                  -- full 32B key hex (when an advert carried it)
            name          TEXT,
            node_class    INTEGER,               -- INTRO flags bits 2-3 (0 = unknown)
            lat           REAL,
            lon           REAL,
            first_seen    REAL NOT NULL,
            last_seen     REAL NOT NULL
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_nodes_last_seen ON nodes(last_seen)",
        """
        CREATE TABLE IF NOT EXISTS repeaters (
            tag           TEXT PRIMARY KEY,      -- path tag hex AS HEARD (1-3 bytes)
            hash_size     INTEGER NOT NULL,      -- 1, 2 or 3 (tag width)
            relay_count   INTEGER NOT NULL DEFAULT 0,
            first_heard   REAL NOT NULL,
            last_heard    REAL NOT NULL,
            pubkey        TEXT,                  -- set the moment an advert matches
            name          TEXT,
            prefix        INTEGER,               -- first pubkey byte after promotion
            node_class    INTEGER DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_repeaters_last_heard ON repeaters(last_heard)",
    ]),
]


def _now() -> float:
    return time.time()


class NodeStore:
    """The node's disk memory (SQLite, stdlib only). Mirrors the RAM
    tables in observations.RollingStore._nodes and
    repeaters.RepeaterTable - same fields, same honesty rules."""

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        parent = Path(self.db_path).parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        # WAL's natural partner: fsync only at checkpoints, not per commit.
        # On a Pi's SD card (and a phone's flash) this removes the
        # per-message fsync stall while staying crash-safe (worst case on
        # power cut: the last commits are lost, never a corrupted database).
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._migrate()

    # ---------------------------------------------------------------- schema

    def _migrate(self) -> None:
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        for target, statements in _MIGRATIONS:
            if target <= version:
                continue
            with self._conn:
                for statement in statements:
                    self._conn.execute(statement)
                self._conn.execute(f"PRAGMA user_version={int(target)}")
            version = target

    def close(self) -> None:
        try:
            self._conn.commit()
            self._conn.close()
        except Exception:
            pass

    # ----------------------------------------------------------------- nodes

    def upsert_node(self, prefix: int, *, name: Optional[str] = None,
                    node_class: Optional[int] = None,
                    lat: Optional[float] = None, lon: Optional[float] = None,
                    pubkey: Optional[str] = None,
                    ts: Optional[float] = None) -> None:
        """Add or refresh one node. Existing non-empty values are kept
        (the plugin's rule): unknown never overwrites known."""
        ts = ts if ts is not None else _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO nodes (prefix, pubkey, name, node_class, lat, lon,
                                   first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(prefix) DO UPDATE SET
                    last_seen  = excluded.last_seen,
                    pubkey     = CASE WHEN excluded.pubkey IS NOT NULL
                                      THEN excluded.pubkey ELSE nodes.pubkey END,
                    name       = CASE WHEN excluded.name IS NOT NULL
                                      AND trim(excluded.name) != ''
                                      THEN excluded.name ELSE nodes.name END,
                    node_class = CASE WHEN excluded.node_class IS NOT NULL
                                      AND excluded.node_class != 0
                                      THEN excluded.node_class
                                      ELSE nodes.node_class END,
                    -- 0.0/0.0 means NO POSITION (the ingest layer's own
                    -- rule, contact rows et al): never overwrite a real
                    -- fix with the null-island sentinel.
                    lat        = CASE WHEN excluded.lat IS NOT NULL
                                      AND (excluded.lat != 0.0
                                           OR excluded.lon IS NULL
                                           OR excluded.lon != 0.0)
                                      THEN excluded.lat ELSE nodes.lat END,
                    lon        = CASE WHEN excluded.lon IS NOT NULL
                                      AND (excluded.lon != 0.0
                                           OR excluded.lat IS NULL
                                           OR excluded.lat != 0.0)
                                      THEN excluded.lon ELSE nodes.lon END
                """,
                (int(prefix), pubkey, name, node_class, lat, lon, ts, ts),
            )

    def node_rows(self) -> List[Dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM nodes").fetchall()
        return [dict(r) for r in rows]

    def node_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM nodes").fetchone()[0])

    def forget_node(self, prefix: int) -> int:
        """Delete ONE node row (the RAM twin-merge's disk mirror: the
        retired identity must not resurrect at the next boot refill).
        Returns rows deleted (0 = nothing to forget)."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM nodes WHERE prefix = ?", (int(prefix),))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def forget_older_than(self, cutoff_ts: float) -> int:
        """Delete nodes silent past the cutoff (the RAM table's
        FORGET_AFTER_S rule, mirrored so disk cannot outgrow RAM's
        posture). Returns rows deleted."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM nodes WHERE last_seen < ?", (cutoff_ts,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # ------------------------------------------------------------- repeaters

    def upsert_repeater(self, tag: bytes, hash_size: int, relay_count: int,
                        first_heard: float, last_heard: float, *,
                        pubkey: Optional[str] = None,
                        name: Optional[str] = None,
                        prefix: Optional[int] = None,
                        node_class: int = 0) -> None:
        """Write one repeater entry. Called from the RAM table after
        each change (write-through), and at boot refill in bulk."""
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO repeaters (tag, hash_size, relay_count,
                                       first_heard, last_heard,
                                       pubkey, name, prefix, node_class)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tag) DO UPDATE SET
                    hash_size   = excluded.hash_size,
                    relay_count = excluded.relay_count,
                    first_heard = excluded.first_heard,
                    last_heard  = excluded.last_heard,
                    pubkey      = CASE WHEN excluded.pubkey IS NOT NULL
                                       THEN excluded.pubkey ELSE repeaters.pubkey END,
                    name        = CASE WHEN excluded.name IS NOT NULL
                                       AND trim(excluded.name) != ''
                                       THEN excluded.name ELSE repeaters.name END,
                    prefix      = CASE WHEN excluded.prefix IS NOT NULL
                                       THEN excluded.prefix ELSE repeaters.prefix END,
                    node_class  = CASE WHEN excluded.node_class IS NOT NULL
                                       AND excluded.node_class != 0
                                       THEN excluded.node_class
                                       ELSE repeaters.node_class END
                """,
                (tag.hex(), int(hash_size), int(relay_count),
                 float(first_heard), float(last_heard), pubkey, name,
                 prefix, int(node_class)),
            )

    def repeater_rows(self) -> List[Dict[str, object]]:
        rows = self._conn.execute("SELECT * FROM repeaters").fetchall()
        out: List[Dict[str, object]] = []
        for r in rows:
            d = dict(r)
            try:
                d["tag_bytes"] = bytes.fromhex(str(d.get("tag") or ""))
            except ValueError:
                continue          # corrupt row: skip, never crash the boot
            out.append(d)
        return out

    def repeater_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM repeaters").fetchone()[0])

    def forget_repeaters_older_than(self, cutoff_ts: float) -> int:
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM repeaters WHERE last_heard < ?", (cutoff_ts,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

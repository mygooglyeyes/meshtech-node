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
    # VECTORED SYNC (2026-09-24, VECTORED-SYNC-DESIGN.md migration 2):
    # nodes.change_seq = the node row's change-counter stamp; the
    # 1-row sync_state table = the table-wide monotonic counter,
    # CONTINUING across restarts (a reset could reuse numbers a phone
    # already consumed - silently skipping changes, the exact bug
    # class this design kills). Backfill: every existing row is
    # stamped at the counter's start value -> the first vectored ask
    # after this ships is one full roster, then it pays.
    (2, [
        "ALTER TABLE nodes ADD COLUMN change_seq INTEGER NOT NULL DEFAULT 0",
        """
        CREATE TABLE IF NOT EXISTS sync_state (
            id           INTEGER PRIMARY KEY CHECK (id = 1),
            change_seq   INTEGER NOT NULL
        )
        """,
        "INSERT OR IGNORE INTO sync_state (id, change_seq) VALUES (1, 0)",
        "UPDATE nodes SET change_seq = (SELECT change_seq FROM sync_state WHERE id = 1)",
        """
        CREATE TABLE IF NOT EXISTS gone_pending (
            prefix       INTEGER PRIMARY KEY,
            gone_at      REAL NOT NULL
        )
        """,
    ]),
    # ROUTE MEMORY (2026-09-24, Brett: routes were never meant to be
    # RAM-only). One row per PATH (the trail of repeater prefixes a
    # packet traversed; a DIRECT hop stores the sender's own prefix
    # byte). No raw packets - the path, the use count, the MEASURED
    # median delay (start-to-end time, honest origin stamps only) and
    # the last-heard age the fade law runs on. A route's deletion is
    # the only route lifecycle event stored (staleness is derived
    # from last_heard, mirroring the node table).
    (3, [
        """
        CREATE TABLE IF NOT EXISTS routes (
            path_hex      TEXT PRIMARY KEY,   -- repeater trail hex AS HEARD ('' never; >=1 byte)
            sender_prefix INTEGER NOT NULL DEFAULT 0,  -- last sender (0 = unknown)
            is_direct     INTEGER NOT NULL DEFAULT 0,  -- heard straight from the sender (the 3/7 fade kind)
            section_id    INTEGER NOT NULL DEFAULT -1, -- square the traffic was heard in (-1 = unknown)
            first_heard   REAL NOT NULL,
            last_heard    REAL NOT NULL,
            packet_count  INTEGER NOT NULL DEFAULT 0,
            delay_med_s   INTEGER NOT NULL DEFAULT 0   -- 0 = unknown (no honest stamps)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_routes_last_heard ON routes(last_heard)",
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
                    ts: Optional[float] = None,
                    change_seq: Optional[int] = None) -> None:
        """Add or refresh one node. Existing non-empty values are kept
        (the plugin's rule): unknown never overwrites known.

        change_seq (vectored sync): when not None, the row's change
        stamp is set to it (the caller bumped the table counter for a
        REAL fact change); None leaves any existing stamp untouched."""
        ts = ts if ts is not None else _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO nodes (prefix, pubkey, name, node_class, lat, lon,
                                   first_seen, last_seen, change_seq)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                                      THEN excluded.lon ELSE nodes.lon END,
                    change_seq = CASE WHEN excluded.change_seq IS NOT NULL
                                      THEN excluded.change_seq
                                      ELSE nodes.change_seq END
                """,
                (int(prefix), pubkey, name, node_class, lat, lon, ts, ts,
                 0 if change_seq is None else int(change_seq)),
            )

    def node_rows(self) -> List[Dict[str, object]]:
        rows = self._conn.execute(
            "SELECT * FROM nodes").fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------- vectored sync

    def sync_seq(self) -> int:
        """The table-wide change counter (monotonic, disk-backed)."""
        return int(self._conn.execute(
            "SELECT change_seq FROM sync_state WHERE id = 1").fetchone()[0])

    def bump_sync_seq(self) -> int:
        """Advance the counter by exactly one and return the new value.
        Called from ONE choke point (the RAM store's disk write-through
        for real fact changes) - no other writer may bump."""
        with self._conn:
            self._conn.execute(
                "UPDATE sync_state SET change_seq = change_seq + 1 "
                "WHERE id = 1")
        return self.sync_seq()

    def stamp_node_change(self, prefix: int, seq: int) -> None:
        """Record a node row's change stamp (called in the same
        transaction as the bump)."""
        self._conn.execute(
            "UPDATE nodes SET change_seq = ? WHERE prefix = ?",
            (int(seq), int(prefix)))

    def changed_node_rows_since(self, marker: int) -> List[int]:
        """Prefixes whose facts changed after the given marker."""
        rows = self._conn.execute(
            "SELECT prefix FROM nodes WHERE change_seq > ?",
            (int(marker),)).fetchall()
        return [int(r[0]) for r in rows]

    def node_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM nodes").fetchone()[0])

    def forget_node(self, prefix: int) -> int:
        """Delete ONE node row (the RAM name-supersede's disk mirror:
        the retired identity must not resurrect at the next boot
        refill). Returns rows deleted (0 = nothing to forget)."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM nodes WHERE prefix = ?", (int(prefix),))
        self.remember_gone(prefix)
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def remember_gone(self, prefix: int) -> None:
        """Queue a 'node is gone' fact for the next vectored ask
        (the phone must be able to REMOVE its dot - an honest
        deletion, not a stale fade). Queued events survive until a
        vectored answer consumes them; capped so a phone-less month
        cannot grow the table forever (oldest dropped past 64)."""
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO gone_pending (prefix, gone_at) "
                "VALUES (?, ?)", (int(prefix), _now()))
            self._conn.execute(
                "DELETE FROM gone_pending WHERE prefix IN ("
                "SELECT prefix FROM gone_pending ORDER BY gone_at "
                "DESC LIMIT -1 OFFSET 64)")

    def pending_gone(self) -> List[int]:
        prefixes = self._conn.execute(
            "SELECT prefix FROM gone_pending ORDER BY gone_at").fetchall()
        return [int(r[0]) for r in prefixes]

    def clear_gone(self, prefixes: List[int]) -> None:
        if not prefixes:
            return
        with self._conn:
            self._conn.executemany(
                "DELETE FROM gone_pending WHERE prefix = ?",
                [(int(p),) for p in prefixes])

    def forget_older_than(self, cutoff_ts: float) -> int:
        """Delete nodes silent past the cutoff (the RAM table's
        FORGET_AFTER_S rule, mirrored so disk cannot outgrow RAM's
        posture). Deleted prefixes are queued as GONE (vectored sync:
        the phone must learn the deletion). Returns rows deleted."""
        with self._conn:
            rows = self._conn.execute(
                "SELECT prefix FROM nodes WHERE last_seen < ?",
                (cutoff_ts,)).fetchall()
            cur = self._conn.execute(
                "DELETE FROM nodes WHERE last_seen < ?", (cutoff_ts,))
            for r in rows:
                self._conn.execute(
                    "INSERT OR REPLACE INTO gone_pending (prefix, gone_at) "
                    "VALUES (?, ?)", (int(r[0]), _now()))
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

    # -------------------------------------------------------------- routes

    def upsert_route(self, *, path_bytes: bytes, first_heard: float,
                     last_heard: float, packet_count: int,
                     delay_med_s: int, sender_prefix: int = 0,
                     is_direct: bool = False, section_id: int = -1,
                     ts: Optional[float] = None) -> None:
        """Write one route use (called from the RAM store's
        write-through at every hearing). count/delay/last-heard are
        REPLACE (the newest answer IS the route); first-heard keeps
        its origin; sender_prefix updates to the latest sender.

        The sender is the route's SECTION ANCHOR: at boot refill the
        RAM positions exist only if the node table survived too - the
        stored sender keeps the route anchored in the same home square
        it was heard in even before any advert re-places it.
        """
        ts = ts if ts is not None else _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO routes (path_hex, sender_prefix, is_direct,
                                    section_id, first_heard, last_heard,
                                    packet_count, delay_med_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path_hex) DO UPDATE SET
                    sender_prefix = excluded.sender_prefix,
                    is_direct     = excluded.is_direct,
                    section_id    = excluded.section_id,
                    last_heard    = excluded.last_heard,
                    packet_count  = excluded.packet_count,
                    delay_med_s   = excluded.delay_med_s,
                    first_heard   = MIN(routes.first_heard, excluded.first_heard)
                """,
                (path_bytes.hex(), int(sender_prefix), 1 if is_direct else 0,
                 int(section_id), float(first_heard), float(last_heard),
                 int(packet_count), int(delay_med_s)),
            )

    def route_rows(self) -> List[Dict[str, object]]:
        rows = self._conn.execute("SELECT * FROM routes").fetchall()
        return [dict(r) for r in rows]

    def route_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM routes").fetchone()[0])

    def forget_routes(self, path_hex_list: List[bytes]) -> int:
        """Delete the given routes (the DEAD stage of Brett's fade law,
        mirrored RAM-side). Returns rows deleted."""
        if not path_hex_list:
            return 0
        with self._conn:
            cur = self._conn.executemany(
                "DELETE FROM routes WHERE path_hex = ?",
                [(p.hex(),) for p in path_hex_list])
        total = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        return total

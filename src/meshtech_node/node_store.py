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
from typing import Dict, List, Optional

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
    # HEARD-BY COVERAGE (2026-09-26, lab plan Ch5): the collector
    # (mqttsource) records WHICH observer heard WHICH packet - the same
    # packet hash reported by several observers is the coverage map
    # (repeater-planning gold). One row per (hash, observer): re-hearing
    # is an upsert, never a duplicate. No raw payloads - hash, observer,
    # region and the signal each observer measured, nothing more.
    # Bounded like every table here (size cap, oldest out first): a
    # time-based fade law is Brett's call, not a guess.
    (4, [
        """
        CREATE TABLE IF NOT EXISTS heard_by (
            packet_hash   TEXT NOT NULL,       -- observer-reported packet hash
            observer_id   TEXT NOT NULL,       -- observer pubkey hex (the hearer)
            region        TEXT NOT NULL DEFAULT '',  -- IATA region from the topic
            rssi          REAL,                -- NULL = honestly unknown
            snr           REAL,
            first_heard   REAL NOT NULL,
            last_heard    REAL NOT NULL,
            hear_count    INTEGER NOT NULL DEFAULT 1,
            PRIMARY KEY (packet_hash, observer_id)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_heard_by_last_heard ON heard_by(last_heard)",
    ]),
    # MESH CLINIC (2026-09-27, CLINIC-WIRE.md): the clinic's stores.
    # Node charts (availability strip, signal stats, hop histogram,
    # 24 h counts), trouble flags (evidence rows, never verdicts),
    # peer reports (what another box SAID, tagged with its origin),
    # and the route delay spread (min/max beside the stored median -
    # lifetime facts, honest non-negative samples only). All
    # write-through like the node table; boot refills every one. No
    # raw packets are ever stored (scope rule).
    (5, [
        "ALTER TABLE routes ADD COLUMN delay_min_s INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE routes ADD COLUMN delay_max_s INTEGER NOT NULL DEFAULT 0",
        """
        CREATE TABLE IF NOT EXISTS clinic_nodes (
            prefix        INTEGER PRIMARY KEY,  -- node key (first pubkey byte)
            first_heard   REAL NOT NULL,
            last_heard    REAL NOT NULL,
            anchor_hour   INTEGER NOT NULL,     -- newest strip slot (epoch hour)
            buckets       BLOB NOT NULL,        -- 24 x uint32 LE hourly hears
            hops          BLOB NOT NULL,        -- 16 x uint32 LE hop histogram
            rssi_n        INTEGER NOT NULL DEFAULT 0,
            rssi_ewma     REAL NOT NULL DEFAULT 0,
            rssi_best     REAL,                 -- NULL = honestly no reading
            rssi_worst    REAL,
            rssi_mean     REAL NOT NULL DEFAULT 0,
            rssi_m2       REAL NOT NULL DEFAULT 0,
            snr_n         INTEGER NOT NULL DEFAULT 0,
            snr_ewma      REAL NOT NULL DEFAULT 0,
            snr_best      REAL,
            snr_worst     REAL,
            snr_mean      REAL NOT NULL DEFAULT 0,
            snr_m2        REAL NOT NULL DEFAULT 0
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_clinic_nodes_last ON clinic_nodes(last_heard)",
        """
        CREATE TABLE IF NOT EXISTS clinic_flags (
            kind          INTEGER NOT NULL,   -- 1 sig-fail 2 ts-back 3 storm 4 corrupt
            subject       INTEGER NOT NULL,   -- key prefix; 0 = mesh-wide (corrupt)
            events        INTEGER NOT NULL DEFAULT 0,
            first         REAL NOT NULL,
            last          REAL NOT NULL,
            detail        INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (kind, subject)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS peer_reports (
            source        INTEGER NOT NULL,   -- the peer box that said it
            report        INTEGER NOT NULL,   -- 1 pulse 2 sect_sum 3 route 4 intro
            subject       INTEGER NOT NULL,
            path_hex      TEXT NOT NULL DEFAULT '',
            first         REAL NOT NULL,
            last          REAL NOT NULL,
            v1            INTEGER NOT NULL DEFAULT 0,
            v2            INTEGER NOT NULL DEFAULT 0,
            v3            INTEGER NOT NULL DEFAULT 0,
            v4            INTEGER NOT NULL DEFAULT 0,
            cls           INTEGER NOT NULL DEFAULT 0,
            lat           REAL,               -- NULL = peer reported no position
            lon           REAL,
            name          TEXT NOT NULL DEFAULT '',
            PRIMARY KEY (source, report, subject, path_hex)
        )
        """,
        "CREATE INDEX IF NOT EXISTS idx_peer_reports_last ON peer_reports(last)",
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
        self._heard_writes = 0      # heard_by upserts since open (cap gate)
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
        refill). MESH CLINIC: the node's chart and its trouble flags
        die with it - charts are removed only when the node is dead or
        gone, never earlier. Returns rows deleted (0 = nothing)."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM nodes WHERE prefix = ?", (int(prefix),))
            self._conn.execute(
                "DELETE FROM clinic_nodes WHERE prefix = ?", (int(prefix),))
            self._conn.execute(
                "DELETE FROM clinic_flags WHERE subject = ?", (int(prefix),))
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
                     delay_min_s: int = 0, delay_max_s: int = 0,
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
                                    packet_count, delay_med_s,
                                    delay_min_s, delay_max_s)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(path_hex) DO UPDATE SET
                    sender_prefix = excluded.sender_prefix,
                    is_direct     = excluded.is_direct,
                    section_id    = excluded.section_id,
                    last_heard    = excluded.last_heard,
                    packet_count  = excluded.packet_count,
                    delay_med_s   = excluded.delay_med_s,
                    delay_min_s   = excluded.delay_min_s,
                    delay_max_s   = excluded.delay_max_s,
                    first_heard   = MIN(routes.first_heard, excluded.first_heard)
                """,
                (path_bytes.hex(), int(sender_prefix), 1 if is_direct else 0,
                 int(section_id), float(first_heard), float(last_heard),
                 int(packet_count), int(delay_med_s), int(delay_min_s),
                 int(delay_max_s)),
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

    # ------------------------------------------------------ mesh clinic

    def upsert_clinic_node(self, row: Dict[str, object]) -> None:
        """Write one node chart row (write-through at every hear; the
        row carries the chart's full state - REPLACE is the truth)."""
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO clinic_nodes (
                    prefix, first_heard, last_heard, anchor_hour,
                    buckets, hops, rssi_n, rssi_ewma, rssi_best,
                    rssi_worst, rssi_mean, rssi_m2, snr_n, snr_ewma,
                    snr_best, snr_worst, snr_mean, snr_m2)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(row["prefix"]), float(row["first_heard"]),
                 float(row["last_heard"]), int(row["anchor_hour"]),
                 bytes(row["buckets"]), bytes(row["hops"]),
                 int(row.get("rssi_n") or 0),
                 float(row.get("rssi_ewma") or 0.0),
                 row.get("rssi_best"), row.get("rssi_worst"),
                 float(row.get("rssi_mean") or 0.0),
                 float(row.get("rssi_m2") or 0.0),
                 int(row.get("snr_n") or 0),
                 float(row.get("snr_ewma") or 0.0),
                 row.get("snr_best"), row.get("snr_worst"),
                 float(row.get("snr_mean") or 0.0),
                 float(row.get("snr_m2") or 0.0)),
            )

    def clinic_node_rows(self) -> List[Dict[str, object]]:
        rows = self._conn.execute("SELECT * FROM clinic_nodes").fetchall()
        return [dict(r) for r in rows]

    def forget_clinic_nodes_before(self, cutoff_ts: float) -> int:
        """Charts die only with their node (30 d silent = gone, the
        node forget law mirrored). Returns rows deleted."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM clinic_nodes WHERE last_heard < ?",
                (float(cutoff_ts),))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    def upsert_clinic_flag(self, row: Dict[str, object]) -> None:
        """Write one trouble-flag row (evidence, never a verdict)."""
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO clinic_flags
                    (kind, subject, events, first, last, detail)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (int(row["kind"]), int(row["subject"]),
                 int(row["events"]), float(row["first"]),
                 float(row["last"]), int(row["detail"])),
            )

    def clinic_flag_rows(self) -> List[Dict[str, object]]:
        rows = self._conn.execute("SELECT * FROM clinic_flags").fetchall()
        return [dict(r) for r in rows]

    def upsert_peer_report(self, row: Dict[str, object]) -> None:
        """Write one peer report (what another box SAID, tagged with
        which box said it). Values ride as v1..v4 - report-shaped
        facts, never raw packets."""
        values = tuple(int(v) for v in row.get("values") or (0, 0, 0, 0))
        values = (values + (0, 0, 0, 0))[:4]
        with self._conn:
            self._conn.execute(
                """
                INSERT OR REPLACE INTO peer_reports
                    (source, report, subject, path_hex, first, last,
                     v1, v2, v3, v4, cls, lat, lon, name)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (int(row["source"]), int(row["report"]),
                 int(row["subject"]), str(row.get("path_hex") or ""),
                 float(row["first"]), float(row["last"]),
                 values[0], values[1], values[2], values[3],
                 int(row.get("cls") or 0), row.get("lat"),
                 row.get("lon"), str(row.get("name") or "")),
            )

    def peer_report_rows(self) -> List[Dict[str, object]]:
        rows = self._conn.execute("SELECT * FROM peer_reports").fetchall()
        out: List[Dict[str, object]] = []
        for r in rows:
            d = dict(r)
            d["values"] = (int(d.get("v1") or 0), int(d.get("v2") or 0),
                           int(d.get("v3") or 0), int(d.get("v4") or 0))
            out.append(d)
        return out

    def trim_peer_reports(self, max_rows: int) -> int:
        """Size cap, oldest out first (bounded like every table here -
        a fade law for peer words is Brett's call, not a guess)."""
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM peer_reports WHERE rowid IN ("
                "SELECT rowid FROM peer_reports ORDER BY last "
                "DESC LIMIT -1 OFFSET ?)", (int(max_rows),))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

    # ----------------------------------------------------------- heard-by

    HEARD_BY_MAX_ROWS = 50_000       # size cap; oldest rows out first

    def upsert_heard_by(self, packet_hash: str, observer_id: str, *,
                        region: str = "", rssi: Optional[float] = None,
                        snr: Optional[float] = None,
                        ts: Optional[float] = None) -> None:
        """Record one observer hearing one packet (Ch5, write-through
        from the MQTT collector). Same (hash, observer) again = the
        newest signal wins, hear_count grows, first_heard keeps its
        origin. rssi/snr stay NULL when the report carried none."""
        if not packet_hash or not observer_id:
            return                     # nothing identifiable to record
        ts = ts if ts is not None else _now()
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO heard_by (packet_hash, observer_id, region,
                                      rssi, snr, first_heard, last_heard,
                                      hear_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(packet_hash, observer_id) DO UPDATE SET
                    region      = excluded.region,
                    rssi        = excluded.rssi,
                    snr         = excluded.snr,
                    last_heard  = MAX(heard_by.last_heard, excluded.last_heard),
                    hear_count  = heard_by.hear_count + 1,
                    first_heard = MIN(heard_by.first_heard, excluded.first_heard)
                """,
                (str(packet_hash), str(observer_id), str(region or ""),
                 float(rssi) if rssi is not None else None,
                 float(snr) if snr is not None else None,
                 float(ts), float(ts)),
            )
        # Bounded table: every 256 writes, evict oldest-over-cap rows.
        self._heard_writes += 1
        if self._heard_writes % 256 == 0:
            self.prune_heard_by()

    def heard_by_rows(self, packet_hash: Optional[str] = None,
                      ) -> List[Dict[str, object]]:
        """Coverage rows (optionally for one packet hash), oldest first."""
        if packet_hash is not None:
            rows = self._conn.execute(
                "SELECT * FROM heard_by WHERE packet_hash = ? "
                "ORDER BY last_heard",
                (str(packet_hash),)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM heard_by ORDER BY last_heard").fetchall()
        return [dict(r) for r in rows]

    def coverage_rows(self, min_hearers: int = 2,
                      ) -> List[Dict[str, object]]:
        """THE coverage map: packet hashes heard by min_hearers or more
        DIFFERENT observers, with each hearer's signal. Returns one row
        per hash: {packet_hash, hearers: [{observer_id, region, rssi,
        snr, last_heard}, ...]} (strongest hearer first)."""
        rows = self._conn.execute(
            "SELECT * FROM heard_by WHERE packet_hash IN ("
            "  SELECT packet_hash FROM heard_by"
            "  GROUP BY packet_hash HAVING COUNT(*) >= ?"
            ") ORDER BY packet_hash, last_heard",
            (int(min_hearers),)).fetchall()
        out: Dict[str, Dict[str, object]] = {}
        for r in rows:
            entry = out.setdefault(r["packet_hash"],
                                   {"packet_hash": r["packet_hash"],
                                    "hearers": []})
            entry["hearers"].append({
                "observer_id": r["observer_id"],
                "region": r["region"],
                "rssi": r["rssi"],
                "snr": r["snr"],
                "last_heard": r["last_heard"],
            })
        for entry in out.values():
            entry["hearers"].sort(
                key=lambda h: -(h["rssi"] if h["rssi"] is not None
                                else -999.0))
        return list(out.values())

    def heard_by_count(self) -> int:
        return int(self._conn.execute(
            "SELECT COUNT(*) FROM heard_by").fetchone()[0])

    def prune_heard_by(self, max_rows: Optional[int] = None) -> int:
        """Size-cap eviction: keep the newest max_rows by last_heard,
        delete the rest (returns rows deleted). The table is bounded
        like every memory here; a time-based fade needs Brett's word."""
        cap = int(max_rows if max_rows is not None
                  else self.HEARD_BY_MAX_ROWS)
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM heard_by WHERE rowid IN ("
                "  SELECT rowid FROM heard_by"
                "  ORDER BY last_heard DESC LIMIT -1 OFFSET ?)",
                (cap,))
        return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0

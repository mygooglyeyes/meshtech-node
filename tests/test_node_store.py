"""Disk-memory tests (2026-09-21, Brett: the plugin's database adopted).

The invariants agreed with Brett before building:
- nodes + repeaters are the ONLY tables (NO raw packet storage anywhere);
- every fact is written through the moment it is learned (no
  accumulate-and-flush buffer, so RAM cannot bloat);
- a restart forgets nobody: boot refill restores RAM from disk, with
  honest staleness (a week-silent node returns STALE, not fresh);
- RAM is the working truth: a database hiccup never takes the RX path
  down, and RAM never loses to the (older) disk copy;
- the repeater table's expiry is actually CALLED now (the audit find),
  with the disk mirror in step.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.node_store import NodeStore  # noqa: E402
from meshtech_node.observations import RollingStore  # noqa: E402
from meshtech_node.repeaters import RepeaterTable  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture()
def store(tmp_path):
    s = NodeStore(str(tmp_path / "test.db"))
    yield s
    s.close()


# ------------------------------------------------------------------ scope

def test_scope_nodes_and_repeaters_only(store):
    """The agreed scope rule: ONLY the two data tables exist. No
    packets, no messages - nothing raw, on any device. (Vectored sync
    2026-09-24 adds sync_state + gone_pending: bookkeeping tables
    holding COUNTERS and retirement notices - still no raw data.)"""
    tables = {r["name"] for r in store._conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"nodes", "repeaters", "sync_state", "gone_pending"}


# ------------------------------------------------------------- write-through

def test_node_facts_written_through(store):
    """A node fact learned in RAM lands on disk immediately (not at
    shutdown, not at flush) - restart can never lose it."""
    ram = RollingStore(window_seconds=3600.0)
    ram.disk = store
    now = time.time()
    ram.add_position(0x42, 38.1074, -122.5697, "Alpha", now=now)
    rows = store.node_rows()
    assert len(rows) == 1
    assert rows[0]["prefix"] == 0x42
    assert rows[0]["name"] == "Alpha"
    assert abs(rows[0]["lat"] - 38.1074) < 1e-6


def test_unknown_never_overwrites_known(store):
    """The plugin's honesty rule carried over: a nameless/positionless
    hearing cannot blank what we already know."""
    ram = RollingStore(window_seconds=3600.0)
    ram.disk = store
    now = time.time()
    ram.add_position(0x42, 38.1074, -122.5697, "Alpha", now=now)
    ram.add_position(0x42, 0.0, 0.0, None, now=now + 1)   # no pos, no name
    row = store.node_rows()[0]
    assert row["name"] == "Alpha"
    assert abs(row["lat"] - 38.1074) < 1e-6


def test_repeater_relay_written_through(store):
    ram = RepeaterTable(sink=store)
    ram.observe_tag(b"\x42\x99", now=1000.0)
    ram.observe_tag(b"\x42\x99", now=1001.0)
    row = store.repeater_rows()[0]
    assert row["tag_bytes"] == b"\x42\x99"
    assert row["relay_count"] == 2
    assert row["hash_size"] == 2


def test_promotion_written_through(store):
    pubkey = bytes([0x42] + list(range(31)))   # starts with the tag
    ram = RepeaterTable(sink=store)
    ram.observe_tag(b"\x42", now=1000.0)
    ram.observe_advert(pubkey, "SwedishRap", 1, now=1005.0)
    row = store.repeater_rows()[0]
    assert row["name"] == "SwedishRap"
    assert row["pubkey"] == pubkey.hex()
    assert row["prefix"] == 0x42


# ------------------------------------------------------------------ refill

def test_restart_refill_restores_nodes(store):
    """THE restart story: RAM dies, disk remembers, boot refills - and
    original last-heard times survive (staleness stays honest)."""
    ram1 = RollingStore(window_seconds=3600.0)
    ram1.disk = store
    weeks_ago = time.time() - 20 * 86400.0   # past the 14-day stale line
    ram1.add_position(0x42, 38.1, -122.5, "Alpha", now=weeks_ago)
    ram1.add_position(0x43, 38.2, -122.6, "Beta", now=time.time())

    # ...the process dies and a new one boots:
    ram2 = RollingStore(window_seconds=3600.0)
    ram2.disk = store
    restored = ram2.refill_nodes(store.node_rows())
    assert restored == 2
    # a node silent for 20 days comes back STALE (off maps) - the same
    # posture it would have had with no restart in between:
    assert ram2.node_is_stale(0x42) is True
    assert ram2.node_is_stale(0x43) is False
    # the fresh, positioned node is mappable again immediately; the
    # stale one is honestly withheld from the map:
    mapped = ram2.map_nodes()
    assert {m["prefix"] for m in mapped} == {0x43}


def test_refill_never_loses_ram_to_disk(store):
    """RAM wins where both exist: disk is always the older copy."""
    ram = RollingStore(window_seconds=3600.0)
    ram.disk = store
    ram.add_position(0x42, 38.5, -122.9, "NewName", now=time.time())
    # a stale disk row for the same node must not overwrite RAM:
    restored = ram.refill_nodes(
        [{"prefix": 0x42, "name": "OldName", "lat": 1.0, "lon": 1.0,
          "last_seen": time.time() - 86400.0}])
    assert restored == 0
    assert ram.node_info(0x42)["name"] == "NewName"


def test_repeater_refill_restores_forgotten_tags(store):
    pubkey = bytes([0x42, 0x99] + list(range(30)))  # starts with the tag
    ram1 = RepeaterTable(sink=store)
    ram1.observe_tag(b"\x42\x99", now=1000.0)
    ram1.observe_advert(pubkey, "SwedishRap", 1, now=1005.0)

    ram2 = RepeaterTable(sink=None)
    restored = ram2.refill_from(store.repeater_rows())
    assert restored == 1
    entry = ram2.entries[b"\x42\x99"]
    assert entry.relay_count == 1
    assert entry.identified
    assert entry.name == "SwedishRap"


# ------------------------------------------------------- RAM-is-the-truth

def test_sink_failure_never_breaks_rx():
    """A database hiccup must never take the RX path down: the RAM
    table keeps counting, the failure is logged, the next change
    retries the write."""
    class Exploding:
        def upsert_repeater(self, *a, **k):
            raise sqlite_error
    import sqlite3 as _sq
    sqlite_error = _sq.OperationalError("disk I/O error")

    ram = RepeaterTable(sink=Exploding())
    entry = ram.observe_tag(b"\x42", now=1000.0)   # must not raise
    assert entry.relay_count == 1


def test_disk_write_failure_never_breaks_ingest(store):
    class Exploding:
        def upsert_node(self, *a, **k):
            raise RuntimeError("disk full")
    ram = RollingStore(window_seconds=3600.0)
    ram.disk = Exploding()
    ram.add_position(0x42, 38.1, -122.5, "Alpha", now=time.time())
    assert ram.node_info(0x42)["name"] == "Alpha"   # RAM kept the truth


# ------------------------------------------------------------------ prune

def test_repeater_prune_actually_runs_and_mirrors_disk(store):
    """The audit find: the expiry existed but nobody called it. RAM and
    disk prune in step now."""
    ram = RepeaterTable(sink=store, expire_after_seconds=100.0)
    ram.observe_tag(b"\x01", now=0.0)       # ancient: silent 10_000 s
    ram.observe_tag(b"\x02", now=10_000.0)  # fresh
    pruned = ram.prune(now=10_000.0)
    assert pruned == 1
    assert b"\x01" not in ram.entries
    assert b"\x02" in ram.entries
    # the disk mirror dropped the same tag:
    assert {r["tag_bytes"] for r in store.repeater_rows()} == {b"\x02"}


def test_node_forget_mirrors_disk(store):
    ram = RollingStore(window_seconds=3600.0)
    ram.disk = store
    ancient = time.time() - 40 * 86400.0    # past the 30-day forget line
    ram.add_position(0x42, 38.1, -122.5, "Ghost", now=ancient)
    counts = ram.prune_nodes()
    assert counts["forgotten"] == 1
    assert store.node_count() == 0          # disk forgot with RAM

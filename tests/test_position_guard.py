"""Planet-range guard tests (2026-09-21, found live by Brett's db peek).

A corrupted advert reached the database claiming position
(-904.8662, -1873.5432) - outside the planet - with a mojibake name.
The invariant: a position that cannot exist is CORRUPTION, not data;
it is rejected as "no position" (the same answer 0.0/0.0 gets), the
rejection is logged loudly, and the node's other facts (name, class,
last-heard) are still recorded.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.observations import RollingStore  # noqa: E402


def _store(tmp_path):
    from meshtech_node.node_store import NodeStore
    disk = NodeStore(str(tmp_path / "g.db"))
    ram = RollingStore(window_seconds=3600.0)
    ram.disk = disk
    return ram, disk


def test_offplanet_position_rejected(tmp_path):
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0xF1, -904.8662, -1873.5432, None, now=now)
    assert ram.node_info(0xF1).get("lat") is None      # never in RAM
    # the node still counts as HEARD (C3: a hearing is evidence), so a
    # bare row with timestamps but NO position is what disk holds:
    row = disk.node_rows()[0]
    assert row["prefix"] == 0xF1
    assert row["lat"] is None and row["lon"] is None


def test_edge_positions_still_accepted(tmp_path):
    """Real-world extremes stay: ±85 lat and ±180 lon pass."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x01, 84.9, -179.9, "NearPole", now=now)
    assert ram.node_info(0x01)["lat"] == 84.9


def test_name_survives_corrupt_position(tmp_path):
    """Name and position are separate evidence: a garbled advert's
    position is corruption, but the (garbled) name is still what the
    wire carried - record it, keep the node accountable."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0xF1, -904.8662, -1873.5432, "Klaxon", now=now)
    row = disk.node_rows()[0]
    assert row["name"] == "Klaxon"
    assert row["lat"] is None


def test_good_advert_after_corrupt_one_wins(tmp_path):
    """A node whose advert got corrupted once is fixed the moment its
    next good advert arrives (self-healing, no manual cleanup)."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0xF1, -904.8662, -1873.5432, None, now=now)
    ram.add_position(0xF1, 38.1074, -122.5697, "NowReal", now=now + 5)
    assert ram.node_info(0xF1)["lat"] == 38.1074
    row = disk.node_rows()[0]
    assert abs(row["lat"] - 38.1074) < 1e-6
    assert row["name"] == "NowReal"

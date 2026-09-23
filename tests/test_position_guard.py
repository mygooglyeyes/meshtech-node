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


# ---- HALF-FIX GUARD (2026-09-22, KHV Solar live) -------------------------
# A torn advert can corrupt ONE half of the fix: KHV Solar stored
# lat exactly 0.0 with a good lon -121.908836. One exact 0.0 against a
# non-zero partner is corruption, not the equator.

def test_half_fix_lat_zero_good_lon_rejected(tmp_path):
    """KHV Solar's actual torn advert: lat 0.0, lon -121.9 -> no-position."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x21, 0.0, -121.908836, "KHV Solar RAK Repeater",
                     now=now)
    info = ram.node_info(0x21)
    assert info.get("lat") is None and info.get("lon") is None
    assert info["name"] == "KHV Solar RAK Repeater"   # hearing evidence stays
    row = disk.node_rows()[0]
    assert row["lat"] is None and row["lon"] is None
    assert row["name"] == "KHV Solar RAK Repeater"


def test_half_fix_lon_zero_good_lat_rejected(tmp_path):
    """Mirror case: good lat, lon torn to exactly 0.0 -> no-position."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x22, 37.4419, 0.0, "TornLon", now=now)
    info = ram.node_info(0x22)
    assert info.get("lat") is None and info.get("lon") is None


def test_true_null_island_still_rejected(tmp_path):
    """0.0/0.0 stays no-position (pre-existing rule, both-zero case)."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x23, 0.0, 0.0, "NullIsland", now=now)
    info = ram.node_info(0x23)
    assert info.get("lat") is None and info.get("lon") is None


def test_good_fix_still_stored_next_to_half_fixes(tmp_path):
    """The guard must not overreach: a normal Novato fix stores fine."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x3e, 38.09191, -122.566098, "Oakview", now=now)
    assert abs(ram.node_info(0x3e)["lat"] - 38.09191) < 1e-9


def test_half_fix_then_good_advert_wins(tmp_path):
    """Self-healing: KHV Solar's next clean advert stores for real."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x21, 0.0, -121.908836, None, now=now)
    ram.add_position(0x21, 37.3, -121.9, None, now=now + 5)
    assert abs(ram.node_info(0x21)["lat"] - 37.3) < 1e-9

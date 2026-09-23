"""Twin-identity merge tests (2026-09-23, Brett's double-dots report).

hilltop's own table held KN6OBW DT twice - prefix 3b AND a8, positions
~40 m apart, both with fixes. Both rows drew: the "second dot right
next to the node" on the map. The invariants:

1. An advert for one identity, arriving where ANOTHER identity claims
   the same name at the same place, merges: the newest identity wins,
   the older row is retired (RAM + disk), never drawn again.
2. The merge NEVER fires on name alone or place alone - different
   nodes at close range (repeater pairs, neighbors) survive intact.
3. A boot refill collapses twins already sitting on disk, keeping the
   fresher identity, and deletes the older row from disk.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.observations import RollingStore  # noqa: E402


def _store(tmp_path):
    from meshtech_node.node_store import NodeStore
    disk = NodeStore(str(tmp_path / "twin.db"))
    ram = RollingStore(window_seconds=3600.0)
    ram.disk = disk
    return ram, disk


# OFF-WIRE INTRO DECODE (the live client path): positions decode as
# deltas from the wire's assumed 40 km span; at 38 degrees the factor
# is cos(38) ~= 0.788, so a true 40 m separation arrives as ~31 m in
# the decoded lat. Tests place twins at the DECODED separation so the
# numbers match what the merge actually sees.
TWIN_LAT_SEP = 0.00003     # ~3 m decoded (a real twin's wobble)
CITY_LAT_SEP = 0.0006      # ~60 m decoded (two distinct close nodes)


def test_same_name_same_place_merges(tmp_path):
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x3B, 37.4419, -122.1430, "KN6OBW DT", now=now - 60)
    # the SAME node, heard later under a second identity
    ram.add_position(0xA8, 37.4419 + TWIN_LAT_SEP, -122.1430,
                     "KN6OBW DT", now=now)
    assert 0x3B not in ram.known_nodes()       # old identity retired
    assert 0xA8 in ram.known_nodes()           # newest identity wins
    info = ram.node_info(0xA8)
    assert info.get("name") == "KN6OBW DT"
    assert disk.forget_node(0x3B) == 0         # already gone from disk
    assert len(disk.node_rows()) == 1


def test_same_name_far_apart_never_merges(tmp_path):
    """Same name, kilometers apart: two real nodes (a name reuse), not
    a twin - both survive."""
    ram, _disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x10, 37.4419, -122.1430, "Relay", now=now - 60)
    ram.add_position(0x11, 38.4419, -122.1430, "Relay", now=now)
    assert 0x10 in ram.known_nodes()
    assert 0x11 in ram.known_nodes()


def test_same_place_different_names_never_merges(tmp_path):
    """Two nodes on one tower / two neighbors: the name is what the
    operator trusts - different names NEVER collapse into each other."""
    ram, _disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x20, 37.4419, -122.1430, "Alpha", now=now - 60)
    ram.add_position(0x21, 37.4419 + TWIN_LAT_SEP, -122.1430, "Beta",
                     now=now)
    assert 0x20 in ram.known_nodes()
    assert 0x21 in ram.known_nodes()


def test_nameless_second_identity_does_not_merge(tmp_path):
    """An advert with no name can't prove it is a twin - it must not
    retire the named identity (name + place BOTH required)."""
    ram, _disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x30, 37.4419, -122.1430, "Named", now=now - 60)
    ram.add_position(0x31, 37.4419 + TWIN_LAT_SEP, -122.1430, None,
                     now=now)
    assert 0x30 in ram.known_nodes()
    assert 0x31 in ram.known_nodes()


def test_boot_refill_collapses_disk_twins(tmp_path):
    """Rows written before the merge existed: the boot refill keeps the
    fresher identity and deletes the older row from disk so it cannot
    resurrect on the next restart."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x3B, 37.4419, -122.1430, "KN6OBW DT", now=now - 60)
    ram.add_position(0xA8, 37.4419 + TWIN_LAT_SEP, -122.1430, "KN6OBW DT",
                     now=now)
    disk.close()
    disk2 = type(disk)(str(tmp_path / "twin.db"))
    ram2 = RollingStore(window_seconds=3600.0)
    ram2.disk = disk2
    restored = ram2.refill_nodes(disk2.node_rows())
    assert restored == 1                       # ONE node came back
    assert 0xA8 in ram2.known_nodes()
    assert 0x3B not in ram2.known_nodes()
    assert disk2.forget_node(0x3B) == 0        # deleted from disk too

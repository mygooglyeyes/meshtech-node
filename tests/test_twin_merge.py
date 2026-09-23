"""Name-supersede tests (2026-09-23, Brett's corrected design).

v39's twin-merge needed same name AND nearly the same place (<=100 m);
Brett's live test then showed dots STILL doubling, and the corrected
rule is simpler and stronger: THE NAME IS THE IDENTITY. A fresh advert
carrying a name that matches an existing DIFFERENT prefix is a verified
change of location - identities are allowed to MOVE (mobile devices) -
so the old dot for that name is retired and the freshest advert wins.
NO distance test, no motion plausibility judgment.

Brett's amendment: implausible location data must still be filtered
before anything is saved - that stays the planet-range + half-fix
guards, which run BEFORE any supersede (test_position_guard.py pins
them; the interplay tests here pin that a torn advert can neither
relocate nor delete a good dot).

The invariants:
1. Same name, ANY distance apart: the fresh advert supersedes - the
   old identity is retired (RAM + disk), the newest wins.
2. A nameless advert cannot prove a name match: it never supersedes,
   never retires anything.
3. Corrupt/half-fix adverts are filtered first: a name arriving with
   an implausible fix keeps its name/hearing evidence but never moves
   a dot and never deletes another node's good position.
4. The boot refill collapses same-name rows already on disk - fresher
   identity kept in EITHER arrival order, older row deleted from disk
   so it cannot resurrect on the next restart.
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


def test_same_name_far_apart_supersedes(tmp_path):
    """The corrected rule: identities MOVE. Same name kilometers apart
    is a device that changed location - the freshest advert wins and
    the old dot is retired (RAM + disk). No distance test anywhere."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x10, 37.4419, -122.1430, "Relay", now=now - 60)
    ram.add_position(0x11, 38.4419, -122.1430, "Relay", now=now)
    assert 0x10 not in ram.known_nodes()       # old identity retired
    assert 0x11 in ram.known_nodes()           # newest identity wins
    info = ram.node_info(0x11)
    assert info.get("name") == "Relay"
    assert disk.forget_node(0x10) == 0         # already gone from disk
    assert len(disk.node_rows()) == 1

    # ...and the device moves BACK: its old prefix re-advertises, the
    # new one retires. Freshest advert wins, every time.
    ram.add_position(0x10, 37.4419, -122.1430, "Relay", now=now + 60)
    assert 0x11 not in ram.known_nodes()
    assert ram.node_info(0x10).get("lat") == 37.4419
    assert disk.forget_node(0x11) == 0


def test_same_place_close_still_supersedes(tmp_path):
    """The twin case that started this: two identities of one node
    ~40 m apart, same name. Distance is irrelevant now - the name
    alone collapses them; newest wins."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x3B, 37.4419, -122.1430, "KN6OBW DT", now=now - 60)
    ram.add_position(0xA8, 37.4419 + 0.00003, -122.1430, "KN6OBW DT",
                     now=now)
    assert 0x3B not in ram.known_nodes()
    assert 0xA8 in ram.known_nodes()
    assert len(disk.node_rows()) == 1


def test_different_names_never_supersede(tmp_path):
    """Two nodes on one tower / two neighbors / a phone and its owner:
    different names NEVER collapse into each other - the name is what
    the operator trusts."""
    ram, _disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x20, 37.4419, -122.1430, "Alpha", now=now - 60)
    ram.add_position(0x21, 37.4419 + 0.00003, -122.1430, "Beta", now=now)
    assert 0x20 in ram.known_nodes()
    assert 0x21 in ram.known_nodes()


def test_nameless_advert_never_supersedes(tmp_path):
    """An advert with no name can't prove it shares a name with an
    existing row - it never retires anything (even at the exact same
    place: place is not identity anymore)."""
    ram, _disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x30, 37.4419, -122.1430, "Named", now=now - 60)
    ram.add_position(0x31, 37.4419, -122.1430, None, now=now)
    assert 0x30 in ram.known_nodes()
    assert 0x31 in ram.known_nodes()


def test_corrupt_advert_filtered_before_supersede(tmp_path):
    """Brett's amendment: implausible location data is filtered BEFORE
    anything is saved. A torn advert carrying a good node's name must
    NOT (a) move that node's dot to the corrupt fix, nor (b) delete
    the node's good position. Off-planet and half-fix both covered."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x40, 38.1074, -122.5697, "Good Node", now=now - 60)
    # off-planet, same name, different prefix
    ram.add_position(0x41, -904.87, -1873.54, "Good Node", now=now)
    # half-fix (lat torn to exactly 0.0), same name, different prefix
    ram.add_position(0x42, 0.0, -121.9, "Good Node", now=now + 1)
    # the good dot survived, unmoved:
    assert 0x40 in ram.known_nodes()
    info = ram.node_info(0x40)
    assert info.get("lat") == 38.1074
    assert info.get("lon") == -122.5697
    # the corrupt rows kept only hearing/name evidence - no position:
    for p in (0x41, 0x42):
        assert p in ram.known_nodes()
        assert ram.node_info(p).get("lat") is None
        assert ram.node_info(p).get("lon") is None
        assert ram.node_info(p).get("name") == "Good Node"
    assert len(disk.node_rows()) == 3          # evidence kept on disk


def test_corrupt_row_superseded_by_later_clean_advert(tmp_path):
    """Mirror case: the corrupt advert arrives FIRST (stored as
    name + hearing, no position), the clean one after. The clean
    advert supersedes it - nothing of value is lost (the old row held
    no dot) and the table converges on one good record per name."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x50, 0.0, -121.9, "Mover", now=now - 60)
    assert ram.node_info(0x50).get("lat") is None   # corrupt: no dot
    ram.add_position(0x51, 38.1074, -122.5697, "Mover", now=now)
    assert 0x51 in ram.known_nodes()           # good fix stored
    assert ram.node_info(0x51).get("lat") == 38.1074
    assert 0x50 not in ram.known_nodes()       # junk row retired
    assert len(disk.node_rows()) == 1          # one record for the name


def test_boot_refill_collapses_disk_twins(tmp_path):
    """Rows written before the name rule existed: the boot refill keeps
    the fresher identity (regardless of arrival order) and deletes the
    older row from disk so it cannot resurrect on the next restart."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x3B, 37.4419, -122.1430, "KN6OBW DT", now=now - 60)
    ram.add_position(0xA8, 38.4419, -122.1430, "KN6OBW DT", now=now)
    disk.close()
    disk2 = type(disk)(str(tmp_path / "twin.db"))
    ram2 = RollingStore(window_seconds=3600.0)
    ram2.disk = disk2
    restored = ram2.refill_nodes(disk2.node_rows())
    assert restored == 1                       # ONE node came back
    assert 0xA8 in ram2.known_nodes()
    assert 0x3B not in ram2.known_nodes()
    assert disk2.forget_node(0x3B) == 0        # deleted from disk too
    # and the fresh dot carries the freshest location:
    assert ram2.node_info(0xA8).get("lat") == 38.4419


def test_boot_refill_collapses_disk_twins_either_order(tmp_path):
    """Same collapse with the rows arriving oldest-LAST (SQLite gives
    no row-order promise): the older identity is still the one that
    loses, and it is deleted from disk too."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x10, 38.4419, -122.1430, "Relay", now=now - 30)
    ram.add_position(0x11, 37.4419, -122.1430, "Relay", now=now)
    disk.close()
    disk2 = type(disk)(str(tmp_path / "twin.db"))
    ram2 = RollingStore(window_seconds=3600.0)
    ram2.disk = disk2
    rows = disk2.node_rows()
    rows.reverse()                             # newest first
    restored = ram2.refill_nodes(rows)
    assert restored == 1
    assert 0x11 in ram2.known_nodes()
    assert 0x10 not in ram2.known_nodes()
    assert disk2.forget_node(0x10) == 0        # older gone BOTH sides


def test_boot_refill_nameless_rows_untouched(tmp_path):
    """Only names collapse at boot - nameless rows restore as-is (the
    live path guards them the same way)."""
    ram, disk = _store(tmp_path)
    now = time.time()
    ram.add_position(0x20, 37.4419, -122.1430, "Named", now=now - 60)
    ram.add_position(0x21, 37.4419, -122.1430, None, now=now)
    disk.close()
    disk2 = type(disk)(str(tmp_path / "twin.db"))
    ram2 = RollingStore(window_seconds=3600.0)
    ram2.disk = disk2
    restored = ram2.refill_nodes(disk2.node_rows())
    assert restored == 2
    assert 0x20 in ram2.known_nodes()
    assert 0x21 in ram2.known_nodes()

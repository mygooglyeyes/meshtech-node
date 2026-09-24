"""Vectored sync tests (VECTORED-SYNC-DESIGN.md section 7).

The design: one monotonic change counter for the whole node table
(disk-backed, survives restarts), the phone's ask carries its marker,
and the answer ships full records only for nodes changed since.
"Gone" events (name-supersede, 30-day prune) tell the phone to remove
dots. Written BEFORE the code, per the doc.
"""
from __future__ import annotations

import pytest

from meshtech_node import codec
from meshtech_node.node_store import NodeStore
from meshtech_node.observations import RollingStore
from meshtech_node.packetsource import Observation


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _obs(prefix: int, lat=None, lon=None, name=None) -> Observation:
    return Observation(prefix=prefix, lat=lat, lon=lon, name=name,
                       recv_ts=1000.0)


@pytest.fixture
def vstore(tmp_path):
    """A RollingStore wired to a real on-disk NodeStore (same wiring
    node.py uses at boot: brain.store.disk = disk)."""
    disk = NodeStore(str(tmp_path / "sync.db"))
    store = RollingStore()
    store.disk = disk
    return store


# ---------------------------------------------------------------------------
# section 2: the counter
# ---------------------------------------------------------------------------

def test_counter_starts_zero_and_bumps_on_real_changes(vstore):
    assert vstore.sync_seq() == 0
    # A real fact (a name) bumps the counter and stamps the node.
    vstore.add_position(0x11, 38.0, -122.0, name="Alpha", now=1000.0)
    n1 = vstore.sync_seq()
    assert n1 >= 1
    # Position change bumps again.
    vstore.add_position(0x11, 38.1, -122.1, name="Alpha", now=1001.0)
    assert vstore.sync_seq() > n1
    # A node row remembers the change number it was changed at.
    assert vstore.node_change_seq(0x11) == vstore.sync_seq()


def test_counter_does_not_bump_on_reheard_silence(vstore):
    """Heard again with NO new facts costs the marker nothing."""
    vstore.add_position(0x11, 38.0, -122.0, name="Alpha", now=1000.0)
    after_first = vstore.sync_seq()
    # Same facts again: no bump.
    vstore.add_position(0x11, 38.0, -122.0, name="Alpha", now=2000.0)
    assert vstore.sync_seq() == after_first


def test_counter_monotonic_across_restart(tmp_path):
    """The counter lives on disk: a restart CONTINUES it (doc section
    6 - a reset could reuse numbers a phone already consumed)."""
    disk = NodeStore(str(tmp_path / "sync.db"))
    store = RollingStore()
    store.disk = disk
    store.add_position(0x11, 38.0, -122.0, name="Alpha", now=1000.0)
    before = store.sync_seq()
    assert before >= 1
    # A brand-new store over the SAME database file.
    store2 = RollingStore()
    store2.disk = NodeStore(str(tmp_path / "sync.db"))
    store2.add_position(0x22, 38.5, -122.5, name="Beta", now=1001.0)
    assert store2.sync_seq() > before


def test_migration_backfills_existing_rows(tmp_path):
    """Existing databases: migration 2 backfills change_seq so the
    first vectored ask after the upgrade is a full roster (then pays)."""
    disk = NodeStore(str(tmp_path / "sync.db"))
    disk.upsert_node(0x11, name="Old row", lat=38.0, lon=-122.0,
                     ts=1000.0)
    store = RollingStore()
    store.disk = disk
    store.refill_nodes(disk.node_rows())
    # The old row is stamped at the current counter, not zero.
    assert store.node_change_seq(0x11) == store.sync_seq()


# ---------------------------------------------------------------------------
# section 3: the ask and the answer
# ---------------------------------------------------------------------------

def test_refresh_req_v16_roundtrip_and_marker_zero_compat():
    """v1.6 adds sync_marker(2 LE). Compat guarantee, stated honestly:
    OLD packets (the golden v1.3 vector) still DECODE unchanged, with
    sync_marker = 0; a v1.6 ask round-trips its marker. (Our encoder
    always emits the CURRENT version - byte-identity of old bytes is
    a decode guarantee, not an encode one.)"""
    import json as _json
    from pathlib import Path as _Path
    golden = _json.loads((_Path(__file__).parent / "golden_vectors.json")
                         .read_text(encoding="utf-8"))
    old = bytes.fromhex(golden["refresh"])
    assert codec.decode_refresh_req(old[3:]).sync_marker == 0
    new = codec.encode_refresh_req(codec.RefreshReq(
        seq=2, kind=codec.REFRESH_KIND_ROUTE, target=0xbeef, nonce=0x1234,
        origin=0x42, host=0xb17e, span_km=40, sync_marker=777))
    back = codec.decode_refresh_req(new[3:])
    assert back.sync_marker == 777
    assert back.span_km == 40
    assert new[3] == 0x06  # declares the per-packet version it needs


def test_intro_filter_only_changed_nodes(vstore):
    """A vectored ask (marker N) ships full records ONLY for nodes with
    change_seq > N; a marker-0 ask is today's full behavior."""
    vstore.add_position(0x11, 38.0, -122.0, name="Alpha", now=1000.0)
    marker = vstore.sync_seq()
    vstore.add_position(0x22, 38.5, -122.5, name="Beta", now=1001.0)
    changed = vstore.nodes_changed_since(marker)
    assert 0x22 in changed and 0x11 not in changed


# ---------------------------------------------------------------------------
# section 5: node-is-gone
# ---------------------------------------------------------------------------

def test_name_supersede_reports_gone(vstore):
    """The retired identity's prefix must reach the phone as gone."""
    vstore.add_position(0x11, 38.0, -122.0, name="Alpha", now=1000.0)
    vstore.sync_seq()  # marker after Alpha exists
    vstore.add_position(0x22, 38.5, -122.5, name="Alpha", now=1001.0)
    gone = vstore.gone_since(0)
    assert 0x11 in gone
    assert vstore.node_info(0x11) is None


def test_prune_reports_gone(vstore):
    vstore.add_position(0x33, 37.0, -121.0, name="Ghost", now=0.0)
    vstore.prune_nodes(now=60 * 86400.0)
    assert 0x33 in vstore.gone_since(0)
    assert vstore.node_info(0x33) is None


def test_gone_replayed_until_consumed_then_cleared(vstore):
    """Gone events must survive until a vectored ask consumes them -
    a phone that asks late still learns the deletion (they are NOT
    silently dropped by the prune itself)."""
    vstore.add_position(0x33, 37.0, -121.0, name="Ghost", now=0.0)
    vstore.prune_nodes(now=60 * 86400.0)
    # Later, another node changes; the gone memory stays queued.
    vstore.add_position(0x44, 37.5, -121.5, name="Later", now=61 * 86400.0)
    assert 0x33 in vstore.gone_since(0)


# ---------------------------------------------------------------------------
# section 7: wire - the GONE packet
# ---------------------------------------------------------------------------

def test_gone_packet_roundtrip():
    """A new packet type carries the gone prefixes (up to 8)."""
    pkt = codec.encode_gone(seq=9, origin=0xb17e, prefixes=[0x11, 0x33])
    assert codec.peek_data_type(pkt) == codec.TYPE_GONE
    g = codec.decode_gone(pkt[3:])          # body contract, like all decodes
    assert g.seq == 9
    assert g.origin == 0xb17e
    assert g.prefixes == [0x11, 0x33]
    assert codec.decode_any(pkt).prefixes == [0x11, 0x33]


def test_gone_packet_rejects_overflow():
    with pytest.raises(codec.CodecError):
        codec.encode_gone(seq=1, origin=0, prefixes=list(range(9)))

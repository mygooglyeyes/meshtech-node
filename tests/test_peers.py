"""Multi-host coexistence: peer table, expiry, owner election, staggered
cadence, and the multi-host refresh rules."""
import time

from meshtech_node import codec
from meshtech_node.config import Settings
from meshtech_node.grid import GridGeometry
from meshtech_node.peers import PeerTable, SectionOwners
from meshtech_node.service import ScopeService


# ---------------------------------------------------------------- peers

def make_layout(origin, lat, lon, grid=3, span_m=40000, seq=1):
    return codec.Layout(seq=seq, grid=grid, center_lat=lat, center_lon=lon,
                        span_m=span_m, origin=origin, name=f"H{origin:04x}")


def test_peer_table_learn_and_expire():
    table = PeerTable(expire_after_seconds=100.0)
    now = 1000.0
    table.observe_layout(make_layout(0x0A, 37.0, -122.0), now=now)
    table.observe_layout(make_layout(0x0B, 38.0, -123.0), now=now)
    assert table.count(now=now) == 2
    # before expiry
    assert table.count(now=now + 99.0) == 2
    # after expiry
    assert table.count(now=now + 101.0) == 0


def test_peer_table_refresh_refreshes_clock():
    table = PeerTable(expire_after_seconds=100.0)
    now = 1000.0
    table.observe_layout(make_layout(0x0A, 37.0, -122.0), now=now)
    table.observe_layout(make_layout(0x0A, 37.0, -122.0), now=now + 90.0)
    assert table.count(now=now + 150.0) == 1   # refreshed, alive
    assert table.count(now=now + 191.0) == 0   # ...until this


# ---------------------------------------------------------------- election

def test_owner_is_lowest_origin_among_covering():
    peers = PeerTable()
    now = time.time()
    # two peers both covering the whole test area
    peers.observe_layout(make_layout(0x20, 37.0, -122.0), now=now)
    peers.observe_layout(make_layout(0x10, 37.0, -122.0), now=now)
    geometry = GridGeometry(grid=3, center_lat=37.0, center_lon=-122.0,
                            span_m=40000)
    owners = SectionOwners(self_origin=0x30, self_geometry=geometry,
                           peers=peers)
    for sid in range(9):
        assert owners.owner_of(sid) == 0x10   # lowest origin wins


def test_owner_prefers_self_on_tie():
    peers = PeerTable()
    now = time.time()
    peers.observe_layout(make_layout(0x20, 37.0, -122.0), now=now)
    geometry = GridGeometry(grid=3, center_lat=37.0, center_lon=-122.0,
                            span_m=40000)
    owners = SectionOwners(self_origin=0x20, self_geometry=geometry,
                           peers=peers)
    assert owners.owner_of(4) == 0x20       # equal origin = self, self wins


def test_owner_partitions_non_overlapping_areas():
    peers = PeerTable()
    now = time.time()
    # peer covers the western half, self covers the eastern half
    peers.observe_layout(make_layout(0x10, 37.0, -122.11), now=now)
    geometry = GridGeometry(grid=3, center_lat=37.0, center_lon=-122.0,
                            span_m=40000)
    owners = SectionOwners(self_origin=0x20, self_geometry=geometry,
                           peers=peers)
    # sections 0,3,6 are the west column -> peer's area
    assert owners.owner_of(0) == 0x10
    assert owners.owner_of(3) == 0x10
    assert owners.owner_of(6) == 0x10
    # east column -> self
    assert owners.owner_of(2) == 0x20
    assert owners.owner_of(8) == 0x20


def test_far_peer_cannot_steal_own_grid():
    """A host always covers its own grid (its sections are relative to
    its own LAYOUT), so a peer whose area lies elsewhere never wins -
    even with a lower origin."""
    peers = PeerTable()
    now = time.time()
    peers.observe_layout(make_layout(0x10, 38.0, -123.0), now=now)  # elsewhere
    geometry = GridGeometry(grid=3, center_lat=37.0, center_lon=-122.0,
                            span_m=40000)
    owners = SectionOwners(self_origin=0x30, self_geometry=geometry,
                           peers=peers)
    assert owners.owner_of(4) == 0x30


def test_overlapping_edge_sections_elect_lowest():
    """Sections at the overlap edge go to the lowest covering origin,
    physical containment decides who covers."""
    peers = PeerTable()
    now = time.time()
    peers.observe_layout(make_layout(0x10, 37.0, -122.11), now=now)  # shifted west
    geometry = GridGeometry(grid=3, center_lat=37.0, center_lon=-122.0,
                            span_m=40000)
    owners = SectionOwners(self_origin=0x30, self_geometry=geometry,
                           peers=peers)
    # east column is outside the peer's square -> self only
    assert owners.owner_of(2) == 0x30
    assert owners.owner_of(5) == 0x30
    assert owners.owner_of(8) == 0x30
    # west column centre is inside both squares -> peer wins (lower id)
    assert owners.owner_of(0) == 0x10


def test_stale_peer_loses_ownership():
    peers = PeerTable(expire_after_seconds=100.0)
    now = time.time()
    peers.observe_layout(make_layout(0x10, 37.0, -122.0), now=now)
    geometry = GridGeometry(grid=3, center_lat=37.0, center_lon=-122.0,
                            span_m=40000)
    owners = SectionOwners(self_origin=0x30, self_geometry=geometry,
                           peers=peers)
    assert owners.owner_of(4) == 0x10          # peer owns it while fresh
    assert owners.owner_of(4, now=now + 200.0) == 0x30  # stale -> self takes over


# ---------------------------------------------------------------- service

class FakeRadio:
    def __init__(self):
        self.sent = []

    async def send_channel_data(self, data_type: int, payload: bytes) -> bool:
        self.sent.append((data_type, bytes(payload)))
        return True


def make_service(multi_host=False, origin=0x30, **feed_overrides):
    settings = Settings()
    settings.area.center_lat = 37.0
    settings.area.center_lon = -122.0
    settings.feed.burst_gap_seconds = 0.0
    settings.feed.multi_host = multi_host
    for key, value in feed_overrides.items():
        setattr(settings.feed, key, value)
    return ScopeService(settings, use_demo=True, origin=origin)


def test_service_answers_single_host():
    radio = FakeRadio()
    svc = make_service(multi_host=False)
    svc.client = radio
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION, target=4,
                           nonce=1)
    import asyncio
    asyncio.run(svc.on_packet(req, "aabbccddeeff"))
    assert len(radio.sent) > 0


def test_multi_host_owner_answers():
    radio = FakeRadio()
    svc = make_service(multi_host=True, origin=0x10)
    svc.client = radio
    # a peer with a HIGHER origin also covering the area
    svc.peers.observe_layout(make_layout(0x20, 37.0, -122.0))
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION, target=4,
                           nonce=1)
    import asyncio
    asyncio.run(svc.on_packet(req, "aabbccddeeff"))
    assert len(radio.sent) > 0      # 0x10 < 0x20 -> we own it, we answer


def test_multi_host_non_owner_stays_silent():
    radio = FakeRadio()
    svc = make_service(multi_host=True, origin=0x20)
    svc.client = radio
    # a peer with a LOWER origin covering the same area
    svc.peers.observe_layout(make_layout(0x10, 37.0, -122.0))
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION, target=4,
                           nonce=1)
    import asyncio
    asyncio.run(svc.on_packet(req, "aabbccddeeff"))
    assert radio.sent == []         # peer owns it; we stay silent


def test_multi_host_no_peers_still_answers():
    """Multi-host mode with zero known peers behaves like single-host:
    a host always owns its own grid."""
    radio = FakeRadio()
    svc = make_service(multi_host=True, origin=0x30)
    svc.client = radio
    assert svc.peers.count() == 0
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION, target=4,
                           nonce=1)
    import asyncio
    asyncio.run(svc.on_packet(req, "aabbccddeeff"))
    assert len(radio.sent) > 0


def test_directed_refresh_other_host_silence():
    radio = FakeRadio()
    svc = make_service(multi_host=False, origin=0x30)
    svc.client = radio
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION, target=4,
                           nonce=1, host=0x99)
    import asyncio
    asyncio.run(svc.on_packet(req, "aabbccddeeff"))
    assert radio.sent == []         # aimed at another host


def test_multi_host_route_refresh_partition():
    radio = FakeRadio()
    svc = make_service(multi_host=True, origin=0x20)
    svc.client = radio
    # lower-origin peer covering the whole area -> we own nothing
    svc.peers.observe_layout(make_layout(0x10, 37.0, -122.0))
    now = time.time()
    svc.store.add(type("O", (), {
        "recv_ts": now, "origin_ts": now - 1.0, "prefix": 0x71,
        "lat": 37.0, "lon": -122.0, "path_prefixes": [0x33, 0x44],
        "channel_name": None, "delay_s": 1.0})())
    from meshtech_node.feedbuilder import route_id
    rid = route_id((0x33, 0x44))
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_ROUTE, target=rid,
                           nonce=2)
    import asyncio
    asyncio.run(svc.on_packet(req, "aabbccddeeff"))
    assert radio.sent == []         # the route lives in peer-owned sections


def test_own_layout_echo_ignored():
    svc = make_service(multi_host=True, origin=0x30)
    own = make_layout(0x30, 37.0, -122.0)
    svc.on_packet.__wrapped__ if False else None
    import asyncio
    asyncio.run(svc.on_packet(own, "aabbccddeeff"))
    assert svc.peers.count() == 0


def test_refresh_defaults_dont_break_old_flow():
    """A v1-style refresh (no host field on the wire) still works."""
    raw = bytes.fromhex("11530801020002efbe3412")
    req = codec.decode_any(raw)
    assert req.host == codec.REFRESH_HOST_ANY
    assert req.target == 0xBEEF
    assert req.nonce == 0x1234

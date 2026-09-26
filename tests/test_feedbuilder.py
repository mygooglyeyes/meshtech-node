"""Feed builder tests: honest aggregation, refresh responses, snapshot."""
import time

from meshtech_node import codec
from meshtech_node.budget import BudgetLimiter
from meshtech_node.config import AreaCfg, FeedCfg, RadioCfg, Settings
from meshtech_node.feedbuilder import FeedBuilder, _route_id
from meshtech_node.grid import geometry_from
from meshtech_node.observations import Observation, RollingStore


def make_builder(section_span_deg=0.36 / 3, span_km=40.0):
    settings = Settings(area=AreaCfg(name="Test", center_lat=37.0,
                                     center_lon=-122.0, span_km=span_km,
                                     grid=3),
                        feed=FeedCfg(burst_gap_seconds=0.2))
    store = RollingStore(window_seconds=3600.0)
    geo = geometry_from(37.0, -122.0, span_km * 1000.0, 3)
    budget = BudgetLimiter(RadioCfg(), 24, 1.0)
    return FeedBuilder(settings, store, geo, budget), store


# --------------------------------------------- v1.3 sized refresh answers

def test_sized_whole_area_trims_layout_and_sections():
    """A 20 km window over a 60 km home box: the LAYOUT announces 20 km,
    only the overlapping home sections fly, names ride the window's
    own center (the zero-dots lesson: offsets are window-relative)."""
    builder, store = make_builder(span_km=60.0)
    now = time.time()
    # two nodes: one at the center (inside every window), one at the
    # far NE corner of the 60 km box (outside a 20 km window). Nodes
    # enter the intro/node table via adverts (add_position), which is
    # what an on-air advert does.
    store.add(Observation(recv_ts=now, origin_ts=now - 2.0,
                          prefix=0x10, lat=37.0005, lon=-122.0005,
                          path_prefixes=[]), now=now)
    corner = 60.0 / 2.0 / 111.32  # ~0.269 deg N and E of center
    store.add(Observation(recv_ts=now, origin_ts=now - 2.0,
                          prefix=0x11, lat=37.0 + corner,
                          lon=-122.0 + corner, path_prefixes=[]), now=now)
    store.add_position(0x10, 37.0005, -122.0005, name="Center", now=now)
    store.add_position(0x11, 37.0 + corner, -122.0 + corner,
                       name="Corner", now=now)
    pkts = builder.build_refresh_response(
        codec.REFRESH_KIND_SECTION, codec.REFRESH_WHOLE_AREA,
        now=now, span_km=20.0)
    kinds = [p.data_type for p in pkts]
    layout = codec.decode_layout(
        next(p for p in pkts if p.data_type == codec.TYPE_LAYOUT).payload[3:])
    assert layout.span_m == 20000          # the WINDOW, not the home box
    assert layout.center_lat == 37.0       # same center
    assert layout.center_lon == -122.0
    # sections carried: only the home squares overlapping 20 km. A 20 km
    # window on a 60 km 3x3 grid (20 km squares) overlaps exactly the
    # CENTER square (5) - the window IS one home square here.
    assert kinds.count(codec.TYPE_SECT_SUM) == 1
    sect = codec.decode_sect_sum(
        next(p for p in pkts if p.data_type == codec.TYPE_SECT_SUM).payload[3:])
    assert sect.section_id == 5            # the center square
    assert sect.active_nodes == 1          # only the center node
    intro = codec.decode_intro(
        next(p for p in pkts if p.data_type == codec.TYPE_INTRO).payload[3:],
        center_lat=layout.center_lat, center_lon=layout.center_lon,
        span_m=layout.span_m)  # the fallback field: unused since v1.5
    # THE RULER (Brett's fix, 2026-09-26): the offsets are measured
    # against a ruler that reaches every node (never smaller than the
    # home box) - the window no longer sets the offset scale, so no
    # position is ever pinned at a window edge.
    assert intro.span_m >= 60000
    # last packet is the live PULSE (service appends it; builder alone
    # does not - the service layer owns that rule)
    assert codec.TYPE_PULSE not in kinds


def test_span_snaps_and_clamps_to_home_box():
    """45 km -> 40; 999 km -> clamped to the home box (60); 0/absent ->
    the home box itself, byte-identical behavior to before v1.3."""
    builder, _store = make_builder(span_km=60.0)
    geo40, km40 = builder._sized_geometry(45.0)
    assert km40 == 40.0 and geo40.span_m == 40000
    geo_big, km_big = builder._sized_geometry(999.0)
    assert km_big == 60.0 and geo_big.span_m == 60000
    geo0, km0 = builder._sized_geometry(0)
    assert km0 == 60.0 and geo0 is builder.geometry


def test_unsized_refresh_is_whole_home_box():
    """span_km omitted (old clients, and the service path that has not
    been updated): the answer covers the WHOLE home box exactly as
    before v1.3 - no trimming by accident."""
    builder, store = make_builder(span_km=40.0)
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=now - 2.0,
                          prefix=0x10, lat=37.0, lon=-122.0,
                          path_prefixes=[]), now=now)
    pkts = builder.build_refresh_response(
        codec.REFRESH_KIND_SECTION, codec.REFRESH_WHOLE_AREA, now=now)
    layout = codec.decode_layout(
        next(p for p in pkts if p.data_type == codec.TYPE_LAYOUT).payload[3:])
    assert layout.span_m == 40000
    # layout + 9 sections, NO intro batch: the observation alone puts
    # the node on no intro roster (roster entries come from adverts,
    # add_position) - same as pre-v1.3 behavior for this store state
    assert len(pkts) == 1 + 9


def test_pulse_reflects_observations():
    builder, store = make_builder()
    now = time.time()
    for i in range(5):
        store.add(Observation(recv_ts=now, origin_ts=now - 2.0,
                              prefix=0x10 + i, lat=37.001, lon=-122.001,
                              path_prefixes=[]), now=now)
    pkt = builder.build_pulse(now=now)
    pulse = codec.decode_pulse(pkt.payload[3:])
    assert pulse.active_total == 5
    # v1.2 1-based: centre section = 5 -> array index 4 (unchanged wire:
    # counts are in NW->SE order; only the ids shifted)
    assert pulse.section_counts[4] == 5
    assert pkt.data_type == codec.TYPE_PULSE


def test_pulse_never_counts_unknown_sender_as_node():
    # PROJECT.md rule 3 + phantom-node guard: group traffic carries no
    # sender identity (prefix=0). With all-traffic recording those rows
    # are the majority - they must inflate rx/hour, never "active nodes".
    builder, store = make_builder()
    now = time.time()
    for i in range(4):
        store.add(Observation(recv_ts=now, origin_ts=None,
                              prefix=0x10 + i, lat=None, lon=None,
                              path_prefixes=[0xAB]), now=now)
    for _ in range(20):                     # heavy foreign chatter
        store.add(Observation(recv_ts=now, origin_ts=None,
                              prefix=0, lat=None, lon=None,
                              path_prefixes=[0xAB]), now=now)
    pkt = builder.build_pulse(now=now)
    pulse = codec.decode_pulse(pkt.payload[3:])
    assert pulse.active_total == 4          # NOT 24
    assert pulse.rx_per_hour == 24          # traffic IS counted


def test_active_prefixes_skip_unknown_sender():
    # The store's section grouping skips prefix=0 too (no phantom node
    # in any section list).
    from meshtech_node.observations import RollingStore
    store = RollingStore()
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=None, prefix=0x42,
                          lat=37.0, lon=-122.0, path_prefixes=[]), now=now)
    store.add(Observation(recv_ts=now, origin_ts=None, prefix=0,
                          lat=None, lon=None, path_prefixes=[0xAB]), now=now)
    groups = store.active_prefixes(lambda o: 1, now=now)
    assert groups == {1: [0x42]}            # no "0" key, no phantom


def test_section_stats_and_routes():
    builder, store = make_builder()
    now = time.time()
    # 3 packets through the same 2-hop route, plus 1 stray
    for i in range(3):
        store.add(Observation(recv_ts=now, origin_ts=now - 1.0 - i,
                              prefix=0x40, lat=37.0, lon=-122.0,
                              path_prefixes=[0x11, 0x22]), now=now)
    store.add(Observation(recv_ts=now, origin_ts=None, prefix=0x41,
                          lat=37.0, lon=-122.0, path_prefixes=[0x11]),
              now=now)
    pkt = builder.build_sect_sum(5, now=now)   # centre square (v1.2)
    sect = codec.decode_sect_sum(pkt.payload[3:])
    assert sect.section_id == 5
    assert sect.active_nodes == 2
    assert sect.packet_count == 4
    assert sect.delay_p50_s > 0          # honest origin stamps -> delay
    assert len(sect.route_stubs) == 2
    assert sect.route_stubs[0] == _route_id((0x11, 0x22))  # busiest first


def test_negative_delay_is_never_published():
    builder, store = make_builder()
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=now + 300.0,  # clock skew
                          prefix=0x50, lat=37.0, lon=-122.0,
                          path_prefixes=[0x11]), now=now)
    pkt = builder.build_sect_sum(5, now=now)
    sect = codec.decode_sect_sum(pkt.payload[3:])
    assert sect.delay_p50_s == 0         # unknown, not a fabricated number


def test_refresh_section_response_shape():
    builder, store = make_builder()
    now = time.time()
    for i in range(4):
        store.add(Observation(recv_ts=now, origin_ts=now - 2.0,
                              prefix=0x60 + i, lat=37.0, lon=-122.0,
                              path_prefixes=[0x0A]), now=now)
        store.add_position(0x60 + i, 37.0 + i * 0.001, -122.0,
                           f"N{i}", now=now)
    packets = builder.build_refresh_response(codec.REFRESH_KIND_SECTION, 5,
                                             now=now)
    types = [p.data_type for p in packets]
    assert types[0] == codec.TYPE_SECT_SUM
    assert codec.TYPE_ROUTE in types
    assert codec.TYPE_INTRO in types
    for p in packets:
        assert len(p.payload) <= codec.TARGET_PAYLOAD


def test_refresh_whole_area_includes_layout():
    """target 0 = WHOLE-AREA: LAYOUT first (grid geometry so a client
    can draw immediately), then one summary per section, then names.
    2026-09-18: target 0 fell through as section 0 and a client's
    'Refresh map' could never fetch the layout."""
    builder, store = make_builder()
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=now - 2.0, prefix=0x61,
                          lat=37.0, lon=-122.0, path_prefixes=[0x0A]),
              now=now)
    packets = builder.build_refresh_response(codec.REFRESH_KIND_SECTION, 0,
                                             now=now)
    types = [p.data_type for p in packets]
    assert types[0] == codec.TYPE_LAYOUT
    assert types.count(codec.TYPE_SECT_SUM) == builder.geometry.section_count
    # INTRO batch appears only when positioned named nodes exist -
    # the builder never fabricates names to fill a packet (honesty).
    # With a lone unnamed observation here, it is legitimately absent.
    # assert codec.TYPE_INTRO in types


def test_refresh_route_by_id():
    builder, store = make_builder()
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=now - 1.0, prefix=0x70,
                          lat=37.0, lon=-122.0, path_prefixes=[0x33, 0x44]),
              now=now)
    rid = _route_id((0x33, 0x44))
    packets = builder.build_refresh_response(codec.REFRESH_KIND_ROUTE, rid,
                                             now=now)
    assert len(packets) == 1
    route = codec.decode_route(packets[0].payload[3:])
    assert route.prefixes == [0x33, 0x44]
    assert route.route_id == rid


def test_snapshot_splits_within_budget():
    builder, store = make_builder()
    now = time.time()
    for i in range(30):
        lat = 36.85 + (i % 10) * 0.015
        lon = -122.15 + (i // 10) * 0.015
        store.add(Observation(recv_ts=now, origin_ts=now - 1.0,
                              prefix=0x80 + i, lat=lat, lon=lon,
                              path_prefixes=[0x11]), now=now)
        store.add_position(0x80 + i, lat, lon, f"Node{i}", now=now)
    parts = builder.build_snapshot(now=now)
    assert len(parts) >= 2
    for p in parts:
        assert p.data_type == codec.TYPE_SNAP
        assert len(p.payload) <= codec.TARGET_PAYLOAD + 3
    snap0 = codec.decode_snap(parts[0].payload[3:])
    snap_last = codec.decode_snap(parts[-1].payload[3:])
    assert snap0.snap_id == snap_last.snap_id
    assert snap0.part == 0
    assert snap_last.part == len(parts) - 1
    assert snap_last.parts == len(parts)


def test_intro_batch_respects_byte_budget():
    builder, store = make_builder()
    now = time.time()
    for i in range(50):
        store.add_position(0x10 + i, 37.0 + i * 0.0001, -122.0,
                           f"VeryLongNodeName{i:02d}", now=now)
    pkt = builder.build_intro_batch(now=now)
    assert pkt is not None
    assert len(pkt.payload) <= 110       # MAX_INTRO_BYTES + framing
    intro = codec.decode_intro(pkt.payload[3:], center_lat=37.0,
                               center_lon=-122.0, span_m=40000.0)
    assert 0 < len(intro.entries) < 50   # rotated, not all at once

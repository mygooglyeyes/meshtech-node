"""Feed builder tests: honest aggregation, refresh responses, snapshot."""
import time

from meshtech_node import codec
from meshtech_node.budget import BudgetLimiter
from meshtech_node.config import AreaCfg, FeedCfg, RadioCfg, Settings
from meshtech_node.feedbuilder import FeedBuilder, _route_id
from meshtech_node.grid import geometry_from
from meshtech_node.observations import Observation, RollingStore


def make_builder(section_span_deg=0.36 / 3):
    settings = Settings(area=AreaCfg(name="Test", center_lat=37.0,
                                     center_lon=-122.0, span_km=40.0,
                                     grid=3),
                        feed=FeedCfg(burst_gap_seconds=0.2))
    store = RollingStore(window_seconds=3600.0)
    geo = geometry_from(37.0, -122.0, 40000.0, 3)
    budget = BudgetLimiter(RadioCfg(), 24, 1.0)
    return FeedBuilder(settings, store, geo, budget), store


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

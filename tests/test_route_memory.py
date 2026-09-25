"""ROUTE MEMORY (2026-09-24, Brett's laws after the RAM-only mistake):

- A route is ANY persistent path between two nodes - a packet heard
  DIRECT (no repeaters) is a one-hop route, never thrown away again.
- Routes are SAVED TO DISK (the same database as the nodes) and
  refilled at boot: a restart no longer wipes the route table.
- The fade mirrors the phone's colors:
    DIRECT routes:    silent 3 days -> STALE, 7 days -> DEAD (deleted)
    MULTI-HOP routes: silent 7 days -> STALE, 14 days -> DEAD (deleted)
- A route's death never erases a NODE (nodes keep their own 14/30 law).
- Every route carries its measured time start-to-end (median of honest
  origin stamps; 0 = unknown, never a fabricated number).
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node import codec
from meshtech_node.budget import BudgetLimiter
from meshtech_node.config import AreaCfg, FeedCfg, RadioCfg, Settings
from meshtech_node.feedbuilder import FeedBuilder, _route_id
from meshtech_node.grid import geometry_from
from meshtech_node.node_store import NodeStore
from meshtech_node.observations import Observation, RollingStore

import pytest

DAY = 86400.0


def builder_section_of_for(lat: float, lon: float):
    """A section function for stores built outside make_builder (the
    boot-refill test); square 5 = the centre of the test grid."""
    return lambda obs: 5


def make_builder(span_km=40.0, disk=None):
    settings = Settings(area=AreaCfg(name="Test", center_lat=37.0,
                                     center_lon=-122.0, span_km=span_km,
                                     grid=3),
                        feed=FeedCfg(burst_gap_seconds=0.2))
    store = RollingStore(window_seconds=3600.0, disk=disk)
    geo = geometry_from(37.0, -122.0, span_km * 1000.0, 3)
    budget = BudgetLimiter(RadioCfg(), 24, 1.0)
    builder = FeedBuilder(settings, store, geo, budget)
    # mirror the service's wiring: the store stamps each heard packet
    # with the square it was heard in (route sections survive restarts)
    store.section_of = builder._section_of
    return builder, store


@pytest.fixture()
def disk(tmp_path):
    s = NodeStore(str(tmp_path / "routes.db"))
    yield s
    s.close()


# ------------------------------------------------------------- direct routes

def test_direct_packet_forms_a_route():
    """A packet with NO repeaters is a route: sender -> heard here,
    one hop. The old code skipped these entirely."""
    builder, store = make_builder()
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=now - 1.0, prefix=0x10,
                          lat=37.0, lon=-122.0, path_prefixes=[]), now=now)
    routes = builder._routes_for_section(5, now=now)
    assert len(routes) == 1
    path, _count, _delays, _last = routes[0]
    assert path == (0x10,)          # the one-hop trail: the sender itself


def test_direct_route_flies_in_sect_sum():
    builder, store = make_builder()
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=None, prefix=0x12,
                          lat=37.0, lon=-122.0, path_prefixes=[]), now=now)
    pkt = builder.build_sect_sum(5, now=now)
    sect = codec.decode_sect_sum(pkt.payload[3:])
    assert sect.route_stubs == [_route_id((0x12,))]


# ---------------------------------------------------------- routes on disk

def test_routes_written_through_to_disk(disk):
    """A route formed in RAM lands on disk immediately; a fresh store
    refills it - the restart no longer wipes the route table."""
    builder, store = make_builder(disk=disk)
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=now - 1.0, prefix=0x21,
                          lat=37.0, lon=-122.0, path_prefixes=[]),
              now=now)                       # DIRECT: sender 0x21 known
    store.add_position(0x21, 37.0, -122.0, name="Anchor", now=now)
    rid = _route_id((0x21,))
    rows = disk.route_rows()
    assert "21" in [r["path_hex"] for r in rows]
    assert rows[0]["sender_prefix"] == 0x21  # the anchor survives on disk
    # The RAM table dies with the process; disk remembers.
    store2 = RollingStore(window_seconds=3600.0, disk=disk)
    restored = store2.refill_routes(disk.route_rows())
    assert restored == 1
    builder2, _ = make_builder(disk=disk)
    builder2.store = store2
    # No positions yet at boot: the route exists but its section is
    # honestly unknown - never guessed.
    assert builder2._section_of_path((0x21,)) == -1
    builder2.store.add_position(0x21, 37.0, -122.0, name="Anchor",
                                now=now)
    assert builder2._section_of_path((0x21,)) == 5
    routes = builder2._routes_for_section(5, now=now)
    assert [p for p, _, _, _ in routes] == [(0x21,)]


def test_multihop_route_anchors_on_positioned_hop(disk):
    """Wire truth: a path-bearing packet carries no origin identity,
    so a multi-hop route anchors on its first POSITIONED hop."""
    builder, store = make_builder(disk=disk)
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=None, prefix=0,
                          lat=None, lon=None, path_prefixes=[0xAB, 0xCD]),
              now=now)
    store.add_position(0xAB, 37.0, -122.0, name="RelayA", now=now)
    assert builder._section_of_path((0xAB, 0xCD)) == 5


def test_route_refill_preserves_age_and_delays(disk):
    """A restored route keeps its ORIGINAL last-heard time (a week-silent
    route is still a week silent - no resurrection at boot) and its
    median delay (the timing Brett asked for survives restarts)."""
    now = time.time()
    store = RollingStore(window_seconds=3600.0, disk=disk)
    store.section_of = builder_section_of_for(37.0, -122.0)
    store.add(Observation(recv_ts=now, origin_ts=now - 4.0, prefix=0x22,
                          lat=37.0, lon=-122.0, path_prefixes=[]), now=now)
    rows = disk.route_rows()
    assert rows[0]["delay_med_s"] == 4      # measured, honest
    store2 = RollingStore(window_seconds=3600.0, disk=disk)
    store2.section_of = builder_section_of_for(37.0, -122.0)
    store2.refill_routes(rows)
    # hear again later: the count/delay grow from the disk base
    later = now + 100.0
    builder2, _ = make_builder(disk=disk)
    builder2.store = store2
    store2.add(Observation(recv_ts=later, origin_ts=later - 2.0,
                           prefix=0x22, lat=37.0, lon=-122.0,
                           path_prefixes=[]), now=later)
    # the restored route has no node-table position yet (a boot refill
    # brings routes and nodes back in separate calls) - anchor it the
    # way an advert does, then read the section
    store2.add_position(0x22, 37.0, -122.0, name="Anchor2", now=later)
    routes = builder2._routes_for_section(5, now=later)
    path, count, delays, last = routes[0]
    assert count == 2 and delays == [2.0, 4.0]
    assert last == pytest.approx(later)


# ------------------------------------------------------------------ the fade

def _route_at(store, prefix, path, ts, delay=1.0, *, positioned=True):
    store.add(Observation(recv_ts=ts, origin_ts=ts - delay, prefix=prefix,
                          lat=37.0 if positioned else None,
                          lon=-122.0 if positioned else None,
                          path_prefixes=path),
              now=ts)
    if positioned:
        # a positioned advert (what puts the node - and so the route -
        # on the map grid)
        store.add_position(prefix, 37.0, -122.0, now=ts)


def test_direct_route_fade_3_7(disk):
    """DIRECT law: silent 3 days -> STALE, silent 7 days -> DEAD."""
    builder, store = make_builder(disk=disk)
    now = time.time()
    _route_at(store, 0x30, [], now - 1 * DAY)
    _route_at(store, 0x31, [], now - 4 * DAY)
    _route_at(store, 0x32, [], now - 8 * DAY)   # dead before the prune
    stale, dead = store.prune_routes(now=now)
    assert (stale, dead) == (1, 1)
    # fresh + stale serve; the dead one is gone from RAM and disk
    routes = builder._routes_for_section(5, now=now)
    ages = sorted(int(now - last) // DAY for _, _, _, last in routes)
    assert ages == [1, 4]
    assert disk.route_rows() and \
        all(int(now - r["last_heard"]) // DAY <= 4 for r in disk.route_rows())


def test_multihop_route_fade_7_14(disk):
    """MULTI-HOP law: silent 7 days -> STALE, silent 14 days -> DEAD."""
    builder, store = make_builder(disk=disk)
    now = time.time()
    # three DISTINCT trails (routes are keyed by path, not sender)
    _route_at(store, 0x40, [0xAB], now - 6 * DAY)
    _route_at(store, 0x41, [0xAC], now - 10 * DAY)
    _route_at(store, 0x42, [0xAD], now - 15 * DAY)
    stale, dead = store.prune_routes(now=now)
    assert (stale, dead) == (1, 1)
    routes = builder._routes_for_section(5, now=now)
    ages = sorted(int(now - last) // DAY for _, _, _, last in routes)
    assert ages == [6, 10]


def test_route_heard_again_is_reborn(disk):
    """The moment a route is used again it leaves the stale state (the
    phone's yellow list is derived from age; a fresh age IS fresh)."""
    builder, store = make_builder(disk=disk)
    old = time.time() - 5 * DAY
    _route_at(store, 0x50, [], old)
    store.prune_routes(now=old + 60)
    now = time.time()
    _route_at(store, 0x50, [], now)          # heard again
    routes = builder._routes_for_section(5, now=now)
    assert len(routes) == 1
    assert now - routes[0][3] < 60           # young again


def test_route_death_never_erases_nodes(disk):
    """A 7-day-dead DIRECT route must not touch the node table (nodes
    keep their own 14/30 law; the dot stays on the map)."""
    builder, store = make_builder(disk=disk)
    old = time.time() - 8 * DAY
    _route_at(store, 0x60, [], old)
    store.add_position(0x60, 37.0, -122.0, "Survivor", now=old)
    store.prune_routes(now=time.time())
    assert store.node_info(0x60) is not None
    assert store.active_nodes_ever() == 1


def test_prune_routes_is_called_by_service_cadence():
    """The fade must actually RUN (the repeater-table audit lesson: a
    law that is never called is not a law). The layout cadence calls
    prune_routes alongside prune_nodes."""
    from meshtech_node.service import ScopeService
    settings = Settings(area=AreaCfg(name="T", center_lat=37.0,
                                     center_lon=-122.0, span_km=40.0,
                                     grid=3))
    svc = ScopeService(settings)
    now = time.time()
    _route_at(svc.store, 0x70, [0xCD], now - 20 * DAY)
    counts = svc.store.prune_routes(now=now)
    assert counts == (0, 1)                  # stale, forgotten


# ------------------------------------------------------------------ timing

def test_route_timing_median_on_wire():
    """The route's start-to-end time rides the wire (median of honest
    origin stamps); no stamps = 0 = the phone shows 'unknown'."""
    builder, store = make_builder()
    now = time.time()
    _route_at(store, 0x80, [0xEF], now, delay=3.0)
    _route_at(store, 0x81, [0xEF], now, delay=9.0)
    rid = _route_id((0xEF,))
    packets = builder.build_refresh_response(codec.REFRESH_KIND_ROUTE, rid,
                                             now=now)
    route = codec.decode_route(packets[0].payload[3:])
    # the wire's p50 rule (the builder's existing percentile, same as
    # the section summaries use): [3, 9] -> 9
    assert route.delay_med_s == 9
    assert route.packet_count == 2
    assert route.prefixes == [0xEF]
    # a direct route's timing comes through the same path
    _route_at(store, 0x82, [], now, delay=2.0)
    rid1 = _route_id((0x82,))
    packets = builder.build_refresh_response(codec.REFRESH_KIND_ROUTE, rid1,
                                             now=now)
    route = codec.decode_route(packets[0].payload[3:])
    assert route.delay_med_s == 2


# -------------------------------- sender + trail anchor (v00.000.047)


def test_multihop_route_records_its_sender(disk):
    """Brett (2026-09-25): EVERY route records who sent it - not just
    one-hop routes. The sender is the anchor the boot re-check looks
    up, so a multi-hop row must store it too."""
    builder, store = make_builder(disk=disk)
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=None, prefix=0x77,
                          lat=None, lon=None,
                          path_prefixes=[0xAB, 0xCD]), now=now)
    rows = disk.route_rows()
    assert len(rows) == 1
    assert rows[0]["path_hex"] == "abcd"
    assert rows[0]["sender_prefix"] == 0x77   # stored, not 0


def test_unplaced_route_is_held_from_every_section():
    """Brett's HOLD rule (2026-09-25): a route nobody can place (no
    sender position, no trail end) sits in NO square - it is never
    guessed onto any section's list."""
    builder, store = make_builder()
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=None, prefix=0x99,
                          lat=None, lon=None,
                          path_prefixes=[0xAA, 0xBB]), now=now)
    assert store.route_entry((0xAA, 0xBB))["section"] == -1
    for sid in range(1, builder.geometry.section_count + 1):
        assert builder._routes_for_section(sid, now=now) == []


def test_trail_far_end_places_the_route():
    """Brett's recipient rule (2026-09-25): sender not mapped -> the
    trail's FAR END (the last repeater we heard send it) places the
    route. Facts can arrive later; the next hearing stamps them in."""
    builder, store = make_builder()
    now = time.time()
    obs = Observation(recv_ts=now, origin_ts=None, prefix=0x90,
                      lat=None, lon=None, path_prefixes=[0xAB, 0xCD])
    store.add(obs, now=now)
    assert store.route_entry((0xAB, 0xCD))["section"] == -1   # held
    # the far end becomes known (its advert finally heard)
    store.add_position(0xCD, 37.0, -122.0, name="LastHop", now=now)
    later = now + 1.0
    store.add(obs, now=later)          # heard again with facts present
    assert store.route_entry((0xAB, 0xCD))["section"] == 5
    assert [p for p, _, _, _ in
            builder._routes_for_section(5, now=later)] == [(0xAB, 0xCD)]


def test_startup_reanchor_places_stored_routes(disk):
    """Brett's v00.000.047 boot re-check: saved routes re-examine
    their square once node facts are back - a sender placed since the
    last run gives the route its square; the unplaceable one stays
    honestly held (-1), never guessed."""
    builder, store = make_builder(disk=disk)
    now = time.time()
    store.add(Observation(recv_ts=now, origin_ts=None, prefix=0x21,
                          lat=None, lon=None, path_prefixes=[]), now=now)
    store.add(Observation(recv_ts=now, origin_ts=None, prefix=0x33,
                          lat=None, lon=None, path_prefixes=[]), now=now)
    assert {r["section_id"] for r in disk.route_rows()} == {-1}
    # BOOT: fresh store + refill + the injected path resolver
    store2 = RollingStore(window_seconds=3600.0, disk=disk)
    store2.section_of = builder_section_of_for(37.0, -122.0)
    builder2, _ = make_builder(disk=disk)
    builder2.store = store2
    store2.path_section_of = builder2._section_of_path
    store2.refill_routes(disk.route_rows())
    # node facts arrive before the re-check (nodes refill first at boot)
    store2.add_position(0x21, 37.0, -122.0, name="Anchor", now=now)
    assert store2.reanchor_routes() == 1
    assert store2.route_entry((0x21,))["section"] == 5
    assert store2.route_entry((0x33,))["section"] == -1   # still held
    rows = {r["path_hex"]: r["section_id"] for r in disk.route_rows()}
    assert rows == {"21": 5, "33": -1}                    # disk moved too

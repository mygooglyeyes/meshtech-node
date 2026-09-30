"""THE DOOR IS THE FULL DATA DUMP (Brett, 2026-09-29: "TCP is full
data dump, BLE carries the updates only").

The bench bug that named this chapter: a WiFi phone showed "no chart
yet" on every card while the node was happily transmitting clinic
batches over the air - the clinic book only ever rode the radio
cadence, so a door (TCP) client could never see a chart. Pinned
here:

1. The door's connect burst carries the WHOLE clinic book in
   wire-legal pages (<= 7 records, <= 163 B each).
2. A door ask's answer carries the asked square's charts (or the
   asked route's chart); a whole-area ask gets the whole book.
3. The AIR is unchanged: clinic never rides radio answers or the
   air connect pulse - the rotating cadence slices stay the air's
   only clinic path ("BLE carries the updates only").
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node import codec  # noqa: E402
from meshtech_node.config import Settings  # noqa: E402
from meshtech_node.feedbuilder import _route_id  # noqa: E402
from meshtech_node.observations import Observation  # noqa: E402
from meshtech_node.service import ScopeService  # noqa: E402


class FakeRadio:
    """Records every send (the radio-path spy)."""

    is_connected = True
    has_slot = True

    def __init__(self):
        self.sent = []

    async def send_channel_data(self, data_type: int, payload: bytes) -> bool:
        self.sent.append((data_type, bytes(payload)))
        return True


class FakeTap:
    """Records what the door was handed (the WebServe FeedTap spy)."""

    def __init__(self):
        self.served = []
        self.current_req_id = None

    def on_built_packet(self, data_type: int, payload: bytes, *,
                        would_tx: bool, tx_ok: bool,
                        in_reply_to=None) -> None:
        self.served.append((data_type, bytes(payload)))


def make_service():
    settings = Settings()
    settings.area.center_lat = 37.0
    settings.area.center_lon = -122.0
    settings.feed.burst_gap_seconds = 0.0
    svc = ScopeService(settings, use_demo=False)   # facts ours
    svc.client = FakeRadio()
    svc.feed_tap = FakeTap()
    return svc


def seed_node(svc, prefix, lat, lon, *, path=()):
    now = time.time()
    svc.store.add(Observation(
        recv_ts=now, origin_ts=now - 1.5, prefix=prefix,
        lat=lat, lon=lon, path_prefixes=list(path)), now=now)
    # positions come from adverts/INTROs in production (add_position)
    svc.store.add_position(prefix, lat, lon, now=now)


def clinic_records(tap):
    """Every clinic record the door was handed - asserting the page
    law (<= 7 records, <= 163 B) on every packet along the way."""
    out = []
    for data_type, payload in tap.served:
        if data_type != codec.TYPE_CLINIC:
            continue
        batch = codec.decode_any(payload)
        assert batch.origin is not None
        assert len(batch.records) <= codec.CLINIC_MAX_RECORDS
        assert len(payload) <= codec.MAX_CHANNEL_DATA
        out.extend(batch.records)
    return out


def node_fact_prefixes(records):
    return {r.prefix for r in records
            if isinstance(r, codec.ClinicNodeFact)}


def route_fact_paths(records):
    return {tuple(r.path) for r in records
            if isinstance(r, codec.ClinicRouteFact)}


# ------------------------------------------- 1. the connect burst's book


@pytest.mark.asyncio
async def test_connect_burst_carries_the_whole_clinic_book():
    """A door connect dumps EVERY chart this box holds - nodes in
    every square, one wire-legal page after another, pulse still
    closing the burst. Nothing touches the air."""
    svc = make_service()
    deg = svc.settings.area.span_km * 1000.0 / 111320.0
    lat, lon = svc.settings.area.center_lat, svc.settings.area.center_lon
    for i in range(12):
        seed_node(svc, 0x30 + i, lat + deg * 0.4, lon)   # another square
    seed_node(svc, 0x60, lat, lon)                       # the home square
    await svc.pulse_now(reason="connect", with_layout=True, via_door=True)
    records = clinic_records(svc.feed_tap)
    assert node_fact_prefixes(records) == {0x30 + i for i in range(12)} \
        | {0x60}, "the connect dump must carry EVERY chart, every square"
    kinds = [dt for dt, _ in svc.feed_tap.served]
    # the pulse still closes the data burst (the node roster follows it)
    assert max(i for i, k in enumerate(kinds) if k == codec.TYPE_CLINIC) \
        < kinds.index(codec.TYPE_PULSE)
    assert svc.client.sent == [], "the door dump never touches the air"


@pytest.mark.asyncio
async def test_air_connect_pulse_stays_clinic_free():
    """The air's connect/answer path is UNCHANGED - the rotating
    cadence slices stay its only clinic path (BLE = updates only)."""
    svc = make_service()
    lat, lon = svc.settings.area.center_lat, svc.settings.area.center_lon
    seed_node(svc, 0x41, lat, lon)
    await svc.pulse_now(reason="connect", with_layout=True)
    assert codec.TYPE_CLINIC not in [dt for dt, _ in svc.client.sent], \
        "an air connect burst must never carry the clinic book"
    assert codec.TYPE_CLINIC not in [dt for dt, _ in svc.feed_tap.served]


# ------------------------------------------- 2. door answers carry the scope


@pytest.mark.asyncio
async def test_door_section_ask_carries_that_squares_charts_only():
    """A door section ask answers with THAT square's charts (and the
    mesh-wide health book) - the other square's facts stay home."""
    svc = make_service()
    deg = svc.settings.area.span_km * 1000.0 / 111320.0
    lat, lon = svc.settings.area.center_lat, svc.settings.area.center_lon
    sec_home = svc.geometry.section_for(lat, lon)
    sec_away = svc.geometry.section_for(lat + deg * 0.4, lon)
    assert sec_home != sec_away, "the test needs two different squares"
    for i in range(3):
        seed_node(svc, 0x40 + i, lat, lon)
        seed_node(svc, 0x50 + i, lat + deg * 0.4, lon)
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                           target=sec_home, nonce=11)
    await svc.on_packet(req, "door", via_door=True)
    records = clinic_records(svc.feed_tap)
    assert node_fact_prefixes(records) == {0x40, 0x41, 0x42}, \
        "the asked square's charts only"
    assert svc.client.sent == [], "door answers never touch the air"


@pytest.mark.asyncio
async def test_whole_area_door_ask_carries_the_whole_book():
    """A whole-area door ask = the full dump, same as connect."""
    svc = make_service()
    deg = svc.settings.area.span_km * 1000.0 / 111320.0
    lat, lon = svc.settings.area.center_lat, svc.settings.area.center_lon
    seed_node(svc, 0x40, lat, lon)
    seed_node(svc, 0x50, lat + deg * 0.4, lon)
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                           target=codec.REFRESH_WHOLE_AREA, nonce=12)
    await svc.on_packet(req, "door", via_door=True)
    records = clinic_records(svc.feed_tap)
    assert node_fact_prefixes(records) == {0x40, 0x50}
    assert svc.client.sent == []


@pytest.mark.asyncio
async def test_door_route_ask_carries_the_route_chart():
    """A door route ask answers with THAT route's chart."""
    svc = make_service()
    lat, lon = svc.settings.area.center_lat, svc.settings.area.center_lon
    seed_node(svc, 0x21, lat, lon, path=(0x11, 0x12))
    rid = _route_id((0x11, 0x12))
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_ROUTE,
                           target=rid, nonce=13)
    await svc.on_packet(req, "door", via_door=True)
    records = clinic_records(svc.feed_tap)
    assert (0x11, 0x12) in route_fact_paths(records), \
        "the asked route's chart must ride the answer"
    assert node_fact_prefixes(records) == set(), \
        "a route ask is about the route - the node charts stay home"
    assert svc.client.sent == []


@pytest.mark.asyncio
async def test_air_refresh_answer_stays_clinic_free():
    """The mirror of the door law on the air: a radio refresh answer
    is exactly what it was - never clinic packets."""
    svc = make_service()
    lat, lon = svc.settings.area.center_lat, svc.settings.area.center_lon
    seed_node(svc, 0x41, lat, lon)
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                           target=codec.REFRESH_WHOLE_AREA, nonce=14)
    await svc.on_packet(req, "aabbccddeeff")
    assert codec.TYPE_CLINIC not in [dt for dt, _ in svc.client.sent], \
        "radio answers never carry clinic packets"

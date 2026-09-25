"""DOOR-BORNE ASKS (Brett, 2026-09-24): "TCP is not the mesh."

An ask that arrives through the TCP data door is answered THROUGH THE
DOOR: no radio TX, no airtime budget, no per-phone limiter, no burst
gaps - the wire costs nothing to speak. The RADIO path keeps every
limit (the 1% duty law protects the air, not the wire). Written
BEFORE the code, per house law.
"""
import asyncio
import time

import pytest

from meshtech_node import codec
from meshtech_node.config import Settings
from meshtech_node.service import ScopeService


class FakeRadio:
    """Records every send (the radio-path spy)."""

    def __init__(self):
        self.sent = []

    async def send_channel_data(self, data_type: int, payload: bytes) -> bool:
        self.sent.append((data_type, bytes(payload)))
        return True


class FakeTap:
    """Stands in for the WebServe FeedTap: records what the door was
    handed (production wires brain.feed_tap = the WS server)."""

    def __init__(self):
        self.served = []
        self.current_req_id = None

    def on_built_packet(self, data_type: int, payload: bytes, *,
                        would_tx: bool, tx_ok: bool,
                        in_reply_to=None) -> None:
        self.served.append((data_type, bytes(payload)))


def make_service(**feed_overrides):
    settings = Settings()
    settings.area.center_lat = 37.0
    settings.area.center_lon = -122.0
    settings.feed.burst_gap_seconds = 0.8  # the REAL gap: door asks skip it
    for key, value in feed_overrides.items():
        setattr(settings.feed, key, value)
    return ScopeService(settings, use_demo=True)


def _seed_traffic(svc):
    """Nodes in the centre section through a known route."""
    now = time.time()
    for i in range(5):
        svc.store.add(type("O", (), {
            "recv_ts": now, "origin_ts": now - 1.5, "prefix": 0x21,
            "lat": 37.0, "lon": -122.0,
            "path_prefixes": [0x11, 0x12],
            "channel_name": None,
            "delay_s": 1.5})())


def test_door_ask_answers_without_radio_tx():
    """The core law: a door ask's answer NEVER touches the radio."""
    async def run():
        svc = make_service()
        radio = FakeRadio()
        svc.client = radio
        tap = FakeTap()
        svc.feed_tap = tap
        _seed_traffic(svc)
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=5, nonce=99, span_km=40)
        await svc.on_packet(req, "door", via_door=True)
        assert len(tap.served) > 0, "the door must be answered"
        assert radio.sent == [], \
            "a door ask must never transmit on the radio"
    asyncio.run(run())


def test_door_ask_skips_rate_limiter_and_budget():
    """A spent limiter and a spent budget must not stop the door."""
    async def run():
        svc = make_service(refresh_cooldown_seconds=3600.0)
        svc.rate.record("door")          # limiter: spent
        for _ in range(500):             # budget: spent to the cap
            svc.budget.record(255)
        radio = FakeRadio()
        svc.client = radio
        tap = FakeTap()
        svc.feed_tap = tap
        _seed_traffic(svc)
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=5, nonce=99, span_km=40)
        await svc.on_packet(req, "door", via_door=True)
        assert len(tap.served) > 0, \
            "the door answers even with every radio limit spent"
        assert radio.sent == []
    asyncio.run(run())


def test_door_ask_has_no_burst_gaps():
    """The 0.8 s airtime gaps exist to pace the RADIO; the door's
    answer must flow without them (a whole-area burst = seconds, not
    tens of seconds)."""
    async def run():
        svc = make_service()
        svc.client = FakeRadio()
        svc.feed_tap = FakeTap()
        _seed_traffic(svc)
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=codec.REFRESH_WHOLE_AREA, nonce=7,
                               span_km=40)
        start = time.monotonic()
        await svc.on_packet(req, "door", via_door=True)
        elapsed = time.monotonic() - start
        # Even ONE 0.8 s gap would show here; a handful of packets with
        # zero gaps lands far under 0.5 s in a test process.
        assert elapsed < 0.5, \
            f"door burst took {elapsed:.2f}s - gaps were not skipped"
    asyncio.run(run())


def test_radio_path_keeps_every_limit():
    """The other side of the law: an ON-AIR ask (the companion era)
    still walks dedupe -> rate limiter -> budget, unchanged."""
    async def run():
        svc = make_service(refresh_cooldown_seconds=3600.0)
        radio = FakeRadio()
        svc.client = radio
        svc.rate.record("aabbccddeeff")   # radio client: just asked
        _seed_traffic(svc)
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=4, nonce=123)
        await svc.on_packet(req, "aabbccddeeff")   # NOT via_door
        assert radio.sent == [], \
            "the radio path keeps its limiter"
    asyncio.run(run())


def test_door_ask_dedupe_still_applies():
    """Honesty guard: the same door ask (same nonce) twice in a row is
    a duplicate - answered once. The door is unlimited, not a hammer."""
    async def run():
        svc = make_service()
        svc.client = FakeRadio()
        tap = FakeTap()
        svc.feed_tap = tap
        _seed_traffic(svc)
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=5, nonce=99, span_km=40)
        await svc.on_packet(req, "door", via_door=True)
        first = len(tap.served)
        assert first > 0
        await svc.on_packet(req, "door", via_door=True)
        assert len(tap.served) == first, \
            "the same ask answered twice would be a lie, not generosity"
    asyncio.run(run())

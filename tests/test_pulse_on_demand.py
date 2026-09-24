"""PULSE-on-demand tests (Brett 2026-09-21: "not waiting 5 minutes").

The Feed-health card reads the PULSE packet; a pulse only went out on
the 300 s cadence, so a freshly connected app stared at "No PULSE
received yet" for up to five minutes. Two fixes pinned here:

- a NEW web client receives a PULSE immediately after connect
  (host mode only - companions have no host pulse to give);
- the CONNECT burst also carries the LAYOUT map frame FIRST
  (Brett, same day: the area grid draws on connect, no refresh
  press needed);
- a WHOLE-AREA refresh burst carries a PULSE too, so the refresh
  button fills the health card on the spot.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncio  # noqa: E402

import pytest  # noqa: E402

from meshtech_node.service import ScopeService  # noqa: E402
from meshtech_node.config import Settings  # noqa: E402


class FakeRadio:
    """The service's send seam itself. Mirrors the real client's honest
    first check: TX disabled -> refuse (return False), never send."""

    def __init__(self, tx_enabled: bool = False):
        self.sent = []
        self.tx_enabled = tx_enabled

    async def send_channel_data(self, data_type, payload):
        if not self.tx_enabled:
            return False
        self.sent.append((data_type, bytes(payload)))
        return True


class _TappedService(ScopeService):
    """Service with a fake radio and a recording tap (no WS needed)."""

    def __init__(self, **feed_overrides):
        settings = Settings()
        super().__init__(settings, use_demo=True)
        self.settings.feed.burst_gap_seconds = 0.0
        for key, value in feed_overrides.items():
            setattr(self.settings.feed, key, value)
        self.radio = FakeRadio(tx_enabled=feed_overrides.get(
            "tx_enabled", False))
        self.client = self.radio          # the send seam (test_service idiom)
        self.tx_enabled = self.settings.feed.tx_enabled
        self.tapped = []

    # capture the tap without a WebServe: _tap -> feed_tap.on_built_packet
    class _Tap:
        def __init__(self, svc):
            self.svc = svc
            self.current_req_id = None

        def on_built_packet(self, data_type, payload, *, would_tx,
                            tx_ok=False, in_reply_to=None):
            self.svc.tapped.append((data_type, bytes(payload),
                                    would_tx, tx_ok))

    def attach_tap(self):
        self.feed_tap = self._Tap(self)


@pytest.mark.asyncio
async def test_connect_burst_carries_layout_then_pulse():
    """The connect burst = LAYOUT, section summaries, then PULSE: map
    draws, health fills, routes are listed - one connect, no refresh
    press. (Summaries added 2026-09-24, Brett: routes on connect.)"""
    svc = _TappedService()
    svc.attach_tap()
    await svc.pulse_now(reason="test-connect", with_layout=True)
    kinds = [dt for dt, _p, _w, _t in svc.tapped]
    assert kinds[0] == 0x5305           # LAYOUT leads
    assert kinds[-1] == 0x5301          # PULSE closes
    assert kinds.count(0x5302) == 9     # one summary per home square


@pytest.mark.asyncio
async def test_pulse_now_without_layout_stays_pulse_only():
    """Other pulse_now callers (cadence catch-up etc.) keep the old
    single-packet behavior - the layout rides only on connect."""
    svc = _TappedService()
    svc.attach_tap()
    await svc.pulse_now(reason="test-plain")
    kinds = [dt for dt, _p, _w, _t in svc.tapped]
    assert kinds == [0x5301]            # PULSE only, no LAYOUT


def test_build_pulse_now_has_honest_uptime():
    svc = _TappedService()
    svc.started_at = __import__("time").time() - 150  # 2.5 min ago
    pulse = svc.build_pulse_now()
    # payload: type(2) len(1) ver(1) seq(2) origin(2) uptime(2)
    uptime = int.from_bytes(pulse.payload[8:10], "little")
    assert uptime == 2


@pytest.mark.asyncio
async def test_connect_summaries_carry_route_stubs():
    """Routes on connect (Brett, 2026-09-24): the connect burst's
    section summaries carry ROUTE STUBS, so a tapped section lists its
    routes immediately - not "No routes yet" until a cadence beat.
    Regression pin for the connect-burst summaries."""
    from meshtech_node.codec import decode_sect_sum, TYPE_SECT_SUM
    from meshtech_node.observations import Observation
    svc = _TappedService()
    svc.attach_tap()
    now = __import__("time").time()
    # One packet through a repeater, sent from the home center: the
    # section summary for that square must carry the path's stub.
    svc.builder.store.add(Observation(
        recv_ts=now, origin_ts=now - 2.0, prefix=0x77,
        lat=svc.settings.area.center_lat + 0.001,
        lon=svc.settings.area.center_lon + 0.001,
        path_prefixes=[0x66]), now=now)
    svc.builder.store.add_position(0x77,
                                   svc.settings.area.center_lat + 0.001,
                                   svc.settings.area.center_lon + 0.001,
                                   name="Sender", now=now)
    await svc.pulse_now(reason="test-connect", with_layout=True)
    sums = [decode_sect_sum(p[3:])
            for dt, p, _w, _t in svc.tapped if dt == TYPE_SECT_SUM]
    assert len(sums) == 9
    stubs = [s for s in sums if s.route_stubs]
    assert len(stubs) == 1, "exactly one section owns the route"
    assert stubs[0].route_stubs == [4198], (
        "the stub id must be derived from the heard path bytes "
        "(prefix<<2 | hops: 0x66+1 hop = 0x199)")


@pytest.mark.asyncio
async def test_pulse_now_taps_even_when_tx_refused():
    """TX off (listen-only): the on-air send is refused but the wire
    tap still serves the pulse - the app must see it either way."""
    svc = _TappedService(tx_enabled=False)
    svc.attach_tap()
    await svc.pulse_now(reason="test")
    assert len(svc.tapped) == 1
    data_type, payload, would_tx, tx_ok = svc.tapped[0]
    assert data_type == 0x5301          # PULSE
    assert would_tx is False            # honest: we never would have TXed
    assert tx_ok is False               # honest: the air send was refused
    assert svc.radio.sent == []         # nothing left for the air


@pytest.mark.asyncio
async def test_whole_area_refresh_burst_carries_a_pulse():
    svc = _TappedService()
    svc.attach_tap()
    from meshtech_node import codec
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                           target=codec.REFRESH_WHOLE_AREA, nonce=1)
    await svc._handle_refresh(req, "ws#1")
    kinds = [dt for dt, _p, _w, _t in svc.tapped]
    assert 0x5301 in kinds              # the pulse rides along
    assert kinds[-1] == 0x5301          # LAST packet: health after map


@pytest.mark.asyncio
async def test_section_refresh_does_not_carry_a_pulse():
    """Cheap per-square refreshes stay cheap - no pulse appended."""
    svc = _TappedService()
    svc.attach_tap()
    from meshtech_node import codec
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                           target=1, nonce=1)
    await svc._handle_refresh(req, "ws#1")
    kinds = [dt for dt, _p, _w, _t in svc.tapped]
    assert 0x5301 not in kinds

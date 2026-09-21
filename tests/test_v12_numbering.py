"""PROTOCOL v1.2 acceptance tests (2026-09-20, Brett's renumbering).

Sections are 1-based everywhere (1 = NW .. 9 = SE); 0 is RESERVED and
means whole-area in a REFRESH_REQ target - never a square. The wire
version byte is 0x03 and unknown versions are refused loudly.

These tests pin the two real bugs the renumbering closed:
- the S2 budget bypass (a kind=section target=0 refresh built a
  whole-map burst but drew no global budget), and
- the log/screen mismatch ("section 0" in logs, "Section 1" on screen).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from meshtech_node import codec  # noqa: E402
from meshtech_node.grid import geometry_from  # noqa: E402


# ------------------------------------------------------------ numbering --

def test_grid_is_one_based():
    geo = geometry_from(37.0, -122.0, 40000.0, 3)
    assert geo.section_for(geo.north - 0.0001, geo.west + 0.0001) == 1
    assert geo.section_for(37.0, -122.0) == 5
    assert geo.section_for(geo.south + 0.0001, geo.east - 0.0001) == 9
    with pytest.raises(ValueError):
        geo.section(0)          # reserved - never a square


def test_codec_rejects_zero_section_id():
    s = codec.SectSum(seq=1, section_id=0, active_nodes=1, packet_count=1,
                      delay_p50_s=0, delay_p90_s=0, origin=0xB17E)
    with pytest.raises(codec.CodecError):
        codec.encode_sect_sum(s)
    with pytest.raises(codec.CodecError):
        codec.encode_route(codec.Route(seq=1, section_id=0, route_id=1,
                                       packet_count=1, delay_med_s=0,
                                       last_heard_min=0, origin=0xB17E))


def test_unknown_wire_version_is_refused():
    body = codec.encode_sect_sum(codec.SectSum(
        seq=1, section_id=5, active_nodes=1, packet_count=1,
        delay_p50_s=0, delay_p90_s=0, origin=0xB17E))
    bad = bytes([0x2A]) + body[1:]      # 0x2A = a future version
    with pytest.raises(codec.CodecError):
        codec.decode_any(bad)
    # sanity: the untouched packet still decodes
    assert codec.decode_any(body).section_id == 5


# --------------------------------------------------------------- budget --

def _serve_with_budget(on_refresh=None):
    from meshtech_node.webserve import WebServe
    return WebServe("127.0.0.1", 8710, on_refresh=on_refresh)


@pytest.mark.asyncio
async def test_section_target_zero_draws_global_budget():
    """The S2 hole: kind=section target=0 IS a whole-map refresh and
    must draw the global budget like kind=layout/map does."""
    from aiohttp.test_utils import TestServer

    seen = []

    async def on_refresh(req, sender_prefix):
        seen.append((req.kind, req.target))

    w = _serve_with_budget(on_refresh=on_refresh)
    server = TestServer(w.app)
    await server.start_server()
    try:
        import aiohttp
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(f"ws://127.0.0.1:{server.port}/feed")
        await ws.receive()                       # hello
        await ws.receive()                       # state
        # the app's map button: kind=section target=0
        await ws.send_json({"type": "refresh", "kind": "section",
                            "section": 0, "req_id": "r1"})
        await asyncio.sleep(0.05)
        assert seen == [(codec.REFRESH_KIND_SECTION, 0)]
        # 2 allowed globally; the THIRD whole-map ask is refused with ack
        for i in range(2):
            await ws.send_json({"type": "refresh", "kind": "section",
                                "section": 0, "req_id": f"r{i + 2}"})
        await asyncio.sleep(0.1)
        acks = []
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=1.0)
            payload = json.loads(msg.data)
            if payload.get("type") == "ack" and not payload.get("accepted"):
                acks.append(payload)
                break
        assert acks[0]["reason"] == "map_budget"
        assert acks[0]["retry_after_s"] > 0
        await session.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_small_section_refresh_spares_budget():
    """Per-section asks (1..9) never consume the global whole-map slots."""
    from aiohttp.test_utils import TestServer

    seen = []

    async def on_refresh(req, sender_prefix):
        seen.append(req.target)

    w = _serve_with_budget(on_refresh=on_refresh)
    server = TestServer(w.app)
    await server.start_server()
    try:
        import aiohttp
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(f"ws://127.0.0.1:{server.port}/feed")
        await ws.receive()
        await ws.receive()                       # state
        for sid in range(1, 10):
            await ws.send_json({"type": "refresh", "kind": "section",
                                "section": sid, "req_id": f"s{sid}"})
        await asyncio.sleep(0.1)
        assert len(seen) == 9                    # none refused, none budgeted
        assert w.map_budget.retry_after_s() == 0  # budget untouched
        await session.close()
    finally:
        await server.close()


@pytest.mark.asyncio
async def test_kind_map_spelling_accepted():
    """v1.2 app sends kind=map for the map button; still dispatched."""
    from aiohttp.test_utils import TestServer

    seen = []

    async def on_refresh(req, sender_prefix):
        seen.append((req.kind, req.target))

    w = _serve_with_budget(on_refresh=on_refresh)
    server = TestServer(w.app)
    await server.start_server()
    try:
        import aiohttp
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(f"ws://127.0.0.1:{server.port}/feed")
        await ws.receive()
        await ws.receive()                       # state
        await ws.send_json({"type": "refresh", "kind": "map",
                            "req_id": "m1"})
        await asyncio.sleep(0.05)
        assert seen == [(codec.REFRESH_KIND_SECTION, 0)]
        await session.close()
    finally:
        await server.close()

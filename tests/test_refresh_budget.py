"""S2 - the wire refresh budget (2026-09-20 review).

Whole-map refreshes draw from a GLOBAL budget (2 per 30 min across all
connections - Brett's design); per-section refreshes do not. The brain's
per-client limiter sees a STABLE per-connection identity, not the
per-click req_id.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from meshtech_node.webserve import _GlobalRefreshBudget, WebServe  # noqa: E402


# --------------------------------------------------------------- budget --

def test_budget_allows_first_two():
    b = _GlobalRefreshBudget(max_per_window=2, window_s=1800.0)
    assert b.allow() and b.allow()


def test_budget_refuses_third():
    b = _GlobalRefreshBudget(max_per_window=2, window_s=1800.0)
    b.allow(); b.allow()
    assert not b.allow()


def test_budget_slot_frees_after_window():
    b = _GlobalRefreshBudget(max_per_window=2, window_s=1800.0)
    now = 1000.0
    b.allow(now=now); b.allow(now=now)
    assert not b.allow(now=now + 10)
    assert b.allow(now=now + 1801)      # first hit aged out


def test_budget_retry_after_is_sane():
    b = _GlobalRefreshBudget(max_per_window=2, window_s=1800.0)
    assert b.retry_after_s() == 0
    now = 1000.0
    b.allow(now=now); b.allow(now=now)
    wait = b.retry_after_s(now=now + 100)
    assert 0 < wait <= 1800


def test_budget_is_global_across_instances_of_use():
    # one WebServe, two "connections" (same budget object) - the third
    # map refresh is refused regardless of who asked.
    w = WebServe("127.0.0.1", 8710)
    assert w.map_budget.allow() and w.map_budget.allow()
    assert not w.map_budget.allow()


# ------------------------------------------------- per-connection id ----

@pytest.mark.asyncio
async def test_refresh_uses_connection_identity_not_req_id():
    """Two refreshes with DIFFERENT req_ids from the SAME connection
    share one limiter identity (ws#N) - the per-click bypass is dead."""
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    seen = []

    async def on_refresh(req, sender_prefix, via_door=False):
        seen.append((sender_prefix, via_door))

    w = WebServe("127.0.0.1", 8710, on_refresh=on_refresh)
    server = TestServer(w.app)
    await server.start_server()
    try:
        import aiohttp
        session = aiohttp.ClientSession()
        ws = await session.ws_connect(
            f"ws://127.0.0.1:{server.port}/feed")
        await ws.receive()          # hello
        await ws.receive()          # state
        await ws.send_json({"type": "refresh", "req_id": "a",
                            "kind": "section", "section": 1})
        await ws.send_json({"type": "refresh", "req_id": "b",
                            "kind": "section", "section": 2})
        await asyncio.sleep(0.2)
        assert len(seen) == 2
        assert seen[0][0] == seen[1][0]     # one identity per connection
        assert seen[0][0].startswith("ws#")
        # DOOR-BORNE (Brett 2026-09-24): every ask through this door
        # is flagged as wire-borne - the brain answers accordingly.
        assert all(v for _, v in seen)
        await ws.close()
        await session.close()
    finally:
        await server.close()

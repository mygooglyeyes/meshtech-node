"""DATA DOOR tests (SELF-CONTAINED RULE, Brett 2026-09-21).

The feed link may cross machines; it carries DATA only. The door's
password gates that data: browsers cannot set custom WS headers, so
they present the token as a WebSocket subprotocol ("bearer.<token>")
- the node picks it when it matches (constant-time) and refuses
everything else BEFORE the socket upgrades. Fail closed.
"""
import asyncio
import json

import pytest

pytestmark = pytest.mark.asyncio

sys_path_done = False
if not sys_path_done:
    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    sys_path_done = True

import aiohttp  # noqa: E402
from aiohttp import WSMsgType, web  # noqa: E402

from meshtech_node.webserve import WebServe  # noqa: E402


def _serve(token):
    return WebServe("127.0.0.1", 0, token=token,
                    feed_info={"tx_enabled": False})


async def _site(serve):
    runner = web.AppRunner(serve.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner, f"ws://127.0.0.1:{port}/feed"


async def _open(url, protocols=None, headers=None):
    session = aiohttp.ClientSession()
    ws = await session.ws_connect(url, protocols=protocols or [],
                                  headers=headers or {})
    return session, ws


async def _recv_hello(ws):
    msg = await asyncio.wait_for(ws.receive(), 3.0)
    assert msg.type == WSMsgType.TEXT
    return json.loads(msg.data)


async def test_subprotocol_token_accepted():
    serve = _serve("s3cret")
    runner, url = await _site(serve)
    try:
        session, ws = await _open(url, protocols=["bearer.s3cret"])
        hello = await _recv_hello(ws)
        assert hello["type"] == "hello"
        await ws.close()
        await session.close()
    finally:
        await runner.cleanup()


async def test_wrong_token_refused_before_upgrade():
    serve = _serve("s3cret")
    runner, url = await _site(serve)
    try:
        session = aiohttp.ClientSession()
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await session.ws_connect(url, protocols=["bearer.wrong"])
        await session.close()
    finally:
        await runner.cleanup()


async def test_missing_token_refused():
    serve = _serve("s3cret")
    runner, url = await _site(serve)
    try:
        session = aiohttp.ClientSession()
        with pytest.raises(aiohttp.WSServerHandshakeError):
            await session.ws_connect(url)
        await session.close()
    finally:
        await runner.cleanup()


async def test_header_token_still_works_for_non_browsers():
    serve = _serve("s3cret")
    runner, url = await _site(serve)
    try:
        session, ws = await _open(url, headers={"X-Node-Token": "s3cret"})
        hello = await _recv_hello(ws)
        assert hello["type"] == "hello"
        await ws.close()
        await session.close()
    finally:
        await runner.cleanup()


async def test_tokenless_server_accepts_anonymous():
    serve = _serve(None)              # loopback/same-origin posture
    runner, url = await _site(serve)
    try:
        session, ws = await _open(url)
        hello = await _recv_hello(ws)
        assert hello["type"] == "hello"
        await ws.close()
        await session.close()
    finally:
        await runner.cleanup()

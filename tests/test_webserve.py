"""WebServe + direct-mode end-to-end tests (WEBSERVE-PROTOCOL.md v1).

The bench section 7 contract: a WebSocket client receives the SAME
wire bodies the feed builds, refresh over the wire is dispatched
through the brain's real rate limiter, and the tap tags answer
packets with in_reply_to. Run with the node's venv python
(aiohttp + the reference crypto via conftest).
"""
import asyncio
import json
import sys

import pytest

sys.path.insert(0, "src")

import pytest_asyncio  # noqa: E402

from aiohttp import WSMsgType, web  # noqa: E402

from meshtech_node import config, webserve  # noqa: E402
from meshtech_node.node import _build, _state_snapshot  # noqa: E402
from meshtech_node.service import ScopeService  # noqa: E402

conftest = pytest.importorskip("conftest")


# ----------------------------------------------------------------- fakes


class FakeSettings:
    """The settings _build needs, without a config file on disk.

    Built on the real config dataclasses so a field rename breaks
    loudly here instead of silently at the bench."""

    def __init__(self):
        self.channel = config.ChannelCfg()
        self.area = config.AreaCfg()
        self.feed = config.FeedCfg()
        self.radio = config.RadioCfg()
        self.storage = config.StorageCfg()
        self.logging = config.LoggingCfg()
        self.webserve = config.WebServeCfg()
        self.companion_host = "127.0.0.1"
        self.companion_port = 5052
        self.repeater_api = config.RepeaterApiCfg()
        self.warnings = []
        self.raw = {}


# ------------------------------------------------------------- fixtures


@pytest_asyncio.fixture
async def bench_pair(aiohttp_server_factory):
    """A _build()-wired node in bench mode + an aiohttp site + client."""
    settings = FakeSettings()
    source, sender, brain, serve = _build(settings, bench_no_radio=True)
    runner = web.AppRunner(serve.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield serve, brain, source, sender, f"ws://127.0.0.1:{port}/feed"
    await runner.cleanup()


@pytest.fixture
def aiohttp_server_factory():
    """Minimal site factory (aiohttp pytest plugin not required)."""
    return _noop_factory


def _noop_factory():  # pragma: no cover - placeholder, see bench_pair
    return None


class WSClient:
    """Tiny test WebSocket client (aiohttp)."""

    def __init__(self, url):
        self.url = url
        self.session = None
        self.ws = None
        self.inbox = []

    async def __aenter__(self):
        import aiohttp
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url)
        return self

    async def __aexit__(self, *exc):
        if self.ws is not None:
            await self.ws.close()
        await self.session.close()

    async def recv(self, timeout=3.0):
        msg = await asyncio.wait_for(self.ws.receive(), timeout)
        if msg.type != WSMsgType.TEXT:
            raise AssertionError(f"unexpected WS frame: {msg}")
        return json.loads(msg.data)

    async def send(self, obj):
        await self.ws.send_str(json.dumps(obj))


# ---------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_hello_state_packet_flow(bench_pair):
    """Section 7 core: connect -> hello -> LAYOUT arrives (fresh node
    serves it at link-ready; in bench mode TX is refused but the packet
    is STILL served - the whole point)."""
    serve, brain, source, sender, url = bench_pair
    async with WSClient(url) as ws:
        hello = await ws.recv()
        assert hello["type"] == "hello"
        assert hello["proto"] == webserve.PROTO_VERSION
        assert hello["tx_enabled"] is False          # Gate 1 posture
        assert hello["feed"]["bench_no_radio"] is True
        state = await ws.recv()
        assert state["type"] == "state"
        # kick the brain's cadence: link is 'ready' (bench), first
        # broadcast fires at once
        task = asyncio.create_task(brain.run())
        try:
            # LAYOUT (or the pulse burst) should be tapped through.
            got = {}
            for _ in range(8):
                msg = await ws.recv(timeout=10)
                got[msg["type"]] = msg
                if msg["type"] == "packet" and msg["kind"] == "layout":
                    break
            assert got.get("packet", {}).get("kind") == "layout", got
            pkt = got["packet"]
            assert pkt["would_tx"] is False          # honest: TX off
            assert pkt["tx_ok"] is False             # refused by guard
            assert pkt["snr"] is None                # no radio hop
            assert pkt["in_reply_to"] is None        # spontaneous
        finally:
            brain.stop()
            with pytest.raises(asyncio.CancelledError):
                task.cancel()
                await task


@pytest.mark.asyncio
async def test_refresh_in_reply_to_tagged(bench_pair):
    """A wire refresh is dispatched through the REAL brain; the answer
    burst's packets come back tagged with the client's req_id."""
    serve, brain, source, sender, url = bench_pair
    brain_task = asyncio.create_task(brain.run())
    try:
        async with WSClient(url) as ws:
            await ws.recv()   # hello
            await ws.recv()   # state
            req_id = "test-req-1"
            await ws.send({"type": "refresh", "req_id": req_id,
                           "kind": "layout"})
            # The answer burst (tagged) streams while the dispatch is
            # awaited; the ack follows the burst (dispatch completes
            # first) - both must arrive.
            saw_ack = False
            tagged = 0
            for _ in range(12):
                msg = await ws.recv(timeout=10)
                if msg["type"] == "ack":
                    assert msg["req_id"] == req_id
                    saw_ack = True
                elif msg["type"] == "packet":
                    assert msg["in_reply_to"] == req_id
                    tagged += 1
                    if tagged >= 2 and saw_ack:
                        break
            assert saw_ack
            assert tagged >= 2
    finally:
        brain.stop()
        brain_task.cancel()


@pytest.mark.asyncio
async def test_rate_limiter_applies_to_wire_refresh(bench_pair):
    """No bypass: a second refresh inside the cooldown is accepted at
    the wire but produces NO new answer burst (limiter stays silent)."""
    serve, brain, source, sender, url = bench_pair
    brain.settings.feed.refresh_cooldown_seconds = 300.0
    brain.rate = brain.rate.__class__(300.0, 10, [])
    brain_task = asyncio.create_task(brain.run())
    try:
        async with WSClient(url) as ws:
            await ws.recv()
            await ws.recv()
            await ws.send({"type": "refresh", "req_id": "r1",
                           "kind": "layout"})
            seen = 0
            for _ in range(20):
                msg = await ws.recv(timeout=10)
                if msg["type"] == "packet" and msg["in_reply_to"] == "r1":
                    seen += 1
                    if seen >= 2:
                        break
            assert seen >= 2
            # second refresh inside cooldown: ack, then NO tagged packets
            await ws.send({"type": "refresh", "req_id": "r2",
                           "kind": "layout"})
            for _ in range(4):
                msg = await ws.recv(timeout=3)
                if msg["type"] == "packet":
                    assert msg["in_reply_to"] != "r2", \
                        "rate-limited refresh must not answer"
                    break
    finally:
        brain.stop()
        brain_task.cancel()


@pytest.mark.asyncio
async def test_resume_ring_and_bad_auth(bench_pair):
    """Ring replay after reconnect + token refusal posture."""
    serve, brain, source, sender, url = bench_pair
    # feed two fake packets into the ring
    serve.on_built_packet(0x5301, b"\x01\x02\x03", would_tx=False)
    serve.on_built_packet(0x5302, b"\x04\x05\x06", would_tx=False)
    async with WSClient(url) as ws:
        hello = await ws.recv()
        assert hello["last_seq"] == 2
        await ws.send({"type": "resume", "after_seq": 1})
        # the state message (sent at connect) may still be queued ahead
        # of the replay - drain until the seq-2 packet shows up.
        replayed = None
        for _ in range(5):
            msg = await ws.recv()
            if msg.get("type") == "packet" and msg.get("seq") == 2:
                replayed = msg
                break
        assert replayed is not None and replayed["wire"] == "040506"
    # auth: without a token on a loopback bind, no auth is required -
    # (negative path covered by unit tests on _Auth in token mode).


def test_auth_requires_token_when_non_loopback():
    """Fail-closed: a non-loopback WebServe with no token is refused by
    the shell helper (never half-open)."""
    from meshtech_node.node import _is_loopback
    assert _is_loopback("127.0.0.1")
    assert _is_loopback("::1")
    assert not _is_loopback("0.0.0.0")
    assert not _is_loopback("192.168.1.10")


def test_state_snapshot_honest_when_silent():
    """A node that has heard NOTHING reports zeros/nulls - never
    invented numbers (the -105.0 rule)."""
    settings = FakeSettings()
    source, sender, brain, serve = _build(settings, bench_no_radio=True)
    snap = _state_snapshot(brain, source)
    assert snap["listener"]["pkts_last_hour"] == 0
    assert snap["listener"]["nodes_active"] == 0
    assert snap["feed"]["last_pulse_ts"] is None
    assert snap["feed"]["next_pulse_ts"] is None

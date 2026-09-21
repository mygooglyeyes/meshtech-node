"""COMPANION MODE tests (Brett 2026-09-20, the phone-app simulation).

A companion device is a separate node that listens to ANOTHER node's
radio server over TCP (observer role) and builds its map purely from
heard packets - the true simulation of the future phone + companion
radio. The invariants pinned here:

- the flag parses (deploy/config.companion.json stays valid),
- the feed cadence is PARKED (nothing built, nothing sent, no airtime),
- an on-air REFRESH_REQ is refused honestly (never answered),
- the web app's refresh button gets a plain listen_only ack,
- heard #scope packets STILL fill the store and reach the app
  (the whole point of the device).
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from meshtech_node import codec, config, webserve  # noqa: E402
from meshtech_node.service import ScopeService  # noqa: E402

from test_webserve import FakeSettings, WSClient  # noqa: E402


# --------------------------------------------------------------- config --

def test_companion_flag_parses():
    raw = {"feed": {"companion_mode": True}}
    settings = config.load.__wrapped__ if hasattr(config.load, "__wrapped__") else None
    # direct loader call via the public path: build a temp config file
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
        json.dump({"feed": {"companion_mode": True,
                            "tx_enabled": False}}, fh)
        path = fh.name
    try:
        s = config.load(path)
        assert s.feed.companion_mode is True
        assert s.feed.tx_enabled is False
    finally:
        os.unlink(path)


def test_companion_template_is_valid_and_defaults_right():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "deploy", "config.companion.json")
    s = config.load(path)
    assert s.feed.companion_mode is True
    assert s.feed.tx_enabled is False          # a companion never transmits
    assert s.companion_host != ""              # placeholder the user edits
    assert s.modem_conf == ""                  # no radio of its own


def test_host_template_has_companion_off():
    path = os.path.join(os.path.dirname(__file__), "..",
                        "deploy", "config.json")
    s = config.load(path)
    assert s.feed.companion_mode is False      # hilltop stays the host


# --------------------------------------------------------------- brain --

def test_companion_feed_cadence_is_parked():
    """The broadcast loop must park before any seed/ TX attempt."""
    settings = FakeSettings()
    settings.feed.companion_mode = True
    brain = ScopeService(settings, use_demo=True, client=_DeadClient())

    async def scenario():
        task = asyncio.create_task(brain.broadcast_loop())
        await asyncio.sleep(0.1)
        assert not task.done()                 # parked, alive, silent
        # nothing was built: the seed tick never ran (0.0 = never set)
        assert brain.builder._last_pulse == 0.0
        assert brain.builder._last_layout == 0.0
        assert brain._startup_seeded is False  # seeding never happened
        task.cancel()

    asyncio.run(scenario())


def test_companion_refuses_onair_refresh():
    """An overheard REFRESH_REQ must be dropped - a companion has no
    TX mandate and no host data."""
    settings = FakeSettings()
    settings.feed.companion_mode = True
    brain = ScopeService(settings, use_demo=True, client=_DeadClient())

    sent = []

    async def fake_burst(packets, **_kw):
        sent.extend(packets)

    brain._send_burst = fake_burst
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                           target=5, nonce=99, origin=0x1234)
    asyncio.run(brain._handle_refresh(req, "deadbeef0000"))
    assert sent == []                          # nothing answered


# --------------------------------------------------------- the link --

def test_transport_start_is_reusable_after_failure(monkeypatch):
    """A failed start must not poison the next one: the closed flag
    resets and the stop-sentinel drains (the companion's patient-link
    precondition)."""
    import cleanmodem.client as cc

    class FlakyClient:
        def __init__(self, *a, **k):
            self.connected = False
            self._stop = asyncio.Event()

        async def run(self):
            raise RuntimeError("first attempt always fails here")

        def stop(self):
            self._stop.set()

    monkeypatch.setattr(cc, "ModemClient", FlakyClient)

    async def scenario():
        from meshtech_node.modemlink import ModemTransport
        modem = ModemTransport("127.0.0.1", 5055, "t")
        modem.connect_timeout_s = 0.1
        with pytest.raises(RuntimeError):
            await modem.start()
        # failed start tore down (closed=True, sentinel queued) - but
        # the NEXT start must reset that, not inherit the poison
        class UpClient:
            def __init__(self, *a, **k):
                self.connected = False
                self._stop = asyncio.Event()

            async def run(self):
                await asyncio.sleep(0.02)
                self.connected = True
                await self._stop.wait()

            def stop(self):
                self._stop.set()

        monkeypatch.setattr(cc, "ModemClient", UpClient)
        modem.connect_timeout_s = 1.0
        await modem.start()                    # second attempt succeeds
        assert modem._closed is False          # reset by the new start
        assert modem.connected is True         # UpClient connects
        assert modem.alive is True             # ...and the task lives
        modem.stop()

    asyncio.run(scenario())


def test_companion_link_restarts_dead_transport(monkeypatch):
    """The monitor's contract: a dead transport task gets restarted
    with backoff; a live one is never double-started."""
    from meshtech_node.node import _companion_link

    class FakeModem:
        def __init__(self):
            self.starts = 0
            self.connected = False
            self.alive = False
            self.host, self.port = "h", 1

        async def start(self):
            self.starts += 1
            self.alive = True                  # start spawns a live task
            self.connected = True

    async def scenario():
        modem = FakeModem()
        stop = asyncio.Event()
        task = asyncio.create_task(_companion_link(modem, stop))
        await asyncio.sleep(0.15)
        assert modem.starts == 1               # started once...
        assert modem.alive                     # ...and not restarted while alive
        modem.alive = False                    # the task died (client crash)
        modem.connected = False
        await asyncio.sleep(1.5)               # monitor polls at 1s
        assert modem.starts == 2               # the monitor restarted it
        stop.set()
        await asyncio.wait_for(task, 2)

    asyncio.run(scenario())


# ------------------------------------------------- heard packets work --

def test_companion_map_still_fills_from_heard_packets():
    """THE point of the device: a heard, ENCRYPTED hilltop packet is
    decrypted by the listener and BOTH paths fire - the brain's
    on_packet (peer tracking) and the on_heard bridge (the app's tap).
    With the feed cadence parked, this bridge is the app's only data
    source - exactly like a companion radio feeding a phone."""
    pytest.importorskip("pymc_core", reason="crypto-backed case")
    from pymc_core.protocol.crypto import CryptoUtils  # noqa: PLC0415
    from test_rxshim import _AES, _encrypt_scope, _frame  # noqa: PLC0415
    from meshtech_node.rawsource import RawPacketSource, RxPacket  # noqa: PLC0415
    from meshtech_node.packets import ChannelKeys  # noqa: PLC0415

    async def scenario():
        settings = FakeSettings()
        settings.feed.companion_mode = True
        brain = ScopeService(settings, use_demo=True, client=_DeadClient())

        heard = []                             # the on_heard bridge's tap
        heard_meta = []

        def tap(data_type, plaintext, rx):
            heard.append(data_type)
            heard_meta.append(rx.snr)

        src = RawPacketSource(
            transport=None,
            channels=[ChannelKeys.from_secret("#scope", "#scope")])
        src.on_scope = brain.on_packet
        src.on_heard = tap

        pulse = codec.Pulse(seq=7, uptime_min=12, rx_per_hour=100,
                            feed_airtime_s_per_h=3, active_total=58,
                            origin=0xABCD)
        data = _frame(0x06, 0, _encrypt_scope(codec.encode_pulse(pulse)))

        obs = src.handle_packet(RxPacket(data=data, snr=11.5))
        assert obs is not None                 # the store ALSO gets the obs
        assert src.stats.decoded == 1
        assert heard == [codec.TYPE_PULSE]     # the bridge fired
        assert heard_meta == [11.5]            # real radio hop carried
        # and the brain heard it through on_scope (peer/origin tracking)
        pending = [t for t in asyncio.all_tasks()
                   if t is not asyncio.current_task()]
        await asyncio.gather(*pending)

    asyncio.run(scenario())


# ------------------------------------------------------------ websocket --

@pytest.mark.asyncio
async def test_companion_ws_refresh_gets_listen_only_ack():
    """The app's refresh button gets plain words, never silence."""
    serve = webserve.WebServe(
        "127.0.0.1", 0,
        feed_info={"tx_enabled": False, "companion_mode": True,
                   "feed": {"bench_no_radio": False}})
    seen = []

    async def fake_handler(req, conn_id):
        seen.append(req)                       # must never be reached

    serve.on_refresh = fake_handler
    from aiohttp import web
    runner = web.AppRunner(serve.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    try:
        async with WSClient(f"ws://127.0.0.1:{port}/feed") as ws:
            hello = await ws.recv()
            assert hello["type"] == "hello"
            assert hello["tx_enabled"] is False
            await ws.recv()                    # initial state message
            await ws.send({"type": "refresh", "req_id": "r1",
                           "kind": "map"})
            ack = await ws.recv()
            assert ack["type"] == "ack"
            assert ack["accepted"] is False
            assert ack["reason"] == "listen_only"
            await ws.send({"type": "refresh", "req_id": "r2",
                           "kind": "section", "section": 5})
            ack2 = await ws.recv()
            assert ack2["accepted"] is False and ack2["reason"] == "listen_only"
        assert seen == []                      # nothing reached the brain
    finally:
        await runner.cleanup()


class _DeadClient:
    """Stand-in client that reports never-connected (honest).
    Matches ModemClient's construction signature."""
    is_connected = False
    has_slot = False

    def __init__(self, *a, **k):
        self.connected = False
        self._stop = asyncio.Event()
        self._task = None

    def send_channel_data(self, data_type, payload):
        return False

    async def run(self):
        await self._stop.wait()

    def stop(self):
        self._stop.set()

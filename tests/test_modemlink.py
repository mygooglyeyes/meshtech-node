"""ModemTransport tests - the real-radio bridge.

The bridge contract: cleanmodem's RX pushes (rssi, snr, sig, data)
become RxPackets the RawPacketSource consumes; a dead link fails
LOUDLY; a dropped packet never kills the link.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from meshtech_node.modemlink import ModemTransport  # noqa: E402


class FakeModemClient:
    """Stand-in for cleanmodem's ModemClient: records construction,
    fakes the connect delay, lets tests push RX like _pump does."""

    instances = []

    def __init__(self, host, port, token, on_rx, service=None):
        self.host, self.port, self.token = host, port, token
        self._on_rx = on_rx
        self.connected = False
        self._stop = asyncio.Event()
        FakeModemClient.instances.append(self)

    async def run(self):
        await asyncio.sleep(0.05)
        self.connected = True
        try:
            await self._stop.wait()
        except asyncio.CancelledError:
            self.connected = False
            raise

    def stop(self):
        self._stop.set()

    def push(self, rssi, snr, sig, data):
        self._loop = asyncio.get_event_loop()
        asyncio.ensure_future(self._deliver(rssi, snr, sig, data))

    async def _deliver(self, rssi, snr, sig, data):
        await self._on_rx(rssi, snr, sig, data)


@pytest.fixture(autouse=True)
def _fake_cls(monkeypatch):
    FakeModemClient.instances = []
    # ModemTransport imports ModemClient lazily from cleanmodem.client
    # (repo-root package, copied as-is per the seed map).
    import sys as _sys
    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    import cleanmodem.client as cc
    monkeypatch.setattr(cc, "ModemClient", FakeModemClient)
    yield


def test_rx_becomes_rxpacket():
    async def main():
        modem = ModemTransport("127.0.0.1", 5055, "tok")
        await modem.start()
        client = FakeModemClient.instances[0]
        client.push(-70, 12.5, -71, b"\x7b\x00abc")
        pkt = await asyncio.wait_for(anext(modem), timeout=2)
        assert pkt.data == b"\x7b\x00abc"
        assert pkt.rssi == -70 and pkt.snr == 12.5
        assert modem.rx_count == 1 and modem.connected
        modem.stop()
    asyncio.run(main())


def test_failed_link_fails_loudly():
    async def main():
        # a client that NEVER connects (link down / bad token)
        class NeverConnects(FakeModemClient):
            async def run(self):
                await asyncio.sleep(10)
        modem = ModemTransport("127.0.0.1", 5055, "tok")
        modem.connect_timeout_s = 0.3   # shrink for the test
        import cleanmodem.client as cc
        cc.ModemClient = NeverConnects
        with pytest.raises(RuntimeError, match="did not come up"):
            await modem.start()
    asyncio.run(main())


def test_bad_packet_never_kills_link():
    async def main():
        modem = ModemTransport("127.0.0.1", 5055, "tok")
        await modem.start()
        client = FakeModemClient.instances[0]
        # _on_rx guards internally: a None data raises inside and is
        # swallowed - the transport survives.
        await modem._on_rx(-70, 1.0, -70, b"")
        client.push(-71, 2.0, -72, b"\x7b\x01zz")
        pkt = await asyncio.wait_for(anext(modem), timeout=2)
        assert pkt.data == b"\x7b\x01zz"
        assert modem.rx_count == 1  # the empty one was not counted
        modem.stop()
    asyncio.run(main())


def test_stop_unblocks_iterator():
    async def main():
        modem = ModemTransport("127.0.0.1", 5055, "tok")
        await modem.start()
        modem.stop()
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(anext(modem), timeout=2)
    asyncio.run(main())

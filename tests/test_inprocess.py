"""In-process radio mode tests (single-process ownership).

Contract: the node embeds cleanmodem's ModemServer with a FAKE hal,
binds its loopback, and the proven ModemClient connects to its own
port. Radio failure = loud refusal, never a radio-less silent feed.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from meshtech_node.inprocess import InProcessRadio  # noqa: E402


class FakeHal:
    """Stand-in for SX126xRadio: records lifecycle, fakes RX."""

    instances = []

    def __init__(self, *args, **kwargs):
        self.started = False
        self.stopped = False
        self.on_rx_packet = None
        FakeHal.instances.append(self)

    async def start(self, loop):
        self.started = True
        return True

    async def stop(self):
        self.stopped = True


@pytest.fixture(autouse=True)
def fake_radio(monkeypatch, tmp_path):
    FakeHal.instances = []
    import cleanmodem.sx126x as sx
    monkeypatch.setattr(sx, "SX126xRadio", FakeHal)
    # a minimal modem.conf pointing at a scratch token
    token = tmp_path / "modem.token"
    token.write_text("test-token-1234\n")
    conf = tmp_path / "modem.conf"
    conf.write_text(
        "host = 127.0.0.1\n"
        "port = 5099\n"
        f"token_file = {token}\n"
        f"controller_file = {token}\n"
        "frequency_hz = 910525000\n"
        "spreading_factor = 7\n")
    return conf


def test_embedded_server_boots_and_serves_loopback(fake_radio):
    async def main():
        radio = InProcessRadio(str(fake_radio))
        port = await radio.start()
        assert port == 5099
        hal = FakeHal.instances[0]
        assert hal.started
        # the proven client connects to the node's OWN loopback
        from cleanmodem.client import ModemClient
        seen = asyncio.Event()

        async def on_rx(rssi, snr, sig, data):
            seen.set()

        client = ModemClient("127.0.0.1", port, "test-token-1234", on_rx)
        task = asyncio.create_task(client.run())
        for _ in range(50):
            if client.connected:
                break
            await asyncio.sleep(0.1)
        assert client.connected, "client never connected to embedded server"
        client.stop()
        task.cancel()
        await radio.stop()
        assert hal.stopped
    asyncio.run(main())


def test_radio_failure_refuses_loudly(fake_radio, monkeypatch):
    async def main():
        import cleanmodem.server as server_mod

        async def fail_start(self):
            return False
        monkeypatch.setattr(server_mod.ModemServer, "start", fail_start)
        radio = InProcessRadio(str(fake_radio))
        with pytest.raises(RuntimeError, match="refuses to run radio-less"):
            await radio.start()
    asyncio.run(main())

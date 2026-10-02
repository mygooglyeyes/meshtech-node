"""The REAL openhop driver against cleanmodem - the lab's actual link.

The stand-ins prove our side; THIS proves the whole link: openhop_core's
TCPLoRaRadio (the exact driver the lab repeater runs - unmodified,
from the read-only reference clone) talking to cleanmodem's server over
a loopback socket:

- the AUTH/PING/SET_CONFIG handshake comes up as the REPEATER role,
- its LBT CAD + TX_REQUEST lands as a real transmission through the
  politeness gate,
- the RX fan-out reaches its callback with the radio's signal facts,
- the config authority stays ours (its SET_CONFIG gets an echo; the
  live config never moves),
- and an OBSERVER-token driver is still refused the air.

Requires the read-only openhop_core clone (conftest's _REF path); the
file skips cleanly when it is absent.
"""
import asyncio
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

pymc = pytest.importorskip(  # noqa: E402
    "pymc_core", reason="openhop_core reference clone needed")

from pymc_core.hardware.tcp_radio import TCPLoRaRadio  # noqa: E402

from cleanmodem.config import ModemConfig  # noqa: E402
from cleanmodem.hal import RadioStatus, RxPacket, TxResult  # noqa: E402
from cleanmodem.server import ROLE_OBSERVER, ROLE_REPEATER, ModemServer  # noqa: E402

OBS_TOKEN = "obs-token-1"
REP_TOKEN = "rep-token-1"
CTL_TOKEN = "ctl-token-1"

# The live mesh numbers (hilltop's modem.conf): the driver proposes
# the SAME config the server holds - exactly like the lab, where both
# sides are set to the same mesh settings.
MESH = dict(frequency=910525000, bandwidth=62500, spreading_factor=7,
            coding_rate=5, tx_power=21, sync_word=0x12, preamble_length=32)


class FakeHal:
    """RadioHal stand-in: records every call, fakes every result."""

    def __init__(self):
        self.on_rx_packet = None
        self.on_hal_error = None
        self.txs = []
        self.cad_calls = 0
        self.applied_configs = []

    async def start(self, loop):
        return True

    async def stop(self):
        pass

    async def tx(self, data):
        self.txs.append(bytes(data))
        return TxResult(ok=True, airtime_us=1234)

    async def cad(self, det_peak=0, det_min=0):
        self.cad_calls += 1
        return False                  # channel clear

    async def noise(self):
        return -104.5

    async def status(self):
        return RadioStatus()

    async def apply_config(self, cfg):
        self.applied_configs.append(dict(cfg))
        return True


async def _start_server():
    hal = FakeHal()
    cfg = ModemConfig()
    cfg.host = "127.0.0.1"
    cfg.port = 0                         # ephemeral: parallel-safe
    server = ModemServer(cfg, hal, observer_token=OBS_TOKEN,
                         controller_token=CTL_TOKEN,
                         repeater_token=REP_TOKEN)
    assert await server.start(), "server failed to start"
    port = server._server.sockets[0].getsockname()[1]
    return server, hal, port


def _driver(port, token):
    return TCPLoRaRadio(host="127.0.0.1", port=port, token=token,
                        connect_timeout=5.0, **MESH)


async def _begin(radio) -> bool:
    """begin() is SYNCHRONOUS (the driver is thread-based by design) -
    run it off the loop or its blocking handshake starves the very
    server it is talking to. In the lab they are separate processes;
    in this harness the loop must stay free to serve."""
    return await asyncio.to_thread(radio.begin)


def test_real_driver_handshake_and_tx_through_the_repeater_door():
    async def main():
        server, hal, port = await _start_server()
        radio = _driver(port, REP_TOKEN)
        try:
            assert await _begin(radio)   # AUTH/PING/SET_CONFIG on a live link
            result = await radio.send(b"hello from the real driver")
            assert result is not None, "TX never came back TX_DONE"
            assert result["airtime_ms"] == pytest.approx(1.234)
            assert hal.txs == [b"hello from the real driver"]
            # it authed AS the repeater role - the lab's whole premise
            roles = [c.role for c in server._clients.values()]
            assert roles == [ROLE_REPEATER]
            # config authority stayed ours: echo, never apply
            assert hal.applied_configs == []
            live = struct.pack(
                "<IIBBbHB", MESH["frequency"], MESH["bandwidth"],
                MESH["spreading_factor"], MESH["coding_rate"],
                MESH["tx_power"], MESH["sync_word"], MESH["preamble_length"])
            assert server._config_bytes == live
        finally:
            radio.cleanup()
            await server.stop()
    asyncio.run(main())


def test_real_driver_rx_receives_the_fan_out():
    async def main():
        server, hal, port = await _start_server()
        radio = _driver(port, REP_TOKEN)
        try:
            assert await _begin(radio)
            got = []
            done = asyncio.Event()
            # the driver dispatches RX onto this loop (call_soon_threadsafe)
            radio.set_rx_callback(lambda data: (got.append(bytes(data)),
                                                done.set()))
            hal.on_rx_packet(RxPacket(rssi=-70, snr=8.5, signal_rssi=-72,
                                      data=b"heard by the real driver"))
            await asyncio.wait_for(done.wait(), 5.0)
            assert got == [b"heard by the real driver"]
            # signal facts survive the whole path into the driver
            assert radio.get_last_rssi() == -70
            assert radio.get_last_snr() == 8.5
        finally:
            radio.cleanup()
            await server.stop()
    asyncio.run(main())


def test_observer_token_driver_is_refused_the_air():
    async def main():
        server, hal, port = await _start_server()
        radio = _driver(port, OBS_TOKEN)
        try:
            assert await _begin(radio)   # the handshake itself is fine
            # the SERVER is ground truth: the driver really authed as
            # the observer (a dead/deferred link would fake this test).
            assert [c.role for c in server._clients.values()] == \
                [ROLE_OBSERVER]
            result = await radio.send(b"not mine to send")
            assert result is None, "observer TX must be refused"
            assert hal.txs == []
        finally:
            radio.cleanup()
            await server.stop()
    asyncio.run(main())


def test_real_driver_reads_the_noise_floor():
    async def main():
        server, hal, port = await _start_server()
        radio = _driver(port, REP_TOKEN)
        try:
            assert await _begin(radio)
            value = await radio.refresh_noise_floor()
            assert value == -104.5
            assert radio.get_noise_floor() == -104.5
        finally:
            radio.cleanup()
            await server.stop()
    asyncio.run(main())

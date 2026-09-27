"""Radio hardware selection - pimesh (the HAT) | ethermesh (the network modem).

Contract (2026-09-26 lab plan, Ch2):
- config.json's `radio_hardware` picks WHO owns the radio:
  "pimesh" (default) = the PiMesh 1W HAT on this Pi - the node embeds
  cleanmodem's radio server in-process (modem_conf).
  "ethermesh" = a MeshSmith EtherMesh-1W on the network - the node
  embeds NOTHING and dials the device like any other modem.
- Unknown values fail the config load (fail loud, never guess).
- HONEST LABEL: the ethermesh path is tested against a STAND-IN that
  speaks the documented wire protocol (TCP 5055 + token). The real
  EtherMesh firmware's live behavior (e.g. two simultaneous clients)
  is unverified until lab bring-up - these tests prove OUR side only.
"""
import asyncio
import json
import os
import struct
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from cleanmodem import frames  # noqa: E402
from meshtech_node import config as cfgmod  # noqa: E402
from meshtech_node.modemlink import ModemTransport  # noqa: E402
from meshtech_node.node import _embedded_radio, _modem_endpoint  # noqa: E402


class _S:
    """Minimal settings stand-in."""

    def __init__(self, **kw):
        self.companion_host = kw.get("companion_host", "127.0.0.1")
        self.companion_port = kw.get("companion_port", 5052)
        self.modem_conf = kw.get("modem_conf", "")
        self.radio_hardware = kw.get("radio_hardware", "pimesh")


# ─── config validation ───────────────────────────────────────────────

def _load(tmp_path, **extra):
    path = tmp_path / "config.json"
    data = {"channel": {"name": "#scope", "secret_hex": ""}}
    data.update(extra)
    path.write_text(json.dumps(data), encoding="utf-8")
    return cfgmod.load(str(path))


def test_default_hardware_is_pimesh(tmp_path):
    settings = _load(tmp_path)
    assert settings.radio_hardware == "pimesh"


def test_ethermesh_accepted_case_insensitive(tmp_path):
    settings = _load(tmp_path, radio_hardware="EtherMesh")
    assert settings.radio_hardware == "ethermesh"


def test_unknown_hardware_fails_loud(tmp_path):
    with pytest.raises(cfgmod.ConfigError, match="radio_hardware"):
        _load(tmp_path, radio_hardware="serial")


# ─── endpoint + embed decision ───────────────────────────────────────

def test_ethermesh_dials_the_device_not_modem_conf():
    # modem_conf describes THIS Pi's embedded server - in ethermesh
    # mode nothing is embedded, so the device endpoint wins.
    s = _S(radio_hardware="ethermesh", modem_conf="/opt/modem.conf",
           companion_host="10.0.0.7", companion_port=5055)
    assert _modem_endpoint(s) == ("10.0.0.7", 5055)


def test_pimesh_still_derives_endpoint_from_modem_conf(tmp_path):
    conf = tmp_path / "modem.conf"
    conf.write_text("host = 127.0.0.1\nport = 5055\n", encoding="utf-8")
    s = _S(radio_hardware="pimesh", modem_conf=str(conf),
           companion_port=5052)
    assert _modem_endpoint(s) == ("127.0.0.1", 5055)


def test_ethermesh_never_embeds_a_radio_server():
    s = _S(radio_hardware="ethermesh", modem_conf="/opt/modem.conf")
    assert _embedded_radio(s, bench=False) is None
    assert _embedded_radio(s, bench=True) is None


def test_pimesh_embeds_with_modem_conf():
    s = _S(radio_hardware="pimesh", modem_conf="/opt/modem.conf")
    radio = _embedded_radio(s, bench=False)
    assert radio is not None
    assert radio.path == "/opt/modem.conf"
    # bench mode and no modem_conf: nothing to embed.
    assert _embedded_radio(s, bench=True) is None
    assert _embedded_radio(_S(modem_conf=""), bench=False) is None


# ─── the ethermesh dial-out path against a stand-in ──────────────────

class StandInModem:
    """A network modem stand-in speaking the documented wire protocol:
    raw-token handshake (\\x01 ack), then full frames (PING/PONG,
    TX_REQUEST/TX_DONE, RX_PACKET pushes). NOT the real EtherMesh
    firmware - see the honest label in the module docstring."""

    def __init__(self, token="eth-token"):
        self.token = token
        self.received = []            # TX payloads seen
        self._server = None
        self._writers = []

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0)
        return self._server.sockets[0].getsockname()[1]

    async def stop(self):
        for w in self._writers:
            w.close()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    def push_rx(self, data, rssi=-80, snr=7.5):
        frame = frames.build_rx_packet(rssi, snr, -82, data)
        for w in self._writers:
            w.write(frame)

    async def _handle(self, reader, writer):
        self._writers.append(writer)
        try:
            first = await asyncio.wait_for(reader.read(256), 5.0)
            supplied = first.decode("utf-8", "replace").strip()
            if supplied != self.token:
                writer.write(b"\x00")
                await writer.drain()
                return
            writer.write(b"\x01")
            await writer.drain()
            buf = b""
            while True:
                parsed = frames.parse_frame(buf)
                if parsed is None:
                    chunk = await reader.read(4096)
                    if not chunk:
                        return
                    buf += chunk
                    continue
                cmd, payload, size = parsed
                buf = buf[size:]
                if cmd == frames.CMD_PING:
                    writer.write(frames.build_frame(frames.CMD_PONG))
                elif cmd == frames.CMD_TX_REQUEST:
                    self.received.append(bytes(payload))
                    writer.write(frames.build_frame(
                        frames.CMD_TX_DONE,
                        struct.pack(frames.TX_DONE_FMT, 2500)))
                await writer.drain()
        except (ConnectionResetError, BrokenPipeError, asyncio.TimeoutError):
            pass
        finally:
            if writer in self._writers:
                self._writers.remove(writer)
            writer.close()


def test_ethermesh_link_against_stand_in_modem():
    async def main():
        stand_in = StandInModem()
        port = await stand_in.start()
        modem = ModemTransport("127.0.0.1", port, "eth-token")
        modem.connect_timeout_s = 5.0
        try:
            await modem.start()
            assert modem.connected
            # the RX feed flows device -> node
            stand_in.push_rx(b"heard-on-air")
            pkt = await asyncio.wait_for(modem.__anext__(), 5.0)
            assert pkt.data == b"heard-on-air"
            assert pkt.rssi == -80
            # and TX flows node -> device (the sender's seam)
            assert await modem.send(b"scope-tx") is True
            assert stand_in.received == [b"scope-tx"]
        finally:
            modem.stop()
            await stand_in.stop()
    asyncio.run(main())


def test_ethermesh_link_refused_by_wrong_token():
    async def main():
        stand_in = StandInModem()
        port = await stand_in.start()
        modem = ModemTransport("127.0.0.1", port, "wrong-token")
        modem.connect_timeout_s = 3.0
        try:
            with pytest.raises(RuntimeError, match="did not come up"):
                await modem.start()
        finally:
            modem.stop()
            await stand_in.stop()
    asyncio.run(main())

"""The repeater role - openhop's driver repeats through THIS radio.

Contract (2026-09-26 lab plan, Ch1):
- The repeater role = observer feed + TX_REQUEST + CAD_REQUEST, all TX
  funneled through the same politeness gate as the controller.
- The radio config's single authority stays the controller (+ its
  modem.conf): a repeater's SET_CONFIG / SET_CAD_PARAMS get an ECHO of
  the live values, never an apply.
- Observers still cannot touch the air (ERR_UNAUTHORIZED).
- One repeater slot: a fresh repeater auth displaces a stale one.
- Token -> role is fail-closed and most-powerful-wins on a shared token.
- Feed listeners (observer + repeater) are exempt from the idle read
  recycle - openhop_core's TCPLoRaRadio is silent after its handshake.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

from cleanmodem import frames  # noqa: E402
from cleanmodem.config import ModemConfig  # noqa: E402
from cleanmodem.hal import RadioStatus, RxPacket, TxResult  # noqa: E402
from cleanmodem.server import (  # noqa: E402
    FEED_LISTENER_ROLES, ROLE_CONTROLLER, ROLE_NONE, ROLE_OBSERVER,
    ROLE_REPEATER, ModemServer)

OBS_TOKEN = "obs-token-1"
REP_TOKEN = "rep-token-1"
CTL_TOKEN = "ctl-token-1"


class FakeHal:
    """RadioHal stand-in: records every call, fakes every result."""

    def __init__(self):
        self.on_rx_packet = None
        self.on_hal_error = None
        self.started = False
        self.stopped = False
        self.txs = []                 # payloads passed to tx()
        self.cad_calls = 0
        self.applied_configs = []     # dicts passed to apply_config()

    async def start(self, loop):
        self.started = True
        return True

    async def stop(self):
        self.stopped = True

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


class FrameClient:
    """Minimal wire-protocol client (the openhop driver's side)."""

    def __init__(self, reader, writer):
        self.reader = reader
        self.writer = writer
        self.buf = b""

    async def send(self, cmd, payload=b""):
        self.writer.write(frames.build_frame(cmd, payload))
        await self.writer.drain()

    async def recv(self, timeout=3.0):
        """Next complete frame as (cmd, payload). Raises on EOF."""
        while True:
            parsed = frames.parse_frame(self.buf)
            if parsed is not None:
                cmd, payload, size = parsed
                self.buf = self.buf[size:]
                return cmd, payload
            chunk = await asyncio.wait_for(self.reader.read(4096), timeout)
            if not chunk:
                raise ConnectionError("server closed the connection")
            self.buf += chunk

    async def recv_cmd(self, want, timeout=3.0):
        """Next frame with the wanted cmd (others are skipped)."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            left = deadline - asyncio.get_running_loop().time()
            cmd, payload = await self.recv(timeout=max(left, 0.01))
            if cmd == want:
                return payload

    def close(self):
        self.writer.close()


async def _expect_dropped(client, timeout=5.0):
    """Wait until the server drops the connection (frames are skipped)."""
    end = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < end:
        try:
            await client.recv(timeout=0.3)
        except ConnectionError:
            return
        except asyncio.TimeoutError:
            continue
    raise AssertionError("connection was never dropped")


async def _connect(port):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    return FrameClient(reader, writer)


async def _auth(port, token, raw=False):
    """Connect and authenticate. raw=True uses the bot's byte handshake."""
    client = await _connect(port)
    if raw:
        client.writer.write(token.encode("utf-8"))
        await client.writer.drain()
        ack = await asyncio.wait_for(client.reader.read(1), 3.0)
        assert ack == b"\x01", f"raw-token auth refused (got {ack!r})"
    else:
        await client.send(frames.CMD_AUTH, token.encode("utf-8"))
        cmd, _ = await client.recv()
        assert cmd == frames.CMD_AUTH_OK, f"AUTH not accepted: 0x{cmd:02X}"
    return client


async def _start_server(hal=None, observer=OBS_TOKEN, controller=CTL_TOKEN,
                        repeater=REP_TOKEN):
    hal = hal or FakeHal()
    cfg = ModemConfig()
    cfg.host = "127.0.0.1"
    cfg.port = 0                         # ephemeral: parallel-safe
    cfg.politeness_seconds = 0.0         # no politeness napping in tests
    cfg.clear_channel_wait_seconds = 0.0
    cfg.lbt_enabled = False
    server = ModemServer(cfg, hal, observer_token=observer,
                         controller_token=controller,
                         repeater_token=repeater)
    assert await server.start(), "server failed to start"
    port = server._server.sockets[0].getsockname()[1]
    return server, hal, port


# ─── TX rights ───────────────────────────────────────────────────────

def test_repeater_can_tx_through_the_gate():
    async def main():
        server, hal, port = await _start_server()
        try:
            rep = await _auth(port, REP_TOKEN)
            await rep.send(frames.CMD_TX_REQUEST, b"hello mesh")
            cmd, payload = await rep.recv()
            assert cmd == frames.CMD_TX_DONE
            assert hal.txs == [b"hello mesh"]
            rep.close()
        finally:
            await server.stop()
    asyncio.run(main())


def test_repeater_can_cad():
    async def main():
        server, hal, port = await _start_server()
        try:
            rep = await _auth(port, REP_TOKEN)
            await rep.send(frames.CMD_CAD_REQUEST)
            cmd, payload = await rep.recv()
            assert cmd == frames.CMD_CAD_RESP
            assert payload == bytes([0])     # clear channel
            assert hal.cad_calls == 1
            rep.close()
        finally:
            await server.stop()
    asyncio.run(main())


def test_repeater_tx_via_raw_token_handshake():
    # The bot's byte handshake resolves the same roles - a raw-token
    # repeater gets the same TX rights as a frame-authed one.
    async def main():
        server, hal, port = await _start_server()
        try:
            rep = await _auth(port, REP_TOKEN, raw=True)
            await rep.send(frames.CMD_TX_REQUEST, b"raw path")
            cmd, _ = await rep.recv()
            assert cmd == frames.CMD_TX_DONE
            assert hal.txs == [b"raw path"]
            rep.close()
        finally:
            await server.stop()
    asyncio.run(main())


def test_observer_still_refused_tx_and_cad():
    async def main():
        server, hal, port = await _start_server()
        try:
            obs = await _auth(port, OBS_TOKEN)
            await obs.send(frames.CMD_TX_REQUEST, b"not mine")
            cmd, payload = await obs.recv()
            assert cmd == frames.CMD_ERROR
            assert payload == bytes([frames.ERR_UNAUTHORIZED])
            await obs.send(frames.CMD_CAD_REQUEST)
            cmd, payload = await obs.recv()
            assert cmd == frames.CMD_ERROR
            assert payload == bytes([frames.ERR_UNAUTHORIZED])
            assert hal.txs == []
            obs.close()
        finally:
            await server.stop()
    asyncio.run(main())


# ─── config authority ────────────────────────────────────────────────

def test_repeater_set_config_is_echoed_never_applied():
    async def main():
        server, hal, port = await _start_server()
        try:
            rep = await _auth(port, REP_TOKEN)
            import struct
            live = server._config_bytes       # the modem's live config
            other = struct.pack(frames.RADIO_CONFIG_FMT,
                                915000000, 125000, 9, 2, 10, 0x21, 8)
            assert other != live, "proposal must differ from live config"
            await rep.send(frames.CMD_SET_CONFIG, other)
            cmd, payload = await rep.recv()
            assert cmd == frames.CMD_CONFIG_RESP
            assert payload == live, "repeater proposal must not change config"
            assert server._config_bytes == live
            assert hal.applied_configs == [], "HAL must never see a tenant's config"
            rep.close()
        finally:
            await server.stop()
    asyncio.run(main())


def test_repeater_set_cad_params_is_echoed():
    async def main():
        server, hal, port = await _start_server()
        try:
            rep = await _auth(port, REP_TOKEN)
            await rep.send(frames.CMD_SET_CAD_PARAMS, bytes([22, 10]))
            cmd, payload = await rep.recv()
            assert cmd == frames.CMD_CAD_PARAMS_RESP
            assert payload == bytes([22, 10])   # echo only
            rep.close()
        finally:
            await server.stop()
    asyncio.run(main())


def test_controller_still_applies_config():
    async def main():
        server, hal, port = await _start_server()
        try:
            ctl = await _auth(port, CTL_TOKEN, raw=True)
            import struct
            new = struct.pack(frames.RADIO_CONFIG_FMT,
                              915000000, 125000, 9, 2, 10, 0x21, 8)
            await ctl.send(frames.CMD_SET_CONFIG, new)
            # recv_cmd skips the observer-state chip push at auth.
            payload = await ctl.recv_cmd(frames.CMD_CONFIG_RESP)
            assert payload == new
            assert len(hal.applied_configs) == 1
            assert hal.applied_configs[0]["frequency_hz"] == 915000000
            ctl.close()
        finally:
            await server.stop()
    asyncio.run(main())


# ─── slots and role resolution ───────────────────────────────────────

def test_second_repeater_displaces_the_first():
    async def main():
        server, hal, port = await _start_server()
        try:
            first = await _auth(port, REP_TOKEN)
            second = await _auth(port, REP_TOKEN)
            with pytest.raises(ConnectionError):
                await first.recv(timeout=3.0)
            await second.send(frames.CMD_PING)
            cmd, _ = await second.recv()
            assert cmd == frames.CMD_PONG
            first.close()
            second.close()
        finally:
            await server.stop()
    asyncio.run(main())


def test_role_resolution_most_powerful_wins_and_fails_closed():
    hal = FakeHal()
    cfg = ModemConfig()
    # One token shared by two roles: the MORE POWERFUL role wins, so a
    # shared token can never quietly hand a tenant extra rights.
    server = ModemServer(cfg, hal, observer_token="same",
                         repeater_token="same", controller_token="same")
    assert server._resolve_role("same") == ROLE_CONTROLLER
    server = ModemServer(cfg, hal, observer_token="a",
                         repeater_token="b", controller_token="c")
    assert server._resolve_role("c") == ROLE_CONTROLLER
    assert server._resolve_role("b") == ROLE_REPEATER
    assert server._resolve_role("a") == ROLE_OBSERVER
    assert server._resolve_role("nope") == ROLE_NONE
    assert server._resolve_role("") == ROLE_NONE
    # Fail closed: a role with no token file matches NOTHING, not "".
    server = ModemServer(cfg, hal, observer_token="", repeater_token="",
                         controller_token="")
    assert server._resolve_role("") == ROLE_NONE
    server = ModemServer(cfg, hal, observer_token="a",
                         repeater_token="", controller_token="c")
    assert server._resolve_role("b") == ROLE_NONE


def test_unknown_token_refused_on_auth():
    async def main():
        server, hal, port = await _start_server()
        try:
            client = await _connect(port)
            await client.send(frames.CMD_AUTH, b"wrong-token")
            cmd, payload = await client.recv()
            assert cmd == frames.CMD_ERROR
            assert payload == bytes([frames.ERR_UNAUTHORIZED])
            client.close()
        finally:
            await server.stop()
    asyncio.run(main())


# ─── feed-listener behavior ──────────────────────────────────────────

def test_repeater_is_a_feed_listener():
    # Structural guard: the idle-timeout exemption and the controller's
    # observer chip both key off FEED_LISTENER_ROLES.
    assert ROLE_REPEATER in FEED_LISTENER_ROLES
    assert ROLE_OBSERVER in FEED_LISTENER_ROLES
    assert ROLE_CONTROLLER not in FEED_LISTENER_ROLES


def test_feed_listener_survives_silence_while_controller_is_recycled(
        monkeypatch):
    # Shrink the read timeouts, then leave both links silent: the
    # repeater (feed listener, openhop's silent-after-handshake driver)
    # must stay, the controller (keepalive PINGer) must be recycled.
    import cleanmodem.server as server_mod
    monkeypatch.setattr(server_mod, "READ_TIMEOUT_S", 0.5)
    monkeypatch.setattr(server_mod, "AUTH_READ_TIMEOUT_S", 0.5)

    async def main():
        server, hal, port = await _start_server()
        try:
            rep = await _auth(port, REP_TOKEN)
            ctl = await _auth(port, CTL_TOKEN, raw=True)
            await asyncio.sleep(1.5)          # both silent
            await rep.send(frames.CMD_PING)   # repeater must still answer
            cmd, _ = await rep.recv()
            assert cmd == frames.CMD_PONG
            await _expect_dropped(ctl)        # controller was recycled
            rep.close()
        finally:
            await server.stop()
    asyncio.run(main())


def test_repeater_tx_is_echoed_to_the_observer_feed():
    async def main():
        server, hal, port = await _start_server()
        try:
            obs = await _auth(port, OBS_TOKEN)
            rep = await _auth(port, REP_TOKEN)
            await rep.send(frames.CMD_TX_REQUEST, b"loopback me")
            # The repeater hears its TX_DONE...
            cmd, _ = await rep.recv()
            assert cmd == frames.CMD_TX_DONE
            # ...and the observer feed stays complete (TX loopback).
            payload = await obs.recv_cmd(frames.CMD_RX_PACKET)
            rssi, snr, sig, data = frames.parse_rx_payload(payload)
            assert data == b"loopback me"
            obs.close()
            rep.close()
        finally:
            await server.stop()
    asyncio.run(main())


def test_tx_queue_still_gates_every_sender():
    # The politeness machinery is sender-agnostic: an oversized payload
    # is refused the same way for a repeater as for the controller.
    async def main():
        server, hal, port = await _start_server()
        try:
            rep = await _auth(port, REP_TOKEN)
            await rep.send(frames.CMD_TX_REQUEST, b"x" * 300)
            cmd, payload = await rep.recv()
            assert cmd == frames.CMD_ERROR
            assert payload == bytes([frames.ERR_PAYLOAD_TOO_BIG])
            assert hal.txs == []
            rep.close()
        finally:
            await server.stop()
    asyncio.run(main())

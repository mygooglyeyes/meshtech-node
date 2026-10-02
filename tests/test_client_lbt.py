"""Host-side LBT in ModemClient.send (Brett, 2026-10-02).

The modem's politeness is coming out, so every sender keeps its own
manners: ask the modem if the channel is busy, back off a random pick
of (120, 240, 360) ms until clear, transmit anyway at the 4 s budget.
A busy check that fails NEVER blocks the send. Recipe numbers pinned
against the reference source (openhop_core kiss_modem_wrapper
_prepare_for_tx_lbt).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest  # noqa: E402

import cleanmodem.client as cc  # noqa: E402
from cleanmodem import frames  # noqa: E402


async def _no_rx(rssi, snr, sig, data):
    return None


class FakeWriter:
    """A scripted modem: every CAD request consumes the next scripted
    busy flag (default quiet), and a TX request answers with an
    instant TX_DONE - exactly the pump's routing, without a socket."""

    def __init__(self, client, busy_script=()):
        self.client = client
        self.busy_script = list(busy_script)
        self.written = []       # frame command bytes, in write order

    def write(self, data):
        cmd, payload, size = frames.parse_frame(data)
        self.written.append(cmd)
        if cmd == frames.CMD_CAD_REQUEST:
            busy = self.busy_script.pop(0) if self.busy_script else False
            self.client._cad_reply.put_nowait(busy)
        elif cmd == frames.CMD_TX_REQUEST:
            self.client._tx_replies.put_nowait(True)

    async def drain(self):
        return None


def _client(busy_script=()):
    cli = cc.ModemClient("127.0.0.1", 5055, "tok", _no_rx)
    cli.connected = True
    writer = FakeWriter(cli, busy_script)
    cli._writer = writer
    return cli, writer


def _fast_lbt(monkeypatch, max_wait_s=0.2):
    monkeypatch.setattr(cc, "LBT_RETRY_DELAYS_MS", (12, 24, 36))
    monkeypatch.setattr(cc, "LBT_MAX_WAIT_S", max_wait_s)


def _record_sleeps(monkeypatch):
    slept = []

    async def fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(cc.asyncio, "sleep", fake_sleep)
    return slept


def test_the_openhop_recipe_numbers_are_pinned():
    # Verified against the reference source (openhop_core
    # kiss_modem_wrapper: LBT_RETRY_DELAYS_MS / LBT_MAX_WAIT_MS).
    assert cc.LBT_RETRY_DELAYS_MS == (120, 240, 360)
    assert cc.LBT_MAX_WAIT_S == 4.0


def test_a_clear_channel_sends_immediately(monkeypatch):
    _fast_lbt(monkeypatch)
    slept = _record_sleeps(monkeypatch)

    async def main():
        cli, writer = _client(busy_script=[False])
        assert await cli.send(b"\x7b\x00hi") is True
        assert writer.written == [frames.CMD_CAD_REQUEST,
                                  frames.CMD_TX_REQUEST]
        assert slept == []
    asyncio.run(main())


def test_busy_backs_off_the_recipe_then_sends(monkeypatch):
    _fast_lbt(monkeypatch)
    slept = _record_sleeps(monkeypatch)

    async def main():
        cli, writer = _client(busy_script=[True, True, False])
        assert await cli.send(b"\x7b\x00hi") is True
        # Two backoff rounds, then the TX - never before.
        assert writer.written == [frames.CMD_CAD_REQUEST,
                                  frames.CMD_CAD_REQUEST,
                                  frames.CMD_CAD_REQUEST,
                                  frames.CMD_TX_REQUEST]
        assert len(slept) == 2
        assert all(d in (0.012, 0.024, 0.036) for d in slept)
    asyncio.run(main())


def test_busy_at_the_budget_still_transmits(monkeypatch):
    _fast_lbt(monkeypatch, max_wait_s=0.05)
    slept = _record_sleeps(monkeypatch)

    async def main():
        # The channel NEVER clears - the budget runs out and the
        # packet still ships (the reference's exact behavior).
        cli, writer = _client(busy_script=[True] * 20)
        assert await cli.send(b"\x7b\x00hi") is True
        assert writer.written[-1] == frames.CMD_TX_REQUEST
        assert sum(slept) == pytest.approx(0.05)
    asyncio.run(main())


def test_a_failed_busy_check_never_blocks_the_send(monkeypatch):
    _fast_lbt(monkeypatch)
    slept = _record_sleeps(monkeypatch)

    async def main():
        cli, writer = _client()

        async def broken():
            raise ConnectionError("modem link down")

        cli._channel_busy = broken
        assert await cli.send(b"\x7b\x00hi") is True
        assert writer.written == [frames.CMD_TX_REQUEST]
        assert slept == []
    asyncio.run(main())

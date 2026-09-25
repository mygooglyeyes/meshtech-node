"""ModemTransport - the real-radio transport for the node.

SEED-MAP.md "the cleanmodem observer-feed transport": cleanmodem runs
ON THE BOX next to the radio (SPI/serial - the PiMesh 1W v2, no ch341,
corrected 2026-09-20 on Brett's challenge). The node talks to it over
the loopback TCP link with the bot's PROVEN ModemClient.

The bot used ModemClient as CONTROLLER (it TXed chat). The node's
default posture is LISTEN-ONLY: it connects as an OBSERVER where the
modem offers one, or as a never-transmitting controller where it does
not - either way every CMD_RX_PACKET the modem pushes lands here and
becomes an RxPacket (rssi, snr, signal_rssi, data) - exactly the
shape RawPacketSource consumes.

TX stays config-gated: the RadioSender guards every send; this
transport never transmits on its own. The controller token is read
from a mode-600 file (first line = password) - never committed.

Run: python -m meshtech_node.modemlink --host 127.0.0.1 --port 5055
     --token-file /path/to/token   (prints nothing on success)
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import AsyncIterator, Optional

from .rawsource import RxPacket

log = logging.getLogger("meshtech-node.modemlink")

CONNECT_TIMEOUT_S = 20.0


class ModemTransport:
    """Async-iterable bridge: ModemClient RX pushes -> RxPacket stream.

    Usage (the shell's real-mode wiring):
        modem = ModemTransport(host, port, token)
        source = RawPacketSource(transport=modem, channels=[channel])
    The transport implements __aiter__/__anext__ (the source's only
    contract) and owns the ModemClient lifecycle."""

    def __init__(self, host: str, port: int, token: str):
        self.host = host
        self.port = port
        self.token = token
        self._queue: "asyncio.Queue[Optional[RxPacket]]" = asyncio.Queue()
        self._client: Optional[object] = None
        self._task: Optional[asyncio.Task] = None
        self._closed = False
        self.rx_count = 0
        # Tests shrink this; production uses CONNECT_TIMEOUT_S.
        self.connect_timeout_s = CONNECT_TIMEOUT_S

    # ------------------------------------------------------- lifecycle --
    async def start(self) -> None:
        """Bring the modem link up; fail LOUDLY if it does not (the
        bot's v0.0.160 lesson: a failed init must tear its client down,
        never leave a retrying zombie fighting over the slot).

        REUSABLE (companion mode, 2026-09-20): a failed start must not
        poison the next attempt - the closed flag resets and any stale
        stop-sentinel drains before a fresh client is created."""
        from cleanmodem.client import ModemClient  # noqa: PLC0415
        self._closed = False
        while not self._queue.empty():
            self._queue.get_nowait()
        client = ModemClient(self.host, self.port, self.token,
                             self._on_rx)
        self._client = client
        self._task = asyncio.create_task(client.run(),
                                         name="modem-link")
        deadline = time.monotonic() + self.connect_timeout_s
        while not client.connected and time.monotonic() < deadline:
            if self._closed:
                return
            await asyncio.sleep(0.2)
        if not client.connected:
            self.stop()
            if self._task is not None:
                self._task.cancel()
            raise RuntimeError(
                f"modem link {self.host}:{self.port} did not come up "
                f"in {self.connect_timeout_s:.0f}s - check cleanmodem "
                "is running and the token matches")
        log.info("modem link up (%s:%s) - listen-only role, "
                 "RX feed live", self.host, self.port)

    def stop(self) -> None:
        self._closed = True
        if self._client is not None:
            try:
                self._client.stop()
            except Exception:
                log.exception("modem client stop raised - continuing")
        self._queue.put_nowait(None)   # unblock the iterator

    # ----------------------------------------------------------- RX ----
    async def _on_rx(self, rssi: int, snr: float, signal_rssi: int,
                     data: bytes) -> None:
        """ModemClient's RX callback - its _pump AWAITS it, so this must
        be a coroutine (a sync callback would crash the modem link on
        the first RX). Never raises: a bad packet is logged and dropped,
        the link lives."""
        try:
            if not data:
                return
            self.rx_count += 1
            self._queue.put_nowait(RxPacket(
                data=bytes(data), rssi=rssi, snr=snr,
                signal_rssi=signal_rssi))
        except Exception:
            log.exception("RX bridge dropped a packet - continuing")

    # ------------------------------------------- the source contract ----
    def __aiter__(self) -> AsyncIterator[RxPacket]:
        return self

    async def __anext__(self) -> RxPacket:
        item = await self._queue.get()
        if item is None:
            raise StopAsyncIteration
        return item

    @property
    def connected(self) -> bool:
        client = self._client
        return bool(client is not None and getattr(client, "connected",
                                                   False))

    @property
    def alive(self) -> bool:
        """True while the client task is running (it reconnects on its
        own) - the companion link's monitor restarts the transport ONLY
        when this is False, never against a live retrying client."""
        task = self._task
        return task is not None and not task.done()


async def wait_connected(modem: ModemTransport,
                         timeout_s: float = CONNECT_TIMEOUT_S) -> None:
    """Poll until the transport reports connected (shell convenience)."""
    deadline = time.monotonic() + timeout_s
    while not modem.connected and time.monotonic() < deadline:
        await asyncio.sleep(0.2)
    if not modem.connected:
        raise RuntimeError("modem transport not connected in time")

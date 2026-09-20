"""The bot's controller connection to cleanmodem.

One authenticated TCP session: RX_PACKETs arrive through the RX
callback; send() transmits via TX_REQUEST and resolves on TX_DONE /
TX_FAIL (the pump routes the reply). Reconnects with backoff; the idle
wait is capped so a dead link on a quiet mesh is noticed in minutes,
not hours (the old feed's hard-won lesson).
"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Awaitable, Callable, Optional, Tuple, Union

from . import frames

log = logging.getLogger("cleanmodem.client")

CONNECT_TIMEOUT_S = 5.0
TX_TIMEOUT_S = 15.0
IDLE_CAP_S = 120.0
IDLE_STEP_S = 2.0
RETRY_MIN_S = 2.0
RETRY_MAX_S = 30.0
MAX_BUFFER = 65_536
# The server recycles sessions that stay quiet for ~30 s; on a quiet
# mesh a controller that only TXs occasionally got recycled every
# ~32 s (hilltop 2026-09-14: down/2s/up flapping, TX landing in the
# gap failed). PING well inside that window keeps the session alive.
PING_INTERVAL_S = 15.0


class ModemClient:
    """Reconnects forever; failures log quietly and retry."""

    def __init__(self, host: str, port: int, token: str,
                 on_rx: Callable[[int, float, int, bytes], Awaitable[None]],
                 service=None) -> None:
        self.host = host
        self.port = port
        self.token = token
        self._on_rx = on_rx
        self._service = service
        self._writer: Optional[asyncio.StreamWriter] = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._tx_replies: "asyncio.Queue[bool]" = asyncio.Queue()
        self._tx_gate = asyncio.Lock()
        self._config_reply: "asyncio.Queue[bytes]" = asyncio.Queue()
        # v0.0.180: NOISE_REQ round-trips resolve on NOISE_RESP.
        self._noise_reply: "asyncio.Queue[float]" = asyncio.Queue()
        self.connected = False
        # v0.0.173: live observer count pushed by the modem (its TCP
        # push customers - openHop on hilltop). Drives the dashboard
        # chip truthfully; -1 = nothing received yet.
        self.observer_count = -1
        self.tx_count = 0
        self.rx_count = 0
        self._stop = asyncio.Event()

    # ── lifecycle ─────────────────────────────────────────────────────
    async def run(self) -> None:
        """Connect, authenticate, pump. Retries with backoff forever."""
        delay = RETRY_MIN_S
        while not self._stop.is_set():
            try:
                await self._connect_once()
                delay = RETRY_MIN_S
                keepalive = asyncio.create_task(self._keepalive())
                try:
                    await self._pump()
                finally:
                    keepalive.cancel()
                    try:
                        await keepalive
                    except asyncio.CancelledError:
                        pass
            except asyncio.CancelledError:
                raise
            except Exception as exc:      # noqa: BLE001
                log.debug("modem link: %s", exc)
            await self._close()
            if self._stop.is_set():
                break
            log.info("modem link down - retry in %gs", delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, RETRY_MAX_S)

    def stop(self) -> None:
        self._stop.set()

    async def _keepalive(self) -> None:
        """Periodic PING so the server's idle recycler leaves us alone.

        The protocol's CMD_PING exists for exactly this; the PONG also
        feeds the pump's idle clock, so the client-side 120 s cap stays
        honest on a quiet mesh too.
        """
        try:
            while True:
                await asyncio.sleep(PING_INTERVAL_S)
                writer = self._writer
                if writer is None:
                    return
                writer.write(frames.build_frame(frames.CMD_PING, b""))
                await writer.drain()
        except asyncio.CancelledError:
            raise
        except Exception:              # noqa: BLE001 - dead link: the pump notices and reconnects
            return

    # ── close ─────────────────────────────────────────────────────────
    async def _close(self) -> None:
        was = self.connected
        self.connected = False
        writer, self._writer = self._writer, None
        self._reader = None
        # A dead link must not strand a waiting sender.
        self._tx_replies.put_nowait(False)
        self._config_reply.put_nowait(b"")
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:            # noqa: BLE001  # nosec B110 - best-effort close during teardown
                pass
        if was and self._service is not None:
            self._service.feed.publish("modem_link_down", {})

    async def _connect_once(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT_S)
        try:
            writer.write(self.token.encode("utf-8"))
            await asyncio.wait_for(writer.drain(), CONNECT_TIMEOUT_S)
            answer = await asyncio.wait_for(reader.readexactly(1),
                                            CONNECT_TIMEOUT_S)
        except Exception:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:            # noqa: BLE001  # nosec B110 - best-effort close; the connect already failed
                pass
            raise
        if answer != b"\x01":
            raise PermissionError(
                "modem rejected the controller token (check the modem's "
                "controller token file against the bot's)")
        self._writer = writer
        self._reader = reader
        self.connected = True
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, "TCP_KEEPIDLE"):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,
                                    30)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL,
                                    10)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except OSError:
                pass
        log.info("modem link up (%s:%s) - controller role", self.host,
                 self.port)
        if self._service is not None:
            self._service.feed.publish("modem_link_up",
                                       {"host": self.host,
                                        "port": self.port})

    # ── RX pump (the only reader while connected) ─────────────────────
    async def _pump(self) -> None:
        writer = self._writer
        reader = self._reader
        if writer is None or reader is None:
            return
        buf = b""
        last_data = time.monotonic()
        while not self._stop.is_set():
            waited = time.monotonic() - last_data
            timeout = max(0.0, min(IDLE_STEP_S, IDLE_CAP_S - waited))
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout)
            except asyncio.TimeoutError:
                if writer.is_closing():
                    return
                if time.monotonic() - last_data >= IDLE_CAP_S:
                    log.info("modem link idle for %gs - reconnecting",
                             IDLE_CAP_S)
                    return
                continue
            if not chunk:
                return
            last_data = time.monotonic()
            buf += chunk
            if len(buf) > MAX_BUFFER:
                log.warning("modem input buffer overflow - dropping")
                buf = b""
                continue
            while True:
                try:
                    parsed = frames.parse_frame(buf)
                except frames.FrameError:
                    idx = frames.find_sync(buf, 1)
                    buf = buf[idx:] if idx >= 0 else b""
                    continue
                if parsed is None:
                    break
                cmd, payload, size = parsed
                buf = buf[size:]
                if cmd == frames.CMD_RX_PACKET:
                    try:
                        rssi, snr, sig, data = frames.parse_rx_payload(
                            payload)
                    except frames.FrameError:
                        continue
                    self.rx_count += 1
                    await self._on_rx(rssi, snr, sig, data)
                elif cmd == frames.CMD_TX_DONE:
                    self.tx_count += 1
                    self._tx_replies.put_nowait(True)
                elif cmd == frames.CMD_TX_FAIL:
                    self._tx_replies.put_nowait(False)
                elif cmd == frames.CMD_CONFIG_RESP:
                    self._config_reply.put_nowait(payload)
                elif cmd == frames.CMD_NOISE_RESP:
                    try:
                        self._noise_reply.put_nowait(
                            frames.parse_noise_payload_or_none(payload))
                    except frames.FrameError:
                        continue
                elif cmd == frames.CMD_OBSERVER_STATE:
                    self.observer_count = (payload[0] if payload else 0)
                elif cmd == frames.CMD_ERROR:
                    log.warning("modem error frame: 0x%02X",
                                payload[0] if payload else 0)
                # CONFIG_RESP / STATUS_RESP / PONG: the bot issues none
                # of those requests today, so they are ignored here.

    # ── TX / config / noise ──────────────────────────────────────────
    async def configure(self, config_payload: bytes) -> Optional[bytes]:
        """One SET_CONFIG (controller only); resolves on CONFIG_RESP.

        Returns the modem's live config echo (so the caller can log it
        against what it asked for), or None on a dead link / timeout.
        """
        writer = self._writer
        if writer is None or not self.connected:
            log.warning("modem link down - config push skipped (%dB)",
                        len(config_payload))
            return None
        async with self._tx_gate:
            while not self._config_reply.empty():
                try:
                    self._config_reply.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                writer.write(frames.build_frame(frames.CMD_SET_CONFIG,
                                                config_payload))
                await asyncio.wait_for(writer.drain(), TX_TIMEOUT_S)
                return await asyncio.wait_for(self._config_reply.get(),
                                              TX_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("config push timed out waiting for the modem")
                return None
            except Exception as exc:      # noqa: BLE001
                log.warning("config push failed: %s", exc)
                return None

    async def noise(self) -> Optional[float]:
        """One NOISE_REQ; resolves on NOISE_RESP (dBm) or None on a
        dead link / timeout. v0.0.180: feeds the dashboard's noise-floor
        monitor in modem mode - the controller asks, the chip's owner
        answers (the same instant-RSSI read the modem's own LBT uses).
        Observers may ask this too; the controller just happens to poll
        it on a schedule."""
        writer = self._writer
        if writer is None or not self.connected:
            return None
        async with self._tx_gate:
            while not self._noise_reply.empty():
                try:
                    self._noise_reply.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                writer.write(frames.build_frame(frames.CMD_NOISE_REQ, b""))
                await asyncio.wait_for(writer.drain(), TX_TIMEOUT_S)
                return await asyncio.wait_for(self._noise_reply.get(),
                                              TX_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.debug("NOISE_REQ timed out waiting for the modem")
                return None
            except Exception as exc:      # noqa: BLE001
                log.debug("NOISE_REQ failed: %s", exc)
                return None

    async def send(self, data: bytes) -> bool:
        """One TX_REQUEST; resolves on TX_DONE (True) / TX_FAIL (False)."""
        writer = self._writer
        if writer is None or not self.connected:
            log.warning("modem link down - TX dropped (%dB)",
                        len(data) if data else 0)
            return False
        # One in-flight TX at a time (the radio serializes anyway).
        async with self._tx_gate:
            # Drain stale replies (a late answer from a previous TX, or
            # the False a reconnect cycle pushed) so this send can only
            # be answered by ITS modem response.
            while not self._tx_replies.empty():
                try:
                    self._tx_replies.get_nowait()
                except asyncio.QueueEmpty:
                    break
            try:
                writer.write(frames.build_frame(frames.CMD_TX_REQUEST, data))
                await asyncio.wait_for(writer.drain(), TX_TIMEOUT_S)
                return await asyncio.wait_for(self._tx_replies.get(),
                                              TX_TIMEOUT_S)
            except asyncio.TimeoutError:
                log.warning("TX timed out waiting for the modem's answer")
                return False
            except Exception as exc:      # noqa: BLE001
                log.warning("TX failed: %s", exc)
                return False

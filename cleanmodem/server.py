"""The modem server: one port, role-by-token, wire-speed fan-out.

Roles:
- observer: the repeater's driver (RX feed only; TX refused server-side).
- controller: the bot (RX feed + exclusive TX rights).

Hardening (every rule here earned, see the plan):
- Auth is mandatory; tokens come from protected files; comparison is
  constant-time; per-connection throttling slows brute force; a missing
  token file means that role is unavailable (fail closed).
- Every connection has read timeouts (slowloris dies), a bounded input
  buffer, and a frame size cap; the connection cap protects the loop
  from floods (oldest dropped).
- Fan-out writes the SAME prebuilt frame bytes to every client and never
  awaits drain inside the loop; a slow client is dropped via the
  transport's write-buffer high-water mark, so it can never delay the
  others.
- Radio TX is serialized by the HAL; LBT/CAD + clear-channel wait +
  politeness gap run before every controller transmission; a controller
  with an oversized TX queue gets refused, not buffered forever.
- TX loopback: bot transmissions are echoed to observers as RX packets
  (a radio never hears itself), so the observer's log stays complete.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import socket
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

from . import frames
from .config import ModemConfig
from .hal import RadioHal, RadioStatus, RxPacket, TxResult

log = logging.getLogger("cleanmodem.server")

READ_TIMEOUT_S = 30.0           # between frames (slowloris guard)
AUTH_READ_TIMEOUT_S = 10.0      # during the handshake
MAX_BUFFER = 65_536             # input buffer cap (drop beyond this)
MAX_CONNECTIONS = 16            # per server (oldest dropped beyond this)
AUTH_MAX_ATTEMPTS = 3
AUTH_THROTTLE_S = 2.0
WRITE_HIGH_WATER = 256 * 1024   # slow-client drop threshold (bytes)
TX_QUEUE_DEPTH = 4              # concurrent TX requests per controller
DEMO_SYNC_WORD = 0x12
# Idle read timeout BETWEEN frames. Controllers renew it with their
# 15 s keepalive PING. Observers are EXEMPT (openhop_core's
# TCPLoRaRadio sends nothing after its handshake, so a live repeater
# was idle-recycled every ~60 s - hilltop 2026-09-14); a dead observer
# is still reaped by TCP keepalive (~60 s) + the transport checks.
OBSERVER_IDLE_TIMEOUT_S = None  # None = no idle timeout (read blocks)

ROLE_OBSERVER = "observer"
ROLE_CONTROLLER = "controller"
ROLE_NONE = "none"

# Commands any authenticated client may use; everything else needs the
# controller role (TX_REQUEST, CAD, SET_CAD_PARAMS). SET_CONFIG is
# special-cased in _dispatch: observers may PROPOSE config and get an
# echo answer (openhop_core's TCPLoRaRadio sends SET_CONFIG during its
# handshake and treats a rejection as a dead link), but the server
# only answers with its own live config and never applies a word of it.
OBSERVER_COMMANDS = frozenset({
    frames.CMD_PING, frames.CMD_STATUS_REQ, frames.CMD_NOISE_REQ,
    frames.CMD_GET_CONFIG, frames.CMD_GET_VERSION, frames.CMD_RX_START,
})


def _peer_s(writer: asyncio.StreamWriter) -> str:
    """Sanitized peer description for logs (no control characters)."""
    peer = writer.get_extra_info("peername")
    if isinstance(peer, tuple):
        host, port = peer[0], peer[1]
        return frames._sanitize(f"{host}:{port}", 60)
    return frames._sanitize(peer or "?", 60)


@dataclass
class ServerStats:
    started: float = field(default_factory=time.time)
    rx_count: int = 0
    tx_count: int = 0
    crc_errors: int = 0
    dropped_packets: int = 0
    auth_failures: int = 0
    clients_dropped: int = 0
    irq_to_fanout_ms: Tuple[float, ...] = ()


class ModemServer:
    """Owns the radio HAL and every client connection."""

    def __init__(self, cfg: ModemConfig, hal: RadioHal,
                 observer_token: str = "",     # nosec B107 - empty = role unavailable (fail closed), not a password
                 controller_token: str = "") -> None:
        self.cfg = cfg
        self.hal = hal
        self.observer_token = observer_token
        self.controller_token = controller_token
        self.stats = ServerStats()
        self._clients: Dict[asyncio.StreamWriter, ClientCtx] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._metrics_task: Optional[asyncio.Task] = None
        self._demo_task: Optional[asyncio.Task] = None
        self._config_bytes = self._pack_config()
        self._politeness_until = 0.0
        self._tx_gate = asyncio.Lock()
        self._last_tx_monotonic = 0.0

    # ── radio parameters ↔ wire config ────────────────────────────────
    def _pack_config(self) -> bytes:
        import struct
        return struct.pack(
            frames.RADIO_CONFIG_FMT,
            self.cfg.frequency_hz, self.cfg.bandwidth_hz,
            self.cfg.spreading_factor, self.cfg.coding_rate,
            self.cfg.tx_power_dbm, self.cfg.sync_word,
            self.cfg.preamble_length)

    # ── lifecycle ─────────────────────────────────────────────────────
    async def start(self) -> bool:
        loop = asyncio.get_running_loop()
        self.hal.on_rx_packet = self._on_rx_packet
        self.hal.on_hal_error = lambda msg: log.warning("radio: %s", msg)
        if not await self.hal.start(loop):
            log.error("radio failed to start - server not listening")
            return False
        self._server = await asyncio.start_server(
            self._handle_client, self.cfg.host, self.cfg.port)
        sockets = ", ".join(
            str(s.getsockname()) for s in
            (self._server.sockets or []))
        log.info("modem listening on %s (observer %s, controller %s)",
                 sockets,
                 "token set" if self.observer_token else "OPEN (no token)",
                 "token set" if self.controller_token else "UNAVAILABLE")
        self._metrics_task = asyncio.create_task(
            self._metrics_loop(), name="metrics")
        if self.cfg.demo_feed:
            self._demo_task = asyncio.create_task(
                self._demo_loop(), name="demo")
        return True

    async def stop(self) -> None:
        for task in (self._metrics_task, self._demo_task):
            if task is not None:
                task.cancel()
        # Drop clients BEFORE wait_closed(): on Python 3.12+
        # Server.wait_closed() waits for the connection handlers to
        # finish, so the handlers must see EOF first or shutdown hangs
        # with any client still attached.
        for writer in list(self._clients):
            self._drop(writer, "server stopping")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
        await self.hal.stop()
        log.info("modem stopped")

    # ── RX path (radio → every client) ────────────────────────────────
    def _on_rx_packet(self, pkt: RxPacket) -> None:
        """Radio thread → loop callback: one frame, every client."""
        try:
            frame = frames.build_rx_packet(
                pkt.rssi, pkt.snr, pkt.signal_rssi, pkt.data)
        except ValueError:
            return
        self.stats.rx_count += 1
        dead = []
        for writer, ctx in list(self._clients.items()):
            if not ctx.rx_enabled:
                continue
            try:
                if writer.transport is None or writer.transport.is_closing():
                    dead.append(writer)
                    continue
                # Slow-client guard BEFORE writing, not after: one client
                # above the high-water mark gets dropped immediately.
                transport = writer.transport
                if transport.get_write_buffer_size() > WRITE_HIGH_WATER:
                    dead.append(writer)
                    continue
                writer.write(frame)          # fire-and-forget; no drain here
            except Exception:                # noqa: BLE001
                dead.append(writer)
        for writer in dead:
            self._drop(writer, "slow or closed")
        self._note_latency(pkt.mono)

    def _note_latency(self, irq_at: float) -> None:
        """Track IRQ→fan-out latency (the 'wire speed' number)."""
        if not irq_at:
            return
        sample = (time.monotonic() - irq_at) * 1000.0
        window = self.stats.irq_to_fanout_ms
        if len(window) >= 256:
            window = window[128:]
        self.stats.irq_to_fanout_ms = window + (sample,)

    # ── client lifecycle ──────────────────────────────────────────────
    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        peer = _peer_s(writer)
        if len(self._clients) >= MAX_CONNECTIONS:
            oldest = next(iter(self._clients))
            log.warning("connection cap reached - dropping oldest (%s)",
                        _peer_s(oldest))
            self._drop(oldest, "connection cap")
        ctx = ClientCtx(peer=peer)
        self._clients[writer] = ctx
        sock = writer.get_extra_info("socket")
        if sock is not None:
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                if hasattr(socket, "TCP_KEEPIDLE"):
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 30)
                    sock.setsockopt(socket.IPPROTO_TCP,
                                    socket.TCP_KEEPINTVL, 10)
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3)
            except OSError:
                pass
        log.info("client connected (%s)", peer)
        buf = b""
        # Handshake peek: the repeater's driver speaks protocol frames
        # from byte one (AUTH 0x50 ...), the bot's controller link
        # sends its token as raw bytes and waits for one answer byte.
        # A first byte that is not SYNC selects the raw-token path.
        try:
            first = await asyncio.wait_for(reader.read(256),
                                           AUTH_READ_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.info("client %s sent nothing - closing", peer)
            self._drop(writer, "handshake timeout")
            return
        if not first:
            self._drop(writer, "closed before handshake")
            return
        if first[0] != frames.PROTO_SYNC:
            if not await self._raw_token_auth(ctx, writer, first):
                return
            buf = b""
        else:
            buf = first
        try:
            while True:
                # Drain everything already buffered BEFORE waiting for
                # more bytes: a request/response client sends one frame
                # and blocks on the answer, so reading first would stall
                # every exchange for the whole read timeout.
                progressed = True
                while progressed:
                    progressed = False
                    try:
                        parsed = frames.parse_frame(buf)
                    except frames.FrameError:
                        self.stats.crc_errors += 1
                        buf = await self._resync(reader, writer, buf, peer)
                        if buf is None:
                            return
                        progressed = bool(buf)
                        continue
                    if parsed is None:
                        break
                    cmd, payload, size = parsed
                    buf = buf[size:]
                    progressed = True
                    if not await self._dispatch(ctx, writer, cmd, payload):
                        return
                try:
                    if ctx.authenticated and ctx.role == ROLE_OBSERVER:
                        chunk = await reader.read(4096)   # no idle recycle
                    else:
                        chunk = await asyncio.wait_for(
                            reader.read(4096),
                            AUTH_READ_TIMEOUT_S if not ctx.authenticated
                            else READ_TIMEOUT_S)
                except asyncio.TimeoutError:
                    log.info("client %s timed out (%s)",
                             peer, "no auth" if not ctx.authenticated
                             else "idle")
                    break
                if not chunk:
                    break
                buf += chunk
                if len(buf) > MAX_BUFFER:
                    log.warning("client %s flooded the input buffer", peer)
                    buf = b""
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception:                    # noqa: BLE001 - never crash serve
            log.exception("client %s handler error", peer)
        finally:
            self._drop(writer, "disconnected")

    def _drop(self, writer: asyncio.StreamWriter, reason: str) -> None:
        ctx = self._clients.pop(writer, None)
        if ctx is None:
            return
        was_observer = ctx.role == ROLE_OBSERVER
        self.stats.clients_dropped += 1
        try:
            writer.close()
        except Exception:  # nosec B110 - best-effort close on an already-failing transport
            pass
        log.info("client %s gone (%s)", ctx.peer, reason)
        if was_observer:
            # v0.0.173: the chip on the controller's dashboard must
            # flip the moment the repeater leaves. _drop runs on the
            # event loop; schedule the push rather than await it.
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return                     # teardown without a loop
            loop.create_task(self._notify_observers_changed())

    async def _raw_token_auth(self, ctx: "ClientCtx",
                              writer: asyncio.StreamWriter,
                              first: bytes) -> bool:
        """The bot's handshake: raw token bytes, one answer byte.
        0x01 = accepted (role assigned), 0x00 = rejected + close.
        Single attempt per connection (the protocol AUTH path keeps
        its own throttling for frame-based clients)."""
        supplied = first.decode("utf-8", "replace").strip()
        role = ROLE_NONE
        if self.controller_token and hmac.compare_digest(
                supplied, self.controller_token):
            role = ROLE_CONTROLLER
        elif self.observer_token and hmac.compare_digest(
                supplied, self.observer_token):
            role = ROLE_OBSERVER
        if role == ROLE_NONE:
            self.stats.auth_failures += 1
            log.warning("raw-token auth rejected for %s", ctx.peer)
            try:
                writer.write(b"\x00")
                await writer.drain()
            except (ConnectionResetError, BrokenPipeError):
                pass
            self._drop(writer, "auth rejected")
            return False
        if role == ROLE_CONTROLLER:
            for other_writer, other in list(self._clients.items()):
                if other is not ctx and other.role == ROLE_CONTROLLER:
                    log.warning("controller displaced by %s", ctx.peer)
                    self._drop(other_writer, "displaced")
        ctx.authenticated = True
        ctx.role = role
        log.info("auth accepted: %s as %s (raw token)", ctx.peer, role)
        try:
            writer.write(b"\x01")
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return False
        if role == ROLE_CONTROLLER:
            # v0.0.173: initial observer count so the controller's
            # dashboard starts truthful ("TCP Push" chip).
            await self._notify_observers_changed()
        elif role == ROLE_OBSERVER:
            await self._notify_observers_changed()
        return True

    def _observer_count(self) -> int:
        return sum(1 for c in self._clients.values()
                   if c.role == ROLE_OBSERVER)

    async def _notify_observers_changed(self) -> None:
        """Tell the controller how many observers are connected.

        One byte: the count. Controller only - the repeater's driver
        must never see an unsolicited frame it did not ask for. Fire
        and forget: a missing update only delays the chip, never the
        radio.
        """
        count = self._observer_count()
        for writer, ctx in list(self._clients.items()):
            if ctx.role != ROLE_CONTROLLER or ctx is None:
                continue
            try:
                if writer.transport is None or writer.transport.is_closing():
                    continue
                await self._send(writer, frames.CMD_OBSERVER_STATE,
                                 bytes([count]))
            except Exception:            # noqa: BLE001 - best-effort notification
                pass
        log.debug("observer state pushed: %d connected", count)

    async def _resync(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter, buf: bytes,
                      peer: str) -> Optional[bytes]:
        """Answer a bad frame, then resync to the next SYNC byte."""
        try:
            writer.write(frames.build_frame(
                frames.CMD_ERROR, bytes([frames.ERR_CRC_MISMATCH])))
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            return None
        idx = frames.find_sync(buf, 1)
        if idx < 0:
            # Nothing usable left; ask for more bytes with a deadline.
            try:
                chunk = await asyncio.wait_for(reader.read(4096),
                                               READ_TIMEOUT_S)
            except asyncio.TimeoutError:
                return None
            if not chunk:
                return None
            buf = buf + chunk
            idx = frames.find_sync(buf)
        return buf[idx:] if idx >= 0 else b""

    # ── command dispatch ──────────────────────────────────────────────
    async def _dispatch(self, ctx: "ClientCtx", writer: asyncio.StreamWriter,
                        cmd: int, payload: bytes) -> bool:
        """Handle one command. Returns False to close the connection."""
        if not ctx.authenticated and cmd != frames.CMD_AUTH:
            if cmd == frames.CMD_PING:
                # Ping stays answerable pre-auth so the driver's very
                # first probe gets a reply even before its AUTH.
                await self._send(writer, frames.CMD_PONG)
                return True
            self.stats.auth_failures += 1
            await self._send(writer, frames.CMD_ERROR,
                             bytes([frames.ERR_UNAUTHORIZED]))
            return True     # session kept; the read timeout bounds idle

        if cmd == frames.CMD_AUTH:
            return await self._handle_auth(ctx, writer, payload)

        if cmd == frames.CMD_PING:
            await self._send(writer, frames.CMD_PONG)
            return True

        # TX and CAD need the controller role - enforced HERE, so a
        # misconfigured observer can never touch the air. SET_CONFIG and
        # SET_CAD_PARAMS fall through: below, observers get their
        # proposal answered with an echo (read-only) - openhop_core's
        # TCPLoRaRadio handshakes with both and treats rejections as a
        # dead link.
        if cmd not in OBSERVER_COMMANDS \
                and cmd not in (frames.CMD_SET_CONFIG,
                                frames.CMD_SET_CAD_PARAMS) \
                and ctx.role != ROLE_CONTROLLER:
            self.stats.auth_failures += 1
            log.warning("TX attempt by role=%s (%s) - refused",
                        ctx.role, ctx.peer)
            await self._send(writer, frames.CMD_ERROR,
                             bytes([frames.ERR_UNAUTHORIZED]))
            return True

        if cmd == frames.CMD_TX_REQUEST:
            await self._handle_tx(writer, payload)
            return True

        if cmd in (frames.CMD_SET_CONFIG, frames.CMD_GET_CONFIG):
            if cmd == frames.CMD_SET_CONFIG:
                if len(payload) != frames.RADIO_CONFIG_SIZE:
                    await self._send(writer, frames.CMD_ERROR,
                                     bytes([frames.ERR_PAYLOAD_TOO_BIG]))
                    return True
                if ctx.role == ROLE_CONTROLLER:
                    self._config_bytes = payload
                    log.info("radio config updated by controller: %s",
                             self._describe_config(payload))
                    ok = await self.hal.apply_config(
                        self._unpack_config(payload))
                    if not ok:
                        log.warning("radio rejected the new config")
                else:
                    # Observer proposal: validate the shape, answer with
                    # the LIVE config. The chip parameters stay ours;
                    # openhop_core's driver just needs an echo to call
                    # the link healthy.
                    self._describe_config(payload)      # raises on malformed
                    log.info("observer config proposal: %s (kept %s)",
                             self._describe_config(payload),
                             self._describe_config(self._config_bytes))
            await self._send(writer, frames.CMD_CONFIG_RESP,
                             self._config_bytes)
            return True

        if cmd == frames.CMD_CAD_REQUEST:
            try:
                busy = await self.hal.cad()
                await self._send(writer, frames.CMD_CAD_RESP,
                                 bytes([1 if busy else 0]))
            except Exception:                # noqa: BLE001
                await self._send(writer, frames.CMD_ERROR,
                                 bytes([frames.ERR_CAD_FAILED]))
            return True

        if cmd == frames.CMD_SET_CAD_PARAMS:
            if ctx.role != ROLE_CONTROLLER:
                # Observer proposal (repeater restores its cached CAD
                # settings at connect): echo only - CAD runs before a
                # TX and observers cannot TX, so the live CAD params
                # (the controller's pre-check tuning) stay untouched.
                log.info("observer CAD params proposal (echoed): %s",
                         payload.hex())
            await self._send(writer, frames.CMD_CAD_PARAMS_RESP, payload)
            return True

        if cmd == frames.CMD_RX_START:
            ctx.rx_enabled = True
            await self._send(writer, frames.CMD_RX_STARTED)
            return True

        if cmd == frames.CMD_STATUS_REQ:
            status = await self.hal.status()
            await self._send(writer, frames.CMD_STATUS_RESP,
                             self._pack_status(status))
            return True

        if cmd == frames.CMD_NOISE_REQ:
            try:
                noise = await self.hal.noise()
            except Exception as exc:         # noqa: BLE001
                # v0.0.183: NO answer beats a FABRICATED one. The old
                # silent -105.0 (inherited from meshtech-modem's
                # hard-coded pack('<h', -1050)) made a dead read
                # indistinguishable from a genuinely quiet channel -
                # exactly how a frozen -105 line hid on the dashboard.
                # The sentinel tells the bot to leave a graph gap.
                log.warning("NOISE_REQ failed, answering NO-VALUE: %s", exc)
                noise = None
            if noise is None:
                await self._send(writer, frames.CMD_NOISE_RESP,
                                 frames.NOISE_NO_VALUE)
            else:
                import struct
                await self._send(writer, frames.CMD_NOISE_RESP,
                                 struct.pack("<h", int(round(noise * 10))))
            return True

        if cmd == frames.CMD_GET_VERSION:
            await self._send(writer, frames.CMD_VERSION_RESP,
                             bytes([1, 0]))
            return True

        log.warning("unknown command 0x%02X from %s", cmd, ctx.peer)
        await self._send(writer, frames.CMD_ERROR,
                         bytes([frames.ERR_INVALID_CMD]))
        return True

    async def _handle_auth(self, ctx: "ClientCtx",
                           writer: asyncio.StreamWriter,
                           payload: bytes) -> bool:
        supplied = payload.decode("utf-8", "replace")
        # Brute-force throttle: each failed attempt costs a delay, and
        # too many closes the connection.
        if ctx.auth_attempts >= AUTH_MAX_ATTEMPTS:
            log.warning("client %s exceeded auth attempts - closing",
                        ctx.peer)
            return False
        if ctx.auth_attempts:
            await asyncio.sleep(AUTH_THROTTLE_S * ctx.auth_attempts)
        ctx.auth_attempts += 1

        role = ROLE_NONE
        if self.controller_token and hmac.compare_digest(
                supplied, self.controller_token):
            role = ROLE_CONTROLLER
        elif self.observer_token and hmac.compare_digest(
                supplied, self.observer_token):
            role = ROLE_OBSERVER
        elif not self.observer_token:
            # No observer token configured = fail closed for the role;
            # only the controller token (if any) is accepted.
            role = ROLE_NONE

        if role == ROLE_NONE:
            self.stats.auth_failures += 1
            log.warning("auth rejected for %s (attempt %d)",
                        ctx.peer, ctx.auth_attempts)
            await self._send(writer, frames.CMD_ERROR,
                             bytes([frames.ERR_UNAUTHORIZED]))
            return True

        if role == ROLE_CONTROLLER:
            # Single controller slot: a new controller displaces a stale
            # one (self-healing after a bot crash).
            for other_writer, other in list(self._clients.items()):
                if other is not ctx and other.role == ROLE_CONTROLLER:
                    log.warning("controller displaced by %s", ctx.peer)
                    self._drop(other_writer, "displaced")
        ctx.authenticated = True
        ctx.role = role
        log.info("auth accepted: %s as %s", ctx.peer, role)
        await self._send(writer, frames.CMD_AUTH_OK)
        if role in (ROLE_CONTROLLER, ROLE_OBSERVER):
            # v0.0.173: observer join/leave -> the controller's chip.
            await self._notify_observers_changed()
        return True

    async def _handle_tx(self, writer: asyncio.StreamWriter,
                         payload: bytes) -> None:
        """One controller transmission with the full politeness ritual."""
        if not payload or len(payload) > frames.MAX_LORA_PAYLOAD:
            await self._send(writer, frames.CMD_ERROR,
                             bytes([frames.ERR_PAYLOAD_TOO_BIG]))
            return
        if self._tx_gate.locked() and self._tx_gate._waiters and \
                len(self._tx_gate._waiters) > TX_QUEUE_DEPTH:
            await self._send(writer, frames.CMD_ERROR,
                             bytes([frames.ERR_RADIO_BUSY]))
            return
        async with self._tx_gate:
            # Politeness gap between OUR OWN packets.
            now = time.monotonic()
            if now < self._politeness_until:
                await asyncio.sleep(self._politeness_until - now)
            # Clear-channel wait: CAD, jittered retries, capped.
            waited = await self._wait_clear_channel()
            if waited:
                log.info("clear-channel wait engaged before TX")
            result: TxResult = await self.hal.tx(payload)
        if result.ok:
            self.stats.tx_count += 1
            self._last_tx_monotonic = time.monotonic()
            self._politeness_until = (self._last_tx_monotonic +
                                      self.cfg.politeness_seconds)
            import struct
            await self._send(writer, frames.CMD_TX_DONE,
                             struct.pack(frames.TX_DONE_FMT,
                                         result.airtime_us))
            # TX loopback: observers see what the bot sent (a radio
            # never hears itself) with synthetic metadata, exactly as
            # the old feed did.
            self._on_rx_packet(RxPacket(rssi=-100, snr=0.0,
                                        signal_rssi=-100,
                                        data=bytes(payload)))
        else:
            log.warning("TX failed: %s", result.error)
            await self._send(writer, frames.CMD_TX_FAIL)

    async def _wait_clear_channel(self) -> bool:
        """CAD pre-check with continuous random backoff (0.10-0.30 s),
        up to the cap. Transmits anyway at the cap. True if it had to
        wait."""
        cap = self.cfg.clear_channel_wait_seconds
        if cap <= 0 or not self.cfg.lbt_enabled:
            return False
        import random
        deadline = time.monotonic() + cap
        waited = False
        while True:
            try:
                busy = await self.hal.cad(self.cfg.cad_peak, self.cfg.cad_min)
            except Exception:                # noqa: BLE001
                return waited                 # CAD unavailable: send anyway
            if not busy:
                return waited
            if time.monotonic() >= deadline:
                log.warning("clear-channel cap hit - transmitting anyway "
                            "(this packet may collide)")
                return True
            waited = True
            # uniform() is fine here: LBT jitter needs only
            # non-correlated timing, not cryptographic strength. The
            # range matches the proven old-stack backoffs (102-295 ms
            # observed on air); a continuous spread keeps nodes from
            # aligning on the same retry slots.
            await asyncio.sleep(random.uniform(0.10, 0.30))  # nosec B311

    # ── plumbing ──────────────────────────────────────────────────────
    async def _send(self, writer: asyncio.StreamWriter, cmd: int,
                    payload: bytes = b"") -> None:
        writer.write(frames.build_frame(cmd, payload))
        try:
            await writer.drain()
        except (ConnectionResetError, BrokenPipeError):
            self._drop(writer, "write failed")

    def _pack_status(self, status: RadioStatus) -> bytes:
        import struct
        return struct.pack(
            frames.STATUS_RESP_FMT,
            status.uptime_s, status.rx_count, status.tx_count,
            status.crc_errors, status.last_rssi, status.last_snr_x10,
            status.noise_x10, 40, status.radio_state,
            status.irq_polls, status.irq_edges, status.last_irq_flags)

    @staticmethod
    def _unpack_config(payload: bytes) -> dict:
        import struct
        (freq, bw, sf, cr, power, sync_word, pre) = struct.unpack(
            frames.RADIO_CONFIG_FMT, payload)
        return {"frequency_hz": freq, "bandwidth_hz": bw,
                "spreading_factor": sf, "coding_rate": cr,
                "tx_power_dbm": power, "sync_word": sync_word,
                "preamble_length": pre}

    @staticmethod
    def _describe_config(payload: bytes) -> str:
        import struct
        (freq, bw, sf, cr, power, sync_word, pre) = struct.unpack(
            frames.RADIO_CONFIG_FMT, payload)
        return (f"{freq / 1e6:.3f}MHz BW{bw / 1000:g}kHz SF{sf} CR{cr} "
                f"{power}dBm sync=0x{sync_word:02X} pre={pre}")

    async def _metrics_loop(self) -> None:
        """One metrics line per minute: counts + latency percentiles."""
        while True:
            await asyncio.sleep(60.0)
            latencies = sorted(self.stats.irq_to_fanout_ms)
            if latencies:
                p50 = latencies[len(latencies) // 2]
                p99 = latencies[min(len(latencies) - 1,
                                    int(len(latencies) * 0.99))]
                lat = f"irq→fanout p50={p50:.1f}ms p99={p99:.1f}ms"
            else:
                lat = "irq→fanout (no traffic)"
            log.info(
                "metrics: rx=%d tx=%d crc_err=%d dropped=%d auth_fail=%d "
                "clients=%d %s",
                self.stats.rx_count, self.stats.tx_count,
                self.stats.crc_errors, self.stats.dropped_packets,
                self.stats.auth_failures, len(self._clients), lat)
            # v0.0.155: IRQ diagnostics in the metrics line (poll/edge
            # counters + last flag word) - makes a deaf RX visible.
            try:
                hs = await self.hal.status()
                log.info(
                    "irq: polls=%d edges=%d flags=0x%04X poll_mode=%s",
                    hs.irq_polls, hs.irq_edges, hs.last_irq_flags,
                    getattr(self.hal, "irq_poll_mode", False))
            except Exception:                # noqa: BLE001
                pass

    async def _demo_loop(self) -> None:
        """Synthetic signed advert every demo_interval (bench testing)."""
        n = 0
        while True:
            await asyncio.sleep(self.cfg.demo_interval)
            n += 1
            pkt = self._demo_advert(n)
            if pkt is not None:
                self._on_rx_packet(RxPacket(rssi=-45, snr=9.5,
                                            signal_rssi=-50, data=pkt))
                log.info("demo advert #%d injected", n)

    @staticmethod
    def _demo_advert(n: int) -> Optional[bytes]:
        """A well-formed signed flood advert (MeshCore wire format)."""
        try:
            from nacl.signing import SigningKey
        except ImportError:
            log.warning("demo_feed needs pynacl - demo disabled")
            return None
        seed = hashlib.sha256(
            b"cleanmodem demo node seed (public)").digest()
        key = SigningKey(seed)
        pubkey = bytes(key.verify_key.encode())
        ts = int(time.time() + n).to_bytes(4, "little")
        appdata = bytes([0x81]) + b"DEMO"
        signature = key.sign(pubkey + ts + appdata).signature
        header = 0x11                   # flood route, ADVERT, version 0
        return bytes([header, 0x00]) + pubkey + ts + signature + appdata


class ClientCtx:
    """Per-connection state."""

    def __init__(self, peer: str) -> None:
        self.peer = peer
        self.authenticated = False
        self.role = ROLE_NONE
        self.auth_attempts = 0
        self.rx_enabled = True

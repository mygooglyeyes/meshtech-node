"""Companion client - the plugin's radio link (DORMANT in the node).

Connects to the repeater's companion frame server over TCP using the
official ``meshcore`` library (same as the answerbot), supervises the
connection, and adds the two scope-specific commands:

- TX: CMD_SEND_CHANNEL_DATA (62) - GRP_DATA send on the scope channel
- RX: CHANNEL_DATA_RECV events - ALL scope packet types: client
  REFRESH_REQ uplinks AND peer hosts' broadcasts (the peer table is
  built from overhearing LAYOUTs, which costs zero airtime)

The companion holds the keys; the plugin never does.

NODE STATUS (2026-09-20): the node's RadioSender (Adapter B) replaced
this class for transport, and the node never constructs it - it is
kept only as the proven wire-frame reference (its static helpers and
the module-level scope_secret/scope_wire_body ARE used). The meshcore
library is imported lazily INSIDE the class, so the node (which never
runs this class) has no dependency on it - a missing library raises a
clear error naming this class, and never breaks the node's startup.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import struct
import time
from typing import Awaitable, Callable, Optional

from . import codec
from .config import Settings

log = logging.getLogger("meshtech-scope.client")

MAX_CHANNEL_SLOTS = 8
# Reference frame server: MAX_FRAME_SIZE 172 minus 9 (companion channel
# binary payload cap, matching the firmware parser).
MAX_CHANNEL_DATA_LENGTH = 163
# Companion command acks: openHop transmits but never acks the command
# itself (verified pattern from the answerbot). Keep the wait short.
SEND_ACK_TIMEOUT = 3.0
_MSG_ACK_TIMEOUT = 0.2
_NO_ACK_REASONS = frozenset({"no_event_received", "timeout"})


def scope_secret(channel_name: str, secret_hex: str = "") -> bytes:
    """The channel secret: configured hex, or the standard hashtag
    derivation sha256('#name')[:16]."""
    if secret_hex:
        return bytes.fromhex(secret_hex)
    return hashlib.sha256(channel_name.encode("utf-8")).digest()[:16]


def scope_wire_body(data_type: int, payload: bytes) -> bytes:
    """Body of a full scope plaintext, verified against data_type.

    The radio adds its own type/len envelope when building the GRP_DATA
    plaintext (openhop_core companion_base.send_channel_data wraps the
    command payload as pack('<HB', type, len) + body), so the
    CMD_SEND_CHANNEL_DATA frame must carry the body ONLY. Passing the
    full plaintext double-wraps the packet on air - the 2026-09-18 bug
    that shifted every client field by 3 bytes and hid hilltop's
    refresh answers.
    """
    payload = bytes(payload)
    if len(payload) < 3:
        raise ValueError("payload too short for scope framing")
    wire_type, body_len = struct.unpack_from("<HB", payload, 0)
    if wire_type != data_type:
        raise ValueError(
            f"payload framing {wire_type:#06x} != data_type {data_type:#06x}")
    if len(payload) - 3 < body_len:
        raise ValueError("payload body truncated")
    return payload[3:]


def _meshcore():
    """The meshcore library, imported ONLY when the dormant
    companion-radio client below actually runs. The node never calls
    this: a missing library raises a clear, named error instead of
    killing the whole program at import time (the 2026-09-20 lesson).
    """
    try:
        from meshcore import EventType, MeshCore
    except ImportError as exc:
        raise RuntimeError(
            "CompanionClient needs the 'meshcore' library "
            "(pip install meshcore). The node itself never uses this "
            "class - this error means something started the dormant "
            "companion-radio path.") from exc
    return EventType, MeshCore


class CompanionClient:
    def __init__(self, settings: Settings,
                 on_packet: Optional[Callable[[object, str],
                                              Awaitable[None]]] = None):
        self.settings = settings
        self.mc: Optional[object] = None  # meshcore instance, on connect
        self.is_connected = False
        self._on_packet = on_packet
        self._slot: Optional[int] = None
        self._send_lock: Optional[asyncio.Lock] = None
        self._own_prefix: str = ""

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        delay = 3.0
        while True:
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Connection problem: %s", exc)
            finally:
                await self._teardown()
            log.info("Reconnecting in %.0fs ...", delay)
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            delay = min(delay * 2, 60.0)

    async def _connect_and_serve(self) -> None:
        EventType, MeshCore = _meshcore()
        log.info("Connecting to %s:%s ...", self.settings.companion_host,
                 self.settings.companion_port)
        mc = await MeshCore.create_tcp(self.settings.companion_host,
                                       self.settings.companion_port,
                                       auto_reconnect=False)
        self.mc = mc
        self._apply_ack_timeout(mc)
        self.is_connected = True
        log.info("Connected to %s:%s", self.settings.companion_host,
                 self.settings.companion_port)

        try:
            key = str((mc.self_info or {}).get("public_key", "") or "").lower()
            if len(key) >= 2:
                self._own_prefix = key[:12]
                log.info("Scope companion pubkey prefix: %s", self._own_prefix)
        except Exception:
            pass

        await self._ensure_scope_channel()
        self._subscribe()

        # Serve until the connection drops.
        while True:
            await asyncio.sleep(1.0)
            if not bool(getattr(self.mc, "is_connected", False)):
                log.info("Connection to radio lost.")
                break

    async def _teardown(self) -> None:
        self.is_connected = False
        mc, self.mc = self.mc, None
        if mc is not None:
            try:
                await mc.stop_auto_message_fetching()
            except Exception:
                pass
            try:
                await mc.disconnect()
            except Exception:
                pass

    def _apply_ack_timeout(self, mc) -> None:
        try:
            mc.commands.default_timeout = SEND_ACK_TIMEOUT
        except AttributeError:
            log.warning("Could not set companion ack timeout (library layout "
                        "changed) - keeping the library default")

    def _send_ctx(self):
        if self._send_lock is None:
            self._send_lock = asyncio.Lock()
        default = None
        try:
            default = self.mc.commands.default_timeout
        except AttributeError:
            pass
        return self._send_lock, default

    # ------------------------------------------------------------------ channel

    async def _ensure_scope_channel(self) -> None:
        """Find or create the scope channel in a companion slot."""
        EventType, _ = _meshcore()
        cfg = self.settings.channel
        wanted = cfg.name.lstrip("#").casefold()
        slot_info: dict = {}
        for idx in range(MAX_CHANNEL_SLOTS):
            try:
                result = await self.mc.commands.get_channel(idx)
            except Exception:
                continue
            if result is None or getattr(result, "type", None) == EventType.ERROR:
                continue
            payload = result.payload or {}
            name = payload.get("name")
            if isinstance(name, bytes):
                name = name.decode("utf-8", "replace")
            slot_info[idx] = (name or "").strip()
            if (name or "").strip().lstrip("#").casefold() == wanted:
                self._slot = idx
                log.info("Scope channel %s found in companion slot %d",
                         cfg.name, idx)
                return
        free = self.settings.channel.companion_slot or None
        if free is None:
            free = next((idx for idx in range(1, MAX_CHANNEL_SLOTS)
                         if not slot_info.get(idx)), None)
        if free is None:
            log.warning("No free companion slot for %s - configure it "
                        "server-side; feed TX disabled until then.", cfg.name)
            return
        secret = scope_secret(cfg.name, cfg.secret_hex)
        ok = await self._try(
            f"set channel {cfg.name} in slot {free}",
            lambda: self.mc.commands.set_channel(free, cfg.name, secret))
        if ok:
            self._slot = free
            log.info("Scope channel %s set in companion slot %d",
                     cfg.name, free)

    async    def _try(self, what: str, command_fn, quiet: bool = False,
                   ack_expected: bool = True) -> bool:
        EventType, _ = _meshcore()
        try:
            result = await command_fn()
            if result is not None and getattr(result, "type", None) == EventType.ERROR:
                reason = ""
                payload = getattr(result, "payload", None)
                if isinstance(payload, dict):
                    reason = str(payload.get("reason", ""))
                if ack_expected is False and reason in _NO_ACK_REASONS:
                    log.debug("%s: no ack (treated as delivered)", what)
                    return True
                if quiet:
                    log.info("%s not accepted (harmless): %s", what,
                             getattr(result, "payload", "error"))
                else:
                    log.warning("%s failed: %s", what,
                                getattr(result, "payload", "error"))
                return False
            return True
        except Exception as exc:
            log.warning("%s raised: %s", what, exc)
            return False

    # ------------------------------------------------------------------ TX

    @staticmethod
    def build_send_channel_frame(slot: int, data_type: int,
                                 body: bytes) -> bytes:
        """The exact CMD_SEND_CHANNEL_DATA (62) frame the reference frame
        server parses (openhop_core frame_server._cmd_send_channel_data,
        pinned by its test_cmd_send_channel_data_valid_direct_path):

            [62, channel_idx, path_len] + data_type(2 LE) + body

        One-byte command code; NO payload length byte (the frame itself
        carries the length); path_len 0xFF = flood-routed.

        `body` is the scope payload WITHOUT the 3-byte data_type/len
        prefix: the radio adds its own envelope when building the
        GRP_DATA plaintext (openhop_core companion_base.send_channel_data:
        plaintext = pack('<HB', data_type, len(payload)) + payload).
        Passing the pre-wrapped plaintext double-wraps the packet on air
        (2026-09-18 bug: every client field read 3 bytes shifted).
        """
        if not 0 < data_type <= 0xFFFF:
            raise ValueError(f"data_type {data_type} outside 1..0xFFFF")
        if len(body) > MAX_CHANNEL_DATA_LENGTH:
            raise ValueError(f"body {len(body)} B > "
                             f"MAX_CHANNEL_DATA_LENGTH {MAX_CHANNEL_DATA_LENGTH}")
        if not 0 <= slot < MAX_CHANNEL_SLOTS:
            raise ValueError(f"slot {slot} outside 0..{MAX_CHANNEL_SLOTS - 1}")
        return bytes([62, slot, 0xFF]) \
            + data_type.to_bytes(2, "little") + body

    async def send_channel_data(self, data_type: int, payload: bytes) -> bool:
        """Send one GRP_DATA packet via CMD_SEND_CHANNEL_DATA (62).

        `payload` is the full scope plaintext (type(2)+len(1)+body, what
        codec.encode_* produce). The 3-byte framing is stripped here at
        the radio boundary: the reference radio code adds its OWN
        type/len envelope to the command's payload when building the
        GRP_DATA plaintext (openhop_core companion_base.send_channel_data
        line: plaintext = pack('<HB', data_type, len(payload)) + payload.
        Passing the pre-wrapped plaintext double-wraps the packet on
        air - the 2026-09-18 bug that shifted every app-side field by
        3 bytes (the 12801-nodes / 560-s numbers) and mangled the app's
        REFRESH_REQ uplinks so hilltop's decoder dropped them silently.

        Acks honestly, in two tiers (the answerbot's proven pattern):
        an explicit ERROR frame is a real rejection -> False; a plain
        no-reply within the wait window is treated as delivered at
        DEBUG (the reference bridge transmits fire-and-forget and only
        writes OK after the radio send completes, which can exceed any
        short wait). Returns False when disconnected, channel missing,
        or the companion sent an error - never fake-success.
        """
        if not self.is_connected or self.mc is None or self._slot is None:
            log.debug("TX dropped: not connected / no slot (type %04x)",
                      data_type)
            return False
        EventType, _ = _meshcore()
        try:
            body = scope_wire_body(data_type, payload)
        except ValueError as exc:
            log.warning("TX dropped (type %04x): %s", data_type, exc)
            return False
        data = self.build_send_channel_frame(self._slot, data_type, body)
        lock, default = self._send_ctx()
        async with lock:
            cmds = self.mc.commands
            if default is not None:
                cmds.default_timeout = SEND_ACK_TIMEOUT
            try:
                result = await cmds.send(data, [EventType.OK, EventType.ERROR])
                if getattr(result, "type", None) == EventType.ERROR:
                    payload = getattr(result, "payload", None)
                    reason = str(payload.get("reason", "")) \
                        if isinstance(payload, dict) else ""
                    if reason in _NO_ACK_REASONS:
                        # No reply inside the window: the reference bridge
                        # sends fire-and-forget and writes OK only after the
                        # radio completes (LBT + airtime can exceed any short
                        # wait). Treat as delivered, honestly labelled.
                        log.info("GRP_DATA TX (type %04x): no companion ack "
                                 "within %.1fs - treated as delivered",
                                 data_type, SEND_ACK_TIMEOUT)
                        return True
                    log.warning("GRP_DATA TX (type %04x) rejected: %s",
                                data_type, payload or "error")
                    return False
                return True
            except Exception as exc:
                log.warning("GRP_DATA TX (type %04x) raised: %s",
                            data_type, exc)
                return False
            finally:
                if default is not None:
                    cmds.default_timeout = default

    # ------------------------------------------------------------------ RX

    def _subscribe(self) -> None:
        EventType, _ = _meshcore()
        mc = self.mc
        self._subs = []
        for event_type in (EventType.CHANNEL_DATA_RECV,):
            try:
                self._subs.append(mc.subscribe(event_type, self._on_channel_data))
            except Exception as exc:
                log.warning("Could not subscribe to %s: %s", event_type, exc)
        # Demo/older SDKs may lack CHANNEL_DATA_RECV - log the gap honestly.
        if not self._subs:
            log.warning("meshcore library exposes no CHANNEL_DATA_RECV - "
                        "client refresh requests will not be received.")

    async def _on_channel_data(self, event) -> None:
        payload = getattr(event, "payload", None)
        if not isinstance(payload, dict):
            return
        if payload.get("channel_idx") != self._slot:
            return  # not the scope channel
        raw = payload.get("payload")
        if isinstance(raw, str):
            try:
                raw = bytes.fromhex(raw)
            except ValueError:
                return
        if not isinstance(raw, (bytes, bytearray)):
            return
        # The companion strips its own 3-byte type/len envelope before
        # queueing (meshcore reader.py: payload = data_len bytes). The
        # event also names the data_type explicitly - decode the BODY
        # from that, tolerating either shape (body-only or full
        # plaintext) so an SDK change cannot silently un-decode packets.
        event_type = payload.get("data_type")
        if not isinstance(event_type, int):
            return
        if not 0x5300 <= event_type <= 0x53FF:
            return  # some other app's channel data
        raw = bytes(raw)
        try:
            if len(raw) >= 3 and codec.peek_data_type(raw) == event_type \
                    and raw[2] == len(raw) - 3:
                # Full plaintext shape - strip the framing.
                obj = codec.decode_any(raw)
            else:
                obj = codec.decode_body(event_type, raw)
        except codec.CodecError as exc:
            log.debug("Dropping undecodable GRP_DATA: %s", exc)
            return
        sender_prefix = str(payload.get("pubkey_prefix", "") or "").lower()
        if not sender_prefix:
            # Scope uplinks are anonymous by protocol; without a key
            # prefix the rate limiter treats it as 'unknown' (limited).
            sender_prefix = "unknown"
        if self._on_packet is not None:
            try:
                await self._on_packet(obj, sender_prefix)
            except Exception as exc:
                log.exception("Scope packet handler error: %s", exc)

    @property
    def own_prefix(self) -> str:
        return self._own_prefix

    @property
    def has_slot(self) -> bool:
        """True once the scope channel slot is known/set."""
        return self._slot is not None

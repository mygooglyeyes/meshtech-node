"""Adapter A - RawPacketSource: heard radio frames -> Observations.

SEED-MAP.md NEW module. Replaces the plugin's RepeaterApiSource: the
node IS the listener, so observations come from cleanmodem's observer
feed (raw bytes + signal) instead of HTTP polling.

Two consumption contracts:
- `run(queue, stop)`: the brain's verified ingest seam. Every item is
  a real Observation (the brain's dataclass stays the single source of
  truth; dicts were tried and cut - _ingest_advert_rows reads
  attributes, dicts would have silently fed it nothing).
- `on_scope(obj, sender_prefix)`: the RX shim callback (rxshim.py).
  Scope packets decoded from the SAME heard frames are handed straight
  to the brain's on_packet - peer layouts, refresh answers-in, and
  client REFRESH_REQs ride the listener, no companion link.

Honesty rules:
- rssi/snr pass through as-is; None stays None (no sentinel guessing).
- every packet lands in exactly one counter: decoded / duplicate /
  malformed / undecodable / ignored_type. Nothing vanishes.
- group packets get prefix=0 (no sender identity on the wire - see
  packets.py); the scope brain owns body-level origin attribution.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable, List, Optional, Tuple

from . import packets
from .observations import Observation
from .packets import (ChannelKeys, FloodDedupe, FrameParts,
                      PAYLOAD_TYPE_ADVERT, PAYLOAD_TYPE_GRP_DATA,
                      PAYLOAD_TYPE_GRP_TXT, parse_advert, split_frame)

log = logging.getLogger("meshtech-node.rawsource")

ScopeCallback = Callable[[object, str], "asyncio.Future[None] | None"]


@dataclass
class RxPacket:
    """One heard frame + signal, exactly as the radio saw it."""
    data: bytes
    rssi: Optional[int] = None
    snr: Optional[float] = None
    signal_rssi: Optional[int] = None
    recv_ts: Optional[float] = None      # None -> time.time() at arrival


@dataclass
class SourceStats:
    received: int = 0
    decoded: int = 0
    duplicates: int = 0
    malformed: int = 0
    undecodable: int = 0                 # group packets with no valid channel
    ignored_type: int = 0
    non_scope: int = 0                   # valid #scope HMAC, other app's data


class ReplayTransport:
    """Bench transport: replays RxPackets through the real pipeline."""

    def __init__(self, items: List[RxPacket], *, interval_s: float = 0.0):
        self._items = list(items)
        self._interval = interval_s

    def __aiter__(self):
        return self._gen()

    async def _gen(self) -> AsyncIterator[RxPacket]:
        for item in self._items:
            if self._interval:
                await asyncio.sleep(self._interval)
            yield item


@dataclass
class RawPacketSource:
    """Adapter A. `channels` = the joined channels (e.g. #scope) whose
    group traffic the node consumes; everything else is counted, not
    decoded. `on_scope` (optional) receives decoded scope packets as
    (codec_object, sender_prefix) - the rxshim wires it to
    service.on_packet."""
    transport: object
    channels: List[ChannelKeys] = field(default_factory=list)
    dedupe: Optional[FloodDedupe] = None
    on_scope: Optional[ScopeCallback] = None
    stats: SourceStats = field(default_factory=SourceStats)
    # FULL header of the last scope packet heard - the bench proof that
    # nothing was stripped (BENCH-CHECKLIST full-header check).
    last_scope_frame: Optional[FrameParts] = None

    def __post_init__(self) -> None:
        if self.dedupe is None:
            self.dedupe = FloodDedupe()

    async def run(self, out_queue: asyncio.Queue, stop: asyncio.Event) -> None:
        """The brain's verified seam: source.run(queue, stop). Items
        are Observations."""
        log.info("RawPacketSource running: %d channel(s) configured, "
                 "crypto_ok=%s", len(self.channels), packets.crypto_ok())
        try:
            if self.transport is None:
                # BENCH/no-radio wiring: no transport exists. Idle until
                # stopped - honestly silent, never a crash-loop.
                log.info("No transport wired - source idles "
                         "(bench/no-radio mode)")
                await stop.wait()
                return
            async for rx in self.transport:
                if stop.is_set():
                    break
                obs = self.handle_packet(rx)
                if obs is not None:
                    await out_queue.put(obs)
        except asyncio.CancelledError:
            raise
        except Exception:
            # a dead transport must never die silently (answerbot rule)
            log.exception("RawPacketSource transport FAILED - source stops")
            raise
        finally:
            log.info("RawPacketSource stopped: %s", self.stats_line())

    def handle_packet(self, rx: RxPacket) -> Optional[Observation]:
        """Parse+decode one heard packet; returns the Observation for
        the ingest queue, or None (counted honestly). Scope packets are
        ALSO offered to the on_scope callback (fire-and-forget; a
        failing callback is logged, never fatal)."""
        self.stats.received += 1
        frame = split_frame(rx.data)
        if frame is None:
            self.stats.malformed += 1
            log.debug("Malformed frame (%dB) skipped", len(rx.data))
            return None

        if frame.payload_type == PAYLOAD_TYPE_ADVERT:
            return self._from_advert(rx, frame)

        if frame.payload_type in (PAYLOAD_TYPE_GRP_TXT, PAYLOAD_TYPE_GRP_DATA):
            if self.dedupe.is_duplicate(frame.payload_type, frame.payload):
                self.stats.duplicates += 1
                return None
            return self._from_group(rx, frame)

        # DMs, room-server traffic etc: counted, not consumed. The node
        # is a scope feed, not a chat bot - the answerbot stays separate.
        self.stats.ignored_type += 1
        return None

    def _emit_scope(self, decoded: Optional[Tuple[ChannelKeys, bytes, float]],
                    frame: FrameParts, rx: RxPacket) -> None:
        """Offer one decrypted #scope plaintext to the RX shim.

        Guard (mirrors the plugin client's event guard, client.py:362):
        only 0x5300-0x53FF data types are scope traffic (codec.py's 0x53
        magic). A valid-HMAC plaintext from another app is counted and
        dropped - never handed to the brain."""
        if decoded is None or self.on_scope is None:
            return
        _channel, plaintext, _origin_ts = decoded
        from . import codec
        try:
            data_type = codec.peek_data_type(plaintext)
        except Exception:
            return                        # too short to carry a type
        if not 0x5300 <= data_type <= 0x53FF:
            self.stats.non_scope += 1
            log.debug("#scope plaintext type %04x is not scope traffic - "
                      "dropped", data_type)
            return
        self.last_scope_frame = frame
        try:
            obj = codec.decode_any(plaintext)
        except Exception as exc:
            log.debug("Scope plaintext undecodable: %s", exc)
            return
        try:
            result = self.on_scope(obj, "unknown")
            if asyncio.iscoroutine(result):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # sync bench path: no loop to schedule on. Closing the
                    # coroutine avoids the never-awaited leak; the drop is
                    # logged loudly, never silent.
                    result.close()
                    log.error("scope packet arrived with NO RUNNING LOOP - "
                              "delivery dropped (handle_packet called "
                              "synchronously). The gap is honest.")
                    return
                task = loop.create_task(result)
                task.add_done_callback(_log_task_death)
        except Exception:
            log.exception("on_scope callback raised - listener continues")

    def _from_advert(self, rx: RxPacket, frame: FrameParts) -> Optional[Observation]:
        info = parse_advert(frame.payload)
        if info is None:
            self.stats.malformed += 1
            return None
        if self.dedupe.is_duplicate(frame.payload_type, frame.payload):
            self.stats.duplicates += 1
            return None
        self.stats.decoded += 1
        return Observation(
            recv_ts=rx.recv_ts or time.time(),
            origin_ts=info.origin_ts,
            prefix=info.prefix,
            lat=info.lat,
            lon=info.lon,
            path_prefixes=self._path_prefixes(frame),
            channel_name=None,
            node_class=info.node_class,
            node_name=info.name,
        )

    def _from_group(self, rx: RxPacket, frame: FrameParts) -> Optional[Observation]:
        decoded = packets.decode_group_payload(frame.payload, self.channels)
        if decoded is None:
            # foreign channel, bad MAC, or crypto unavailable - honest
            # either way; never a fabricated plaintext
            self.stats.undecodable += 1
            return None
        channel, _plaintext, origin_ts = decoded
        self.stats.decoded += 1
        if frame.payload_type == PAYLOAD_TYPE_GRP_DATA:
            self._emit_scope(decoded, frame, rx)
        return Observation(
            recv_ts=rx.recv_ts or time.time(),
            origin_ts=origin_ts if origin_ts > 0 else None,
            prefix=0,             # no sender identity on group wire
            lat=None,
            lon=None,
            path_prefixes=self._path_prefixes(frame),
            channel_name=channel.name,
            node_class=0,
            node_name=None,
        )

    @staticmethod
    def _path_prefixes(frame: FrameParts) -> list:
        """Path hashes as ints for the Observation's path_prefixes.
        Full path bytes stay in the FrameParts (last_scope_frame keeps
        the whole header); this is the per-hop summary the store
        consumes."""
        if frame.hops == 0 or frame.hash_size == 0:
            return []
        return [int.from_bytes(frame.path[i:i + frame.hash_size], "big")
                for i in range(0, len(frame.path), frame.hash_size)]

    def stats_line(self) -> str:
        s = self.stats
        return (f"heard {s.received}: decoded {s.decoded}, dup {s.duplicates}, "
                f"malformed {s.malformed}, undecodable {s.undecodable}, "
                f"ignored {s.ignored_type}, non-scope {s.non_scope}")


def _log_task_death(task: "asyncio.Task") -> None:
    """A dead background task is NEVER silent (answerbot rule)."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("on_scope task died: %s: %s", type(exc).__name__, exc)

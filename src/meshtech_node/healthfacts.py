"""MESH HEALTH FACTS (HEALTH-DEFINITIONS.md, Brett 2026-09-29).

The mesh-health numbers the clinic wire carries, minted ONLY from
what the box can honestly count:

  - duplicate ratio (extra flood copies / packets, mesh + per sender)
  - channel occupancy (heard airtime share of the window)
  - duty-cycle headroom (our TX allowance minus what we sent)
  - per-sender loss / reordering (clinic sequence gaps) and flaps
  - hash collisions (one short tag carrying two public keys)
  - ask -> answer success (exchanges overheard on the air)

Honesty rules baked in:
  - every window reports its REAL length in minutes (window_min on
    the wire) - a partial window never claims a full span,
  - counters are SESSION-scoped: a restart starts fresh and never
    back-fills from guesses,
  - a count that cannot be minted is left at its unknown sentinel,
  - sequence jumps larger than REORDER_JUMP mean a rebooted sender -
    counted as nothing, never as loss.
"""

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

FLAP_SILENCE_S = 30 * 60        # a flap: silent this long, then heard again
EXCHANGE_WINDOW_S = 10 * 60     # an ask is answered within this window
REORDER_JUMP = 32               # seq jumps beyond this = a reboot, no count
MESH_WINDOW_S = 3600            # the airtime/dup window (an hour)
SENDER_WINDOW_S = 24 * 3600     # the per-sender window (a day)
COLLISIONS_MAX = 8              # the collision list keeps at most this many


class _Window:
    """One honest counting window with a CLOSED slot.

    Counts collect in the current window; when its span closes the
    window freezes into [closed] and a fresh one starts. read()
    reports the CLOSED window when one exists (the last COMPLETE
    window), else the current one with its true partial minutes.
    """

    def __init__(self, span_s: float) -> None:
        self.span_s = float(span_s)
        self.start: Optional[float] = None
        self.counts: Dict[str, float] = {}
        self.closed: Optional[Tuple[float, Dict[str, float]]] = None

    def add(self, key: str, value: float, now: float) -> None:
        self._roll(now)
        self.counts[key] = self.counts.get(key, 0.0) + value

    def read(self, now: float) -> Tuple[int, Dict[str, float]]:
        """(window minutes, counts) for the best honest window."""
        self._roll(now)
        if self.closed is not None:
            start, counts = self.closed
            minutes = int(max(0.0, now - start) // 60)
            return min(minutes, int(self.span_s // 60)), dict(counts)
        if self.start is None:
            return 0, {}
        minutes = int(max(0.0, now - self.start) // 60)
        return minutes, dict(self.counts)

    def _roll(self, now: float) -> None:
        if self.start is None:
            self.start = now
            return
        if now - self.start >= self.span_s:
            self.closed = (self.start, self.counts)
            self.start = now
            self.counts = {}


@dataclass
class _SeqState:
    last: int
    seen: float


class HealthTracker:
    """The one book behind the health records (clinic kinds 5-8).

    Fed by the radio pipeline (frames, duplicates, scope headers, tag
    collisions) and the brain (overheard asks/answers). records()
    mints ONLY what has evidence; everything else stays uncounted.
    """

    def __init__(self, origin: int, *,
                 airtime_ms_fn: Optional[Callable[[int], float]] = None,
                 duty_fn: Optional[Callable[[float], Tuple[int, int]]] = None,
                 ) -> None:
        self.origin = int(origin) & 0xFFFF
        # airtime_ms_fn(frame bytes) -> estimated ms in the air
        self._airtime_ms_fn = airtime_ms_fn
        # duty_fn(now) -> (used seconds, allowance seconds) for OUR TX
        self._duty_fn = duty_fn
        self._mesh = _Window(MESH_WINDOW_S)
        self._senders: Dict[int, _Window] = {}
        self._seq: Dict[int, _SeqState] = {}
        self._last_heard: Dict[int, float] = {}
        # hash -> (sender origin or None, when) for dup attribution
        self._payload_origins: Dict[bytes, Tuple[Optional[int], float]] = {}
        self._asks: List[Tuple[int, int, float]] = []
        self._exchange = _Window(SENDER_WINDOW_S)
        self._answer_delays: List[int] = []
        self._collisions: Dict[Tuple[bytes, bytes, bytes], float] = {}

    # ------------------------------------------------------- radio feed

    def note_frame(self, frame_bytes: int, *, now: Optional[float] = None,
                   payload: Optional[bytes] = None) -> None:
        """One frame heard (every frame): occupancy airtime + the
        dup-lookup seed. [payload] is the frame's payload bytes when
        a duplicate verdict needs per-sender attribution."""
        now = time.time() if now is None else now
        air_ms = 0.0
        if self._airtime_ms_fn is not None:
            try:
                air_ms = float(self._airtime_ms_fn(int(frame_bytes)))
            except Exception:
                log.exception("airtime estimate raised - counted as 0")
        self._mesh.add("air_ms", max(0.0, air_ms), now)
        self._mesh.add("frames", 1, now)
        self._prune_maps(now)

    def note_dup(self, now: Optional[float] = None,
                 payload: Optional[bytes] = None) -> None:
        """One extra copy of an already-heard flood packet."""
        now = time.time() if now is None else now
        self._mesh.add("dup", 1, now)
        sender = self._sender_of(payload, now)
        if sender is not None:
            self._sender_window(sender).add("dup", 1, now)

    def note_scope(self, sender: int, seq: int, *,
                   now: Optional[float] = None,
                   payload: Optional[bytes] = None) -> None:
        """One IDENTIFIED scope packet (a self-identifying body): the
        sender's sequence number feeds loss/reordering, the arrival
        feeds flaps, and the payload hash feeds later dup lookups."""
        now = time.time() if now is None else now
        sender = int(sender) & 0xFFFF
        seq = int(seq) & 0xFFFF
        self._track_seq(sender, seq, now)
        self._track_flap(sender, now)
        if payload:
            self._payload_origins[hashlib.sha256(payload).digest()[:8]] = \
                (sender, now)

    def note_collision(self, tag: bytes, key_a: bytes, key_b: bytes, *,
                       now: Optional[float] = None) -> None:
        """One proven tag collision: [tag] carried BOTH keys on air."""
        now = time.time() if now is None else now
        if not tag or key_a == key_b:
            return
        a, b = sorted((bytes(key_a), bytes(key_b)))
        key = (bytes(tag), a, b)
        if key not in self._collisions \
                and len(self._collisions) >= COLLISIONS_MAX:
            log.warning("collision list full (%d) - further pairs "
                        "counted out loud and dropped", COLLISIONS_MAX)
            return
        self._collisions[key] = now

    # ------------------------------------------------------ brain feed

    def observe_packet(self, obj: object, *, now: Optional[float] = None) \
            -> None:
        """One scope packet the brain handled ON THE AIR (the door
        never counts - 'TCP is not the mesh'). Asks are matched to
        later answers of the same target."""
        now = time.time() if now is None else now
        self._prune_asks(now)
        cls = type(obj).__name__
        if cls == "RefreshReq":
            self._asks.append((int(obj.kind), int(obj.target), now))
            self._exchange.add("asked", 1, now)
        elif cls in ("SectSum", "Route"):
            target_kind = 1 if cls == "SectSum" else 2
            target = int(obj.section_id if cls == "SectSum"
                         else obj.route_id)
            self._match_answer(target_kind, target, now)

    def note_own_answer(self, kind: int, target: int, *,
                        now: Optional[float] = None) -> None:
        """An ask answered BY THIS BOX. Our own TX never echoes back
        to our own listener (the flood dedupe suppresses it), so the
        answer is credited at send time instead of on the air."""
        now = time.time() if now is None else now
        self._prune_asks(now)
        self._match_answer(int(kind), int(target), now)

    def _match_answer(self, kind: int, target: int, now: float) -> None:
        for i, (ask_kind, ask_target, ts) in enumerate(self._asks):
            if ask_kind == kind and ask_target == target:
                del self._asks[i]
                self._exchange.add("answered", 1, now)
                self._answer_delays.append(int(now - ts))
                if len(self._answer_delays) > 256:
                    self._answer_delays = self._answer_delays[-256:]
                return

    # ---------------------------------------------------------- minting

    def records(self, now: Optional[float] = None) -> List[object]:
        """Every health record with honest evidence. No evidence, no
        record - never a pinned zero standing in for 'unknown'."""
        from . import codec
        now = time.time() if now is None else now
        out: List[object] = []
        self._prune_maps(now)
        self._prune_asks(now)

        # Kind 5 - the mesh-wide airtime fact (its own window).
        minutes, mesh = self._mesh.read(now)
        if minutes >= 1 or mesh:
            frames = mesh.get("frames", 0.0)
            dup = mesh.get("dup", 0.0)
            total = frames + dup
            dup_pm = _per_mille(dup, total) if total else codec.NUM_UNKNOWN
            air_ms = mesh.get("air_ms", 0.0)
            span_ms = minutes * 60 * 1000
            occ_pm = _per_mille(air_ms, span_ms) if span_ms else \
                codec.NUM_UNKNOWN
            used_s, allowance_s = codec.NUM_UNKNOWN, codec.NUM_UNKNOWN
            if self._duty_fn is not None:
                try:
                    used_s, allowance_s = self._duty_fn(now)
                except Exception:
                    log.exception("duty read raised - stays unknown")
            headroom = codec.NUM_UNKNOWN
            if used_s != codec.NUM_UNKNOWN and allowance_s != codec.NUM_UNKNOWN:
                headroom = max(0, int(allowance_s) - int(used_s))
            out.append(codec.ClinicAirtimeFact(
                source=self.origin, window_min=minutes,
                dup_per_mille=dup_pm, occupancy_per_mille=occ_pm,
                duty_headroom_s=_sat(headroom), tx_used_s=_sat(used_s)))

        # Kind 6 - one per sender with any honest count.
        for sender in sorted(self._senders):
            window = self._senders[sender]
            minutes, counts = window.read(now)
            if not counts:
                continue
            total = counts.get("heard", 0.0) + counts.get("dup", 0.0)
            dup_pm = _per_mille(counts.get("dup", 0.0), total) \
                if total else codec.NUM_UNKNOWN
            out.append(codec.ClinicSenderFact(
                source=self.origin, sender=sender, window_min=minutes,
                dup_per_mille=dup_pm,
                lost=_sat(int(counts.get("lost", 0))),
                reordered=_sat(int(counts.get("reordered", 0))),
                flaps=_sat(int(counts.get("flaps", 0)))))

        # Kind 7 - the overheard exchange score (its own window).
        minutes, counts = self._exchange.read(now)
        if counts:
            delays = sorted(self._answer_delays)
            med = delays[len(delays) // 2] if delays else 0
            out.append(codec.ClinicExchangeFact(
                source=self.origin, window_min=minutes,
                asked=_sat(int(counts.get("asked", 0))),
                answered=_sat(int(counts.get("answered", 0))),
                median_answer_s=_sat(int(med))))

        # Kind 8 - one per proven collision pair.
        for (tag, a, b) in sorted(self._collisions):
            seen = self._collisions[(tag, a, b)]
            out.append(codec.ClinicCollisionFact(
                source=self.origin, tag=tuple(tag),
                key_a=a[:8], key_b=b[:8],
                last_age_min=_age_minutes(now - seen)))
        return out

    # -------------------------------------------------------- internals

    def _sender_window(self, sender: int) -> _Window:
        window = self._senders.get(sender)
        if window is None:
            window = _Window(SENDER_WINDOW_S)
            self._senders[sender] = window
        return window

    def _sender_of(self, payload: Optional[bytes], now: float) \
            -> Optional[int]:
        if not payload:
            return None
        hit = self._payload_origins.get(
            hashlib.sha256(payload).digest()[:8])
        if hit is None:
            return None
        sender, seen = hit
        return sender if now - seen <= 120.0 else None

    def _track_seq(self, sender: int, seq: int, now: float) -> None:
        window = self._sender_window(sender)
        window.add("heard", 1, now)
        state = self._seq.get(sender)
        if state is None:
            self._seq[sender] = _SeqState(last=seq, seen=now)
            return
        forward = (seq - state.last) & 0xFFFF
        if forward == 0:
            # the same number twice: a reboot marker or a replay -
            # counted as neither loss nor order.
            state.seen = now
            return
        if forward == 1:
            pass                      # in order - nothing to count
        elif forward <= REORDER_JUMP:
            window.add("lost", forward - 1, now)   # the gap we never heard
        elif 0 < (state.last - seq) & 0xFFFF <= REORDER_JUMP:
            window.add("reordered", 1, now)        # a late arrival
        else:
            pass                      # a rebooted counter: count nothing
        state.last = seq
        state.seen = now

    def _track_flap(self, sender: int, now: float) -> None:
        last = self._last_heard.get(sender)
        if last is not None and now - last >= FLAP_SILENCE_S:
            self._sender_window(sender).add("flaps", 1, now)
        self._last_heard[sender] = now

    def _prune_asks(self, now: float) -> None:
        keep = []
        for kind, target, ts in self._asks:
            if now - ts <= EXCHANGE_WINDOW_S:
                keep.append((kind, target, ts))
        self._asks = keep

    def _prune_maps(self, now: float) -> None:
        stale = [h for h, (_, seen) in self._payload_origins.items()
                 if now - seen > 120.0]
        for h in stale:
            del self._payload_origins[h]
        if len(self._payload_origins) > 4096:
            # flood-proof the map: drop the oldest half honestly
            ordered = sorted(self._payload_origins.items(),
                             key=lambda kv: kv[1][1])
            for h, _ in ordered[:len(ordered) // 2]:
                del self._payload_origins[h]


def _per_mille(part: float, whole: float) -> int:
    if whole <= 0:
        return 0
    return _sat(int(round(part * 1000.0 / whole)))


def _sat(value: int) -> int:
    """Counts saturate at 0xFFFE - never wrap into the unknown
    sentinel (0xFFFF)."""
    return max(0, min(int(value), 0xFFFE))


def _age_minutes(seconds: float) -> int:
    minutes = int(seconds // 60)
    if minutes < 0:
        return 0
    return 0xFFFF if minutes > 0xFFFF else minutes

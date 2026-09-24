"""Airtime budget limiter + refresh dedupe/rate limiting.

The feed must never overload the mesh. Three independent guards:

1. BudgetLimiter - hard caps: max packets/hour and a duty-cycle cap
   (estimated TX seconds per hour as a percent of 3600), using the
   Semtech airtime estimate at the configured radio settings.
2. RefreshDedupe - drops repeat (kind, target, nonce) requests for
   10 minutes (clients retry; the mesh retransmits floods, so the host
   sees the same request several times).
3. RefreshRateLimiter - per-client cooldown + hourly cap, and an
   optional allow-list of client prefixes.

All three are pure state machines (no I/O) and unit-tested as such.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from .airtime import airtime_from_settings
from .config import RadioCfg


class BudgetLimiter:
    """Counts estimated TX seconds against hourly packet + duty caps."""

    def __init__(self, radio: RadioCfg, max_packets_per_hour: int,
                 max_duty_percent: float, *, now: float = 0.0):
        self._radio = radio
        self._max_packets = max(1, int(max_packets_per_hour))
        self._max_tx_seconds_per_hour = max_duty_percent / 100.0 * 3600.0
        self._window_start = now
        self._tx_ms: Deque[Tuple[float, float]] = deque()

    def _prune(self, now: float) -> None:
        while self._tx_ms and now - self._tx_ms[0][0] > 3600.0:
            self._tx_ms.popleft()

    def allow(self, payload_len: int, *, now: Optional[float] = None) -> bool:
        """Would sending `payload_len` bytes stay inside the budget?"""
        now = time.time() if now is None else now
        self._prune(now)
        if len(self._tx_ms) >= self._max_packets:
            return False
        tx_ms = airtime_from_settings(payload_len + 16, self._radio)
        # +16: GRP_DATA adds channel_hash(1) + MAC(2) and the LoRa layer
        # adds its own header/CRC; the estimate errs on the high side.
        spent = sum(ms for _, ms in self._tx_ms)
        return (spent + tx_ms) / 1000.0 <= self._max_tx_seconds_per_hour

    def record(self, payload_len: int, *, now: Optional[float] = None) -> float:
        """Record one send; returns the estimated TX milliseconds."""
        now = time.time() if now is None else now
        tx_ms = airtime_from_settings(payload_len + 16, self._radio)
        self._tx_ms.append((now, tx_ms))
        return tx_ms

    def tx_seconds_last_hour(self, *, now: Optional[float] = None) -> int:
        """Estimated feed TX seconds in the last hour (for PULSE)."""
        now = time.time() if now is None else now
        self._prune(now)
        return int(round(sum(ms for _, ms in self._tx_ms) / 1000.0))


class RefreshDedupe:
    """Drops repeated (kind, target, nonce) for `ttl_seconds`.

    Security review 2026-09-17: entries are airtime-bounded in theory,
    but a hostile client can still pump unique nonces; the table is
    therefore HARD-CAPPED and evicts oldest first (dicts keep
    insertion order), so memory stays flat no matter what arrives.
    """

    MAX_ENTRIES = 4096

    def __init__(self, ttl_seconds: float = 600.0):
        self._ttl = ttl_seconds
        self._seen: Dict[Tuple[int, int, int], float] = {}

    def seen_before(self, kind: int, target: int, nonce: int, *,
                    now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        key = (kind, target, nonce)
        stamp = self._seen.get(key)
        if stamp is not None and now - stamp <= self._ttl:
            return True
        self._seen[key] = now
        # opportunistic prune
        cutoff = now - self._ttl
        stale = [k for k, ts in self._seen.items() if ts < cutoff]
        for k in stale:
            del self._seen[k]
        # hard cap: evict oldest (insertion order) until inside the cap
        while len(self._seen) > self.MAX_ENTRIES:
            oldest = next(iter(self._seen))
            del self._seen[oldest]
        return False


class RefreshRateLimiter:
    """Per-client prefix cooldown + hourly cap + optional allow-list.

    Security review 2026-09-17: client prefixes are attacker-chosen
    (uplink identity is unauthenticated), so the per-client maps are
    HARD-CAPPED - a flood of random prefixes evicts the oldest client
    instead of growing memory forever.

    v1.3 (MAP-SIZE-DESIGN section 5, Brett 2026-09-23): the hourly cap
    is PER SIZE - 60 km asks 1/hour, 40 km 2/hour, 20 km 3/hour - so
    every level costs the mesh ~12-14 packets/hour (the packet math in
    the design doc). The ledger buckets by the SNAPPED size (the same
    snap the builder uses), so a 45 km ask draws from the 40 km pool.
    Legacy calls with no size keep the single configured cap.
    """

    MAX_CLIENTS = 512

    # Brett's load-balanced caps (km -> asks/hour); anything unmapped
    # falls back to the configured cap.
    SPAN_CAPS = {60.0: 1, 40.0: 2, 20.0: 3}

    def __init__(self, cooldown_seconds: float, hourly_cap: int,
                 allowed_prefixes: Optional[List[str]] = None):
        self._cooldown = cooldown_seconds
        self._cap = max(1, int(hourly_cap))
        self._allowed = [p.lower() for p in (allowed_prefixes or [])]
        self._last: Dict[str, float] = {}
        # hourly hits, keyed (prefix, span_bucket) - a 60 km ask never
        # consumes a 20 km slot and vice versa.
        self._hourly: Dict[Tuple[str, float], Deque[float]] = {}

    def span_cap(self, span_km: float) -> int:
        """The hourly cap for a window size (snapped; 0 = legacy cap)."""
        if not span_km or span_km <= 0:
            return self._cap
        snapped = min(self.SPAN_CAPS, key=lambda c: abs(c - span_km))
        return self.SPAN_CAPS.get(snapped, self._cap)

    @staticmethod
    def _snap_bucket(span_km: float) -> float:
        if not span_km or span_km <= 0:
            return 0.0
        return min(RefreshRateLimiter.SPAN_CAPS,
                   key=lambda c: abs(c - span_km))

    def _evict_if_needed(self) -> None:
        while len(self._last) > self.MAX_CLIENTS:
            oldest = min(self._last, key=self._last.get)
            del self._last[oldest]
            for key in [k for k in self._hourly if k[0] == oldest]:
                del self._hourly[key]

    def verdict(self, client_prefix: str, *, now: Optional[float] = None,
                span_km: float = 0.0) -> Tuple[bool, str, int]:
        """(allowed, reason, retry_after_s) - the HONEST refusal.

        The 2026-09-24 hunt (Brett's resize reports): a cooldown hit
        returned bare False and the wire handler sent NOTHING, so the
        app sat silent on a tap ("route tap does nothing"). Now every
        refusal names itself: "cooldown" (the 30 s per-client gap,
        retry in Ns) or "hourly_cap" (the size's pool, retry in Ns).
        allowed() stays as the boolean convenience wrapper."""
        now = time.time() if now is None else now
        prefix = client_prefix.lower()
        if self._allowed and not any(prefix.startswith(a) or a.startswith(prefix)
                                     for a in self._allowed):
            return False, "not_allowed", 0
        last = self._last.get(prefix)
        if last is not None and now - last < self._cooldown:
            return False, "cooldown", int(self._cooldown - (now - last)) + 1
        bucket = (prefix, self._snap_bucket(span_km))
        hits = self._hourly.setdefault(bucket, deque())
        while hits and now - hits[0] > 3600.0:
            hits.popleft()
        if len(hits) >= self.span_cap(span_km):
            wait = int(3600.0 - (now - hits[0])) + 1
            return False, "hourly_cap", wait
        return True, "", 0

    def allowed(self, client_prefix: str, *, now: Optional[float] = None,
                span_km: float = 0.0) -> bool:
        now = time.time() if now is None else now
        prefix = client_prefix.lower()
        if self._allowed and not any(prefix.startswith(a) or a.startswith(prefix)
                                     for a in self._allowed):
            return False
        last = self._last.get(prefix)
        if last is not None and now - last < self._cooldown:
            return False
        bucket = (prefix, self._snap_bucket(span_km))
        hits = self._hourly.setdefault(bucket, deque())
        while hits and now - hits[0] > 3600.0:
            hits.popleft()
        if len(hits) >= self.span_cap(span_km):
            return False
        return True

    def record(self, client_prefix: str, *, now: Optional[float] = None,
               span_km: float = 0.0) -> None:
        now = time.time() if now is None else now
        prefix = client_prefix.lower()
        self._last[prefix] = now
        bucket = (prefix, self._snap_bucket(span_km))
        self._hourly.setdefault(bucket, deque()).append(now)
        self._evict_if_needed()

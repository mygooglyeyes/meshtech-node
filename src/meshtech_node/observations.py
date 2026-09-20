"""Observation model + in-memory rolling observation store.

The packet source turns the repeater's raw view (REST API, or the demo
generator) into Observations; the rolling store keeps the last
`window_seconds` of them and aggregates what the feed builder needs:

- active nodes per section (and their prefixes)
- packets per section, per (section, path) route
- estimated propagation delay for packets that carry origin timestamps
- last-heard times

An observation is ONE received packet as the repeater saw it.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple


@dataclass
class BackboneNeighbor:
    """One direct RF neighbor of the host repeater, from the repeater's
    own neighbor_links table (live-probed 2026-09-18). The repeater
    measures these itself; the feed re-publishes, never re-derives."""

    prefix: int                     # neighbor's prefix byte
    sample_count: int = 0           # total link samples
    active: bool = False            # link currently alive (repeater's call)
    age_seconds: Optional[float] = None
    last_rssi: Optional[float] = None
    last_snr: Optional[float] = None
    ewma_rssi: Optional[float] = None
    ewma_snr: Optional[float] = None
    best_score: Optional[float] = None
    worst_score: Optional[float] = None


@dataclass
class Observation:
    recv_ts: float                 # host receipt time (time.time())
    origin_ts: Optional[float]     # sender's timestamp if the packet carried one
    prefix: int                    # first byte of the sender's pubkey
    lat: Optional[float]           # sender position if known (adverts)
    lon: Optional[float]
    path_prefixes: List[int]       # repeaters the packet traversed (if known)
    channel_name: Optional[str] = None
    node_class: int = 0            # INTRO flags bits 2-3 (0 = unknown)
    # Sender's name when the payload carries one (advert appdata); None
    # = honestly unknown, the store keeps whatever it already had.
    node_name: Optional[str] = None

    @property
    def delay_s(self) -> Optional[float]:
        """Estimated propagation delay (origin -> receipt), when honest."""
        if self.origin_ts is None or self.origin_ts <= 0:
            return None
        delay = self.recv_ts - self.origin_ts
        # Clock skew produces negative delays; clamp to None (unknown),
        # never publish a negative estimate. Ceiling 3600 s: an
        # advertising node that was powered off for days must not
        # reappear as a "6 hour delay".
        if delay < 0 or delay > 3600.0:
            return None
        return delay


class RollingStore:
    """The last `window_seconds` of observations, with aggregations."""

    def __init__(self, window_seconds: float = 3600.0):
        self.window_seconds = window_seconds
        self._obs: Deque[Observation] = deque()
        # node prefix -> (lat, lon, name, last_advert_ts)
        self._nodes: Dict[int, Dict[str, object]] = {}
        # prefix -> BackboneNeighbor (direct RF links of the host box)
        self._neighbors: Dict[int, BackboneNeighbor] = {}

    # ------------------------------------------------------------ input

    def add(self, obs: Observation, *, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._obs.append(obs)
        self._prune(now)

    def add_position(self, prefix: int, lat: float, lon: float,
                     name: Optional[str] = None, *,
                     now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        node = self._nodes.setdefault(prefix, {})
        node.update({"lat": lat, "lon": lon, "last_advert_ts": now})
        if name:
            node["name"] = name

    def add_node_class(self, prefix: int, node_class: int) -> None:
        """Record a node-class hint (INTRO flags bits 2-3; 0 = unknown).

        Unknown never overwrites a known class (the first real evidence
        sticks until newer evidence says otherwise).
        """
        node = self._nodes.setdefault(prefix, {})
        value = int(node_class) & 0x3
        if value != 0:
            node["node_class"] = value

    def add_name(self, prefix: int, name: str) -> None:
        self._nodes.setdefault(prefix, {})["name"] = name

    def add_backbone_neighbor(self, nb: BackboneNeighbor) -> None:
        """Record/refresh one direct RF neighbor (repeater-measured)."""
        self._neighbors[nb.prefix] = nb

    def backbone_neighbors(self) -> List[BackboneNeighbor]:
        """All known direct neighbors, strongest EWMA score first.

        Inactive links stay listed (aged out of the repeater's active
        set) - the wire carries their scores so the client can show
        them fading, never vanishing silently.
        """
        def sort_key(nb: BackboneNeighbor):
            return -(nb.ewma_rssi if nb.ewma_rssi is not None else -999.0)
        return sorted(self._neighbors.values(), key=sort_key)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._obs and self._obs[0].recv_ts < cutoff:
            self._obs.popleft()

    # ------------------------------------------------------------ reads

    def observations(self, *, now: Optional[float] = None) -> List[Observation]:
        now = time.time() if now is None else now
        self._prune(now)
        return list(self._obs)

    def node_info(self, prefix: int) -> Optional[Dict[str, object]]:
        return self._nodes.get(prefix)

    def known_nodes(self) -> List[int]:
        return sorted(self._nodes.keys())

    def rx_per_hour(self, *, now: Optional[float] = None) -> int:
        return len(self.observations(now=now))

    def active_prefixes(self, section_of, *, now: Optional[float] = None,
                        ) -> Dict[int, List[int]]:
        """prefixes of active nodes grouped by section id (-1 = unknown)."""
        groups: Dict[int, List[int]] = {}
        seen = set()
        for obs in self.observations(now=now):
            if obs.prefix in seen:
                continue
            seen.add(obs.prefix)
            groups.setdefault(section_of(obs), []).append(obs.prefix)
        return groups

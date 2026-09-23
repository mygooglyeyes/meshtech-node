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

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional, Tuple

log = logging.getLogger("meshtech-node.observations")


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
    """The last `window_seconds` of observations, with aggregations.

    DISK MEMORY (2026-09-21, Brett: the plugin's database adopted):
    `disk`, when set, is the NodeStore. Node-table facts (who exists,
    name, position, class) are written through the moment they are
    learned - no accumulate-and-flush buffer, so RAM cannot bloat -
    and at boot refill_nodes() restores what RAM forgot. The packet
    window itself is NEVER written to disk (scope rule: no raw packet
    storage anywhere).
    """

    def __init__(self, window_seconds: float = 3600.0):
        self.window_seconds = window_seconds
        self._obs: Deque[Observation] = deque()
        # node prefix -> (lat, lon, name, last_advert_ts)
        self._nodes: Dict[int, Dict[str, object]] = {}
        # prefix -> BackboneNeighbor (direct RF links of the host box)
        self._neighbors: Dict[int, BackboneNeighbor] = {}
        # Optional NodeStore (disk write-through + boot refill).
        self.disk = None

    # ------------------------------------------------------------ input

    def add(self, obs: Observation, *, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._obs.append(obs)
        self._prune(now)

    def _disk_node(self, prefix: int, node: Dict[str, object], *,
                   now: float) -> None:
        """Write one node's current facts through to disk (best-effort:
        a database hiccup never takes the RX path down; the RAM table
        stays the working truth, retried on the next fact)."""
        if self.disk is None:
            return
        try:
            lat = node.get("lat")
            lon = node.get("lon")
            self.disk.upsert_node(
                prefix,
                name=node.get("name"),
                node_class=node.get("node_class"),
                lat=float(lat) if lat is not None else None,
                lon=float(lon) if lon is not None else None,
                ts=now)
        except Exception:
            log.exception("node disk write-through failed (prefix %02x) "
                          "- RAM keeps the truth", prefix)

    # PLANET-RANGE GUARD (2026-09-21, found live by Brett's db peek):
    # a position outside these bounds is CORRUPTION (RF bit errors in a
    # garbled advert), not data. Hilltop heard prefix F1 claim
    # (-904.87, -1873.54) with a mojibake name in the same payload.
    # Storing it would put an off-planet dot on the map; the honest
    # answer is the same one we give 0.0/0.0: no position.
    LAT_MAX = 85.0
    LON_MAX = 180.0

    def add_position(self, prefix: int, lat: float, lon: float,
                     name: Optional[str] = None, *,
                     now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        node = self._nodes.setdefault(prefix, {})
        # HALF-FIX GUARD (2026-09-22, KHV Solar live): a torn advert
        # (RF bit errors) can corrupt ONE half of the fix. The evidence:
        # latitude stored as exactly 0.0 (the equator) while the
        # longitude decoded fine (-121.9 San Jose area) - a "position"
        # in the Gulf of Guinea. A REAL fix has both halves credible;
        # one exact 0.0 against a non-zero partner is corruption. Same
        # honest answer as the planet-range guard: no-position (the
        # name/hearing evidence stays real). 0.0/0.0 (null island) is
        # rejected here too - it used to be filtered one layer up; the
        # guard is the single choke point now, so no caller can forget.
        half_fix = lat == 0.0 or lon == 0.0
        if abs(lat) > self.LAT_MAX or abs(lon) > self.LON_MAX or half_fix:
            if half_fix:
                log.warning("position REJECTED as half-corrupt: prefix %02x "
                            "claimed (%.4f, %.4f) - one half is the torn-"
                            "advert zero; stored as no-position", prefix,
                            lat, lon)
            else:
                log.warning("position REJECTED as corruption: prefix %02x "
                            "claimed (%.4f, %.4f) - outside the planet; "
                            "stored as no-position", prefix, lat, lon)
            # The corrupt position is dropped, but everything else in
            # the advert is real evidence (name, hearing) and the node
            # still counts as heard (C3).
            node["last_advert_ts"] = now
            node.pop("stale", None)
            if name:
                node["name"] = name
            self._disk_node(prefix, node, now=now)
            return
        node.update({"lat": lat, "lon": lon, "last_advert_ts": now})
        node.pop("stale", None)      # heard again: no longer stale (C3)
        if name:
            node["name"] = name
        self._disk_node(prefix, node, now=now)

    def add_node_class(self, prefix: int, node_class: int) -> None:
        """Record a node-class hint (INTRO flags bits 2-3; 0 = unknown).

        Unknown never overwrites a known class (the first real evidence
        sticks until newer evidence says otherwise).
        """
        node = self._nodes.setdefault(prefix, {})
        value = int(node_class) & 0x3
        if value != 0:
            node["node_class"] = value
            self._disk_node(prefix, node, now=time.time())

    def add_name(self, prefix: int, name: str, *,
                 now: Optional[float] = None) -> None:
        """Record a name. C3: name-only evidence also counts as a
        hearing - else a node heard only in group traffic would carry
        no last_advert_ts and dodge the lifecycle entirely."""
        now = time.time() if now is None else now
        node = self._nodes.setdefault(prefix, {})
        node["name"] = name
        node["last_advert_ts"] = max(float(node.get("last_advert_ts", 0)),
                                     now)
        node.pop("stale", None)
        self._disk_node(prefix, node, now=now)

    def refill_nodes(self, rows: List[dict]) -> int:
        """Boot refill: restore the disk node table into RAM.

        Disk rows become RAM node entries with their ORIGINAL
        first/last times (staleness math stays honest across a
        restart - a node silent for a week is still a week silent).
        RAM wins where both exist: an existing RAM entry is never
        touched (disk is the older copy). Returns rows restored.
        """
        restored = 0
        now = time.time()
        for row in rows:
            try:
                prefix = int(row.get("prefix"))
            except (TypeError, ValueError):
                continue          # malformed row: skip, never crash boot
            if prefix in self._nodes:
                continue          # RAM wins: disk is the older copy
            node: Dict[str, object] = {
                "last_advert_ts": float(row.get("last_seen") or now),
            }
            if row.get("name"):
                node["name"] = row["name"]
            if row.get("lat") is not None and row.get("lon") is not None:
                node["lat"] = row["lat"]
                node["lon"] = row["lon"]
            if row.get("node_class"):
                node["node_class"] = int(row["node_class"])
            # C3 honesty across restarts: disk last_seen older than the
            # stale line means the node comes back STALE (off maps) -
            # exactly as it would have been with no restart in between.
            if now - float(node["last_advert_ts"]) > self.STALE_AFTER_S:
                node["stale"] = True
            self._nodes[prefix] = node
            restored += 1
        if restored:
            log.info("node table refilled from disk: %d node(s) "
                     "restored (RAM table now %d)", restored,
                     len(self._nodes))
        return restored

    def add_backbone_neighbor(self, nb: BackboneNeighbor) -> None:
        """Record/refresh one direct RF neighbor (repeater-measured)."""
        self._neighbors[nb.prefix] = nb

    # ------------------------------------------------- node lifecycle (C3)

    STALE_AFTER_S = 14 * 86400.0     # no advert for 14 days -> not on maps
    FORGET_AFTER_S = 30 * 86400.0    # silent 30 days -> pruned entirely

    def prune_nodes(self, *, now: Optional[float] = None) -> dict:
        """C3 (2026-09-20 review, Brett's rule): the node table never
        grows forever. A node unheard for 14 days goes STALE (kept in
        the table but excluded from map/active aggregation), and one
        unheard for 30 days is FORGOTTEN (deleted). Returns the counts
        for honest logging."""
        now = time.time() if now is None else now
        stale = forgotten = 0
        for prefix, node in list(self._nodes.items()):
            last = node.get("last_advert_ts")
            if last is None:
                continue               # no evidence of age: leave it
            age = now - float(last)
            if age > self.FORGET_AFTER_S:
                del self._nodes[prefix]
                forgotten += 1
            elif age > self.STALE_AFTER_S:
                node["stale"] = True
                stale += 1
        # DISK MEMORY: mirror the forget rule so disk cannot outgrow
        # RAM's posture (stale-marking is derived, only deletion is
        # stored - the disk copy carries no stale flag).
        if self.disk is not None and forgotten:
            try:
                self.disk.forget_older_than(now - self.FORGET_AFTER_S)
            except Exception:
                log.exception("node disk prune failed - RAM stays the truth")
        return {"stale": stale, "forgotten": forgotten}

    def node_is_stale(self, prefix: int, *, now: Optional[float] = None,
                      ) -> bool:
        node = self._nodes.get(prefix)
        if not node:
            return False
        if node.get("stale"):
            return True
        last = node.get("last_advert_ts")
        if last is None:
            return False
        return (now if now is not None else time.time()) - \
            float(last) > self.STALE_AFTER_S

    def active_nodes_ever(self) -> int:
        """Total node-table size (the C3 growth metric for logs)."""
        return len(self._nodes)

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

    def map_nodes(self) -> List[Dict[str, object]]:
        """C3: the map's node table - stale (14d silent) nodes are
        EXCLUDED here, forgotten (30d) are already deleted."""
        return [{"prefix": p, **node} for p, node in
                self._nodes.items() if not node.get("stale")]

    def rx_per_hour(self, *, now: Optional[float] = None) -> int:
        return len(self.observations(now=now))

    def active_prefixes(self, section_of, *, now: Optional[float] = None,
                        ) -> Dict[int, List[int]]:
        """prefixes of active nodes grouped by section id (-1 = unknown).

        prefix=0 is SKIPPED: group traffic carries no sender identity
        (prefix=0 = honestly unknown), and with all-traffic recording
        (PROJECT.md rule 3) unknown-sender rows now form the majority.
        Counting them would fabricate a phantom "node 0" in the active
        totals. Node identity comes from adverts only."""
        groups: Dict[int, List[int]] = {}
        seen = set()
        for obs in self.observations(now=now):
            if obs.prefix in seen or obs.prefix == 0:
                continue
            seen.add(obs.prefix)
            groups.setdefault(section_of(obs), []).append(obs.prefix)
        return groups

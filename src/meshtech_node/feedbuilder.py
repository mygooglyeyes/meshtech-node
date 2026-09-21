"""Feed builder - turns observations into the wire packets of PROTOCOL.md.

Cadence (all configurable, all budget-checked by the caller):

- PULSE every pulse_interval_seconds
- LAYOUT every layout_interval_seconds
- one background SECT_SUM per pulse interval (round-robin) with its top
  route stubs
- INTRO batches rotate the known-node table (positioned nodes first)
- ROUTE details + remaining SECT_SUMs on client refresh request
- SNAP on demand (host start / layout change), split into parts

The builder never transmits; it returns packets with their payloads so
the service can budget-check, pace and send them.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from . import codec
from .budget import BudgetLimiter
from .config import Settings
from .grid import GridGeometry
from .observations import RollingStore

log = logging.getLogger("meshtech-scope.feed")

MAX_STUBS = 6
MAX_PREFIXES_PER_ROUTE = 8
MAX_INTRO_BYTES = 100  # body budget for INTRO entries (inside 120 target)
DELAY_UNKNOWN = 0


def route_id(path: Tuple[int, ...]) -> int:
    """Stable 16-bit route id from the path bytes.

    v1.1: id collisions across DIFFERENT paths in one section are
    possible with byte-aliasing, so ids are only offered to clients
    when unique within the section (build_sect_sum filters). Kept
    public because peers.py derives route ids the same way.
    """
    value = 0
    for hop in path:
        value = ((value << 8) | (hop & 0xFF)) & 0xFFFF
    return value ^ ((len(path) & 0xF) << 12)


@dataclass
class OutPacket:
    data_type: int
    payload: bytes        # full GRP_DATA plaintext: type(2)+len(1)+body
    reason: str           # "pulse" | "layout" | "background" | "refresh" | "snap"


def percentile(sorted_values: List[float], pct: float) -> int:
    """Median/p90 as integer seconds; 0 (unknown) when no honest data."""
    if not sorted_values:
        return DELAY_UNKNOWN
    idx = min(len(sorted_values) - 1, int(len(sorted_values) * pct / 100.0))
    return max(0, int(round(sorted_values[idx])))


class FeedBuilder:
    def __init__(self, settings: Settings, store: RollingStore,
                 geometry: GridGeometry, budget: BudgetLimiter,
                 *, origin: int = 0):
        self.settings = settings
        self.store = store
        self.geometry = geometry
        self.budget = budget
        self.origin = origin & 0xFFFF
        self.seq: int = 0
        self.snap_id: int = 0
        self._last_pulse = 0.0
        self._last_layout = 0.0
        self._last_beacon = 0.0
        self._background_section = 0
        self._intro_cursor = 0

    def _next_seq(self) -> int:
        self.seq = (self.seq + 1) & 0xFFFF
        return self.seq

    # ------------------------------------------------------------ aggregations

    def _section_of(self, obs) -> int:
        if obs.lat is None or obs.lon is None:
            # DRY-RUN FIX (2026-09-17): fall back to the node table.
            # Packets often carry no position of their own (non-advert
            # rows); their sender may still have a known position from
            # INTRO adverts or /nodes. Without this, path-bearing
            # packets landed nowhere (-1), routes never formed, and the
            # live feed would have published empty sections while
            # hearing traffic. Falls back to -1 ("unknown") when the
            # sender is genuinely unpositioned - never guessed.
            node = self.store.node_info(obs.prefix) or {}
            lat, lon = node.get("lat"), node.get("lon")
            if lat is None or lon is None:
                return -1
            return self.geometry.section_for(float(lat), float(lon))
        return self.geometry.section_for(obs.lat, obs.lon)

    def _section_stats(self, section_id: int, *,
                       now: float) -> Tuple[int, int, List[float]]:
        """(active_nodes, packet_count, sorted delays) for one section."""
        active: Dict[int, bool] = {}
        packets = 0
        delays: List[float] = []
        for obs in self.store.observations(now=now):
            if self._section_of(obs) != section_id:
                continue
            packets += 1
            active[obs.prefix] = True
            if obs.delay_s is not None:
                delays.append(obs.delay_s)
        delays.sort()
        return len(active), packets, delays

    def _routes_for_section(self, section_id: int, *,
                            now: float) -> List[Tuple[Tuple[int, ...], int, List[float], float]]:
        """Routes in one section: (path, count, delays, last_heard).

        A route is the tuple of repeater prefixes a packet traversed
        (its "ghost trail"). Delays only from honest origin stamps.
        """
        routes: Dict[Tuple[int, ...], Dict[str, object]] = {}
        for obs in self.store.observations(now=now):
            if self._section_of(obs) != section_id:
                continue
            path = tuple(obs.path_prefixes[:MAX_PREFIXES_PER_ROUTE])
            if not path:
                continue
            entry = routes.setdefault(path, {"count": 0, "delays": [],
                                             "last": 0.0})
            entry["count"] = int(entry["count"]) + 1
            if obs.delay_s is not None:
                entry["delays"].append(obs.delay_s)
            entry["last"] = max(float(entry["last"]), obs.recv_ts)
        out: List[Tuple[Tuple[int, ...], int, List[float], float]] = []
        for path, entry in routes.items():
            delays = sorted(float(d) for d in entry["delays"])
            out.append((path, int(entry["count"]), delays,
                        float(entry["last"])))
        out.sort(key=lambda item: -item[1])  # busiest first
        return out

    def section_of_observation(self, obs):
        return self.geometry.section_for(obs.lat, obs.lon) if (
            obs.lat is not None and obs.lon is not None) else -1

    # ------------------------------------------------------------ packets

    def build_pulse(self, *, now: Optional[float] = None) -> OutPacket:
        now = time.time() if now is None else now
        counts = []
        total_set = set()
        for obs in self.store.observations(now=now):
            total_set.add(obs.prefix)
        for sid in range(1, self.geometry.section_count + 1):
            active, _, _ = self._section_stats(sid, now=now)
            counts.append(min(active, 255))
        pulse = codec.Pulse(
            seq=self._next_seq(),
            uptime_min=int(now) % 65536,   # replaced by service with real uptime
            rx_per_hour=min(self.store.rx_per_hour(now=now), 0xFFFF),
            feed_airtime_s_per_h=min(self.budget.tx_seconds_last_hour(now=now),
                                     0xFFFF),
            active_total=min(len(total_set), 0xFFFF),
            origin=self.origin,
            section_counts=counts,
        )
        return OutPacket(codec.TYPE_PULSE, codec.encode_pulse(pulse), "pulse")

    def build_layout(self, *, now: Optional[float] = None) -> OutPacket:
        now = time.time() if now is None else now
        area = self.settings.area
        layout = codec.Layout(
            seq=self._next_seq(),
            grid=area.grid,
            center_lat=area.center_lat,
            center_lon=area.center_lon,
            span_m=int(area.span_km * 1000.0),
            origin=self.origin,
            name=area.name[:codec.MAX_NAME],
        )
        return OutPacket(codec.TYPE_LAYOUT, codec.encode_layout(layout),
                         "layout")

    def build_sect_sum(self, section_id: int, *, now: Optional[float] = None,
                       top_routes: Optional[List[Tuple[int, ...]]] = None,
                       ) -> OutPacket:
        now = time.time() if now is None else now
        active, packets, delays = self._section_stats(section_id, now=now)
        routes = self._routes_for_section(section_id, now=now)
        ids: Dict[int, int] = {}
        for path, count, _delays, _last in routes[:MAX_STUBS]:
            # Route ids are derived from the path bytes (stable, host-only)
            ids.setdefault(_route_id(path), _route_id(path))
        sect = codec.SectSum(
            seq=self._next_seq(),
            section_id=section_id,
            active_nodes=min(active, 255),
            packet_count=min(packets, 0xFFFF),
            delay_p50_s=percentile(delays, 50),
            delay_p90_s=percentile(delays, 90),
            origin=self.origin,
            route_stubs=list(ids.values())[:MAX_STUBS],
        )
        return OutPacket(codec.TYPE_SECT_SUM, codec.encode_sect_sum(sect),
                         "refresh" if top_routes is not None else "background")

    def build_route(self, section_id: int, path: Tuple[int, ...], *,
                    now: Optional[float] = None) -> Optional[OutPacket]:
        now = time.time() if now is None else now
        routes = self._routes_for_section(section_id, now=now)
        for rpath, count, delays, last in routes:
            if rpath == tuple(path):
                route = codec.Route(
                    seq=self._next_seq(),
                    section_id=section_id,
                    route_id=_route_id(rpath),
                    packet_count=min(count, 0xFFFF),
                    delay_med_s=percentile(delays, 50),
                    last_heard_min=max(0, min(0xFFFF,
                                              int((now - last) / 60.0))),
                    origin=self.origin,
                    prefixes=[p & 0xFF for p in rpath],
                )
                return OutPacket(codec.TYPE_ROUTE, codec.encode_route(route),
                                 "refresh")
        return None

    def build_intro_batch(self, *, now: Optional[float] = None) -> Optional[OutPacket]:
        """One INTRO batch of <= 100 body-bytes, positioned nodes first."""
        now = time.time() if now is None else now
        area = self.settings.area
        span_m = area.span_km * 1000.0
        intro = codec.Intro(seq=self._next_seq(), origin=self.origin,
                            center_lat=area.center_lat,
                            center_lon=area.center_lon, span_m=span_m)
        entries: List[codec.IntroEntry] = []
        body_len = 1
        known = sorted(self.store.known_nodes())
        # positioned nodes first, then the rest; rotate by cursor so every
        # node gets out over successive batches
        positioned, plain = [], []
        for prefix in known:
            info = self.store.node_info(prefix) or {}
            if info.get("lat") is not None and info.get("lon") is not None:
                positioned.append(prefix)
            else:
                plain.append(prefix)
        ordered = positioned[self._intro_cursor % max(1, len(positioned)):] + \
            positioned[:self._intro_cursor % max(1, len(positioned))] + \
            plain[self._intro_cursor % max(1, len(plain)):] + \
            plain[:self._intro_cursor % max(1, len(plain))]
        for prefix in ordered:
            info = self.store.node_info(prefix) or {}
            name = info.get("name")
            entry = codec.IntroEntry(
                prefix=prefix,
                name=str(name)[:codec.MAX_NAME] if name else None,
                lat=info.get("lat"),
                lon=info.get("lon"),
                node_class=int(info.get("node_class") or 0) & 0x3,
            )
            # size estimate: prefix(1)+flags(1)+name_len(1)+name+pos(4)
            size = 3 + len((entry.name or "").encode("utf-8")[:codec.MAX_NAME])
            if entry.lat is not None:
                size += 4
            if body_len + size > MAX_INTRO_BYTES:
                break
            entries.append(entry)
            body_len += size
        if not entries:
            self._intro_cursor = 0
            return None
        intro.entries = entries
        self._intro_cursor += len(entries)
        pkt = codec.encode_intro(intro)
        if len(pkt) > codec.TARGET_PAYLOAD:
            # v1.1 header growth: drop the tail until we fit, honestly
            while intro.entries and len(codec.encode_intro(intro)) \
                    > codec.TARGET_PAYLOAD:
                intro.entries.pop()
            if not intro.entries:
                return None
            pkt = codec.encode_intro(intro)
        return OutPacket(codec.TYPE_INTRO, pkt, "background")

    # ------------------------------------------------------------ refresh

    def build_refresh_response(self, kind: int, target: int, *,
                               now: Optional[float] = None) -> List[OutPacket]:
        """Packets answering one REFRESH_REQ (section or route).

        PROTOCOL v1.2: section targets are 1-based (1 = NW .. 9 = SE)
        and 0 = WHOLE-AREA refresh whatever the kind string: LAYOUT (grid
        geometry so the client can draw the map immediately) + one
        summary per section + node names. (History: target 0 once fell
        through the section range check as section 0, so "Refresh map"
        fetched only the NW square; 2026-09-18 made 0 mean whole-area;
        2026-09-20 made the whole numbering 1-based so the wire, the
        logs, and the screens all agree.)"""
        now = time.time() if now is None else now
        out: List[OutPacket] = []
        if kind == codec.REFRESH_KIND_SECTION:
            if target == codec.REFRESH_WHOLE_AREA:
                out.append(self.build_layout(now=now))
                for sid in range(1, self.geometry.section_count + 1):
                    out.append(self.build_sect_sum(sid, now=now,
                                                   top_routes=[]))
                pkt = self.build_intro_batch(now=now)
                if pkt:
                    out.append(pkt)
                return out
            if not 1 <= target <= self.geometry.section_count:
                return out
            out.append(self.build_sect_sum(target, now=now, top_routes=[]))
            routes = self._routes_for_section(target, now=now)
            for path, _count, _delays, _last in routes[:3]:
                pkt = self.build_route(target, path, now=now)
                if pkt:
                    out.append(pkt)
            pkt = self.build_intro_batch(now=now)
            if pkt:
                out.append(pkt)
        elif kind == codec.REFRESH_KIND_ROUTE:
            # target is a route_id; find its section+path
            for sid in range(1, self.geometry.section_count + 1):
                for path, _count, _delays, _last in \
                        self._routes_for_section(sid, now=now):
                    if _route_id(path) == target:
                        pkt = self.build_route(sid, path, now=now)
                        if pkt:
                            out.append(pkt)
                        break
                if out:
                    break
        return out

    # ------------------------------------------------------------ snapshot

    def build_snapshot(self, *, now: Optional[float] = None) -> List[OutPacket]:
        """Full snapshot split into SNAP parts (<= 120 B each)."""
        now = time.time() if now is None else now
        self.snap_id = (self.snap_id + 1) & 0xFF
        pieces: List[bytes] = []

        def add(pkt: bytes) -> None:
            pieces.append(pkt)

        # Bodies of LAYOUT / SECT_SUMs / top ROUTEs / INTRO batches,
        # minus their data_type framing (SNAP bodies concatenate raw).
        layout = self.build_layout(now=now)
        add(layout.payload[3:])
        for sid in range(1, self.geometry.section_count + 1):
            sect = self.build_sect_sum(sid, now=now)
            add(sect.payload[3:])
        intro = self.build_intro_batch(now=now)
        if intro:
            add(intro.payload[3:])

        parts: List[bytes] = []
        current = b""
        # SNAP wire cost: data_type(2)+len(1)+header(5)+snap fields(3),
        # so the concatenated body stays inside TARGET_PAYLOAD.
        snap_budget = codec.TARGET_PAYLOAD - 11
        for piece in pieces:
            if len(piece) + len(current) > snap_budget:
                parts.append(current)
                current = b""
            current += piece
        if current:
            parts.append(current)
        out: List[OutPacket] = []
        for i, body in enumerate(parts):
            snap = codec.Snap(seq=self._next_seq(), snap_id=self.snap_id,
                              part=i, parts=len(parts), origin=self.origin,
                              body=body)
            out.append(OutPacket(codec.TYPE_SNAP, codec.encode_snap(snap),
                                 "snap"))
        return out


def _route_id(path: Tuple[int, ...]) -> int:
    """Deprecated alias (kept for tests/imports); see route_id()."""
    return route_id(path)

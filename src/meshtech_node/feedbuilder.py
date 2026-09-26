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
        # v1.2 RENUMBERING (2026-09-21): squares are 1..9 and 0 is
        # RESERVED (whole-area) - the encoder refuses 0. Seed at 1 and
        # rotate 1..9 only. The 0 seed here was the silent feed-killer:
        # the first cadence pulse raised CodecError inside the loop.
        self._background_section = 1
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
                # RECIPIENT = THE TRAIL (Brett, 2026-09-25): when the
                # sender is not one of our mapped nodes, the trail's
                # FAR END - the last repeater we heard send it -
                # places the traffic. Beyond that: honestly -1
                # (held), never guessed.
                if obs.path_prefixes:
                    end = self.store.node_info(obs.path_prefixes[-1]) or {}
                    elat, elon = end.get("lat"), end.get("lon")
                    if elat is not None and elon is not None:
                        return self.geometry.section_for(float(elat),
                                                         float(elon))
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

        ROUTE MEMORY (2026-09-24, Brett: routes were never meant to be
        RAM-only): the route table is DURABLE now - fed at every packet
        hearing (direct hops included: "a route is any persistent path
        between two nodes"), refilled from disk at boot, so the 1-hour
        packet window no longer bounds a route's life.

        A route is the tuple of repeater prefixes a packet traversed;
        a packet heard direct is the one-hop route (sender,). Delays
        only from honest origin stamps.
        """
        routes: Dict[Tuple[int, ...], Dict[str, object]] = {}
        # The durable table first (restored routes survive the window).
        # Each route carries the section its traffic was HEARD in (its
        # own honest anchor; see add_route) - no re-derivation needed.
        for path, entry in self.store.routes_all():
            if int(entry.get("section") or -1) != section_id:
                continue
            routes[path] = {"count": int(entry["count"]),
                            "delays": list(entry["delays"]),
                            "last": float(entry["last"])}
        # The 1-hour window refines live counts/delays/last-heard.
        # Every window observation already landed in the durable table
        # (add() writes through at the hearing), so a window packet is
        # NEVER re-counted on top of its durable row - that would
        # double fresh routes. The window aggregates on its own first,
        # then fills ONLY the paths the durable table is missing (a
        # disk-less store, or a row already forgotten).
        window: Dict[Tuple[int, ...], Dict[str, object]] = {}
        for obs in self.store.observations(now=now):
            path = tuple(obs.path_prefixes[:MAX_PREFIXES_PER_ROUTE]) \
                if obs.path_prefixes else ((obs.prefix,) if obs.prefix else ())
            if not path or self._section_of(obs) != section_id:
                continue
            entry = window.setdefault(path, {"count": 0, "delays": [],
                                             "last": 0.0})
            entry["count"] = int(entry["count"]) + 1
            if obs.delay_s is not None:
                entry["delays"].append(obs.delay_s)
            entry["last"] = max(float(entry["last"]), obs.recv_ts)
        for path, entry in window.items():
            if path not in routes:
                routes[path] = entry
        out: List[Tuple[Tuple[int, ...], int, List[float], float]] = []
        for path, entry in routes.items():
            delays = sorted(float(d) for d in entry["delays"])
            out.append((path, int(entry["count"]), delays,
                        float(entry["last"])))
        out.sort(key=lambda item: -item[1])  # busiest first
        return out

    def _section_of_path(self, path: Tuple[int, ...]) -> int:
        """Which section a DURABLE route belongs to. The anchor is the
        route's stored sender (outgoing; honest: the node actually
        heard at the trail's start for direct routes), then the
        trail's FAR END (incoming - the last repeater we heard send
        it; Brett, 2026-09-25), then any POSITIONED node in the path;
        nobody positioned is honestly unknown (-1) - never guessed.
        The boot re-check calls this where a route predates its node
        facts."""
        entry = self.store.route_entry(path)
        sender = int(entry.get("sender") or 0) if entry else 0
        if sender:
            node = self.store.node_info(sender) or {}
            lat, lon = node.get("lat"), node.get("lon")
            if lat is not None and lon is not None:
                return self.geometry.section_for(float(lat), float(lon))
        # THE TRAIL'S FAR END (Brett, 2026-09-25: recipient = trail -
        # the last repeater we heard send it) decides before the
        # sender-side hop scan below.
        if path:
            node = self.store.node_info(path[-1]) or {}
            lat, lon = node.get("lat"), node.get("lon")
            if lat is not None and lon is not None:
                return self.geometry.section_for(float(lat), float(lon))
        for hop in path:
            node = self.store.node_info(hop) or {}
            lat, lon = node.get("lat"), node.get("lon")
            if lat is not None and lon is not None:
                return self.geometry.section_for(float(lat), float(lon))
        return -1

    def section_of_observation(self, obs):
        return self.geometry.section_for(obs.lat, obs.lon) if (
            obs.lat is not None and obs.lon is not None) else -1

    # ------------------------------------------------------------ packets

    def build_pulse(self, *, now: Optional[float] = None) -> OutPacket:
        now = time.time() if now is None else now
        counts = []
        total_set = set()
        for obs in self.store.observations(now=now):
            if obs.prefix != 0:
                # prefix=0 = sender honestly unknown (group wire carries
                # no identity) - never a "node 0" in the totals. Node
                # identity comes from adverts only.
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
            rows=area.rows,
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
        span_m = self._ruler_m()
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
        # ROTATE THE WHOLE LIST AS ONE (2026-09-21 roster fix): rotating
        # each group independently re-queued positioned nodes at the
        # front of EVERY batch, so name-only (plain) nodes beyond the
        # first ~9 entries never fit any batch - proven in the sandbox
        # (plain nodes invisible on air). One combined rotation: the
        # cursor walks the entire roster, positioned nodes still lead
        # the very first batch (cursor 0).
        ordered_all = positioned + plain
        c = self._intro_cursor % max(1, len(ordered_all))
        ordered = ordered_all[c:] + ordered_all[:c]
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

    # Allowed client window sizes (MAP-SIZE-DESIGN section 4/5). 0 =
    # host decides = the whole home box. Any other value snaps to the
    # nearest allowed size - the same snap rule config.py uses, so the
    # two sides agree by construction.
    SPAN_CHOICES = (20.0, 40.0, 60.0)

    def _sized_geometry(self, span_km: float) -> Tuple[GridGeometry, float]:
        """Geometry for a client's requested window, snapped to the
        allowed sizes, clamped so it can never exceed the home box.
        Returns (geometry, snapped_km). span_km 0 = home box itself.

        The window keeps the SAME CENTER as the home box (the design:
        the server watches one box; a smaller window is just a closer
        look at it, not a different place)."""
        home_km = self.geometry.span_m / 1000.0
        if not span_km or span_km <= 0:
            return self.geometry, home_km
        snapped = min(self.SPAN_CHOICES, key=lambda c: abs(c - span_km))
        # A window bigger than the home box just gets the home box
        # (honest: there is no data beyond what the server watches).
        snapped = min(snapped, home_km)
        sized = GridGeometry(
            grid=self.geometry.grid,
            rows=self.geometry.rows,
            center_lat=self.geometry.center_lat,
            center_lon=self.geometry.center_lon,
            span_m=int(snapped * 1000.0),
        )
        return sized, snapped

    def _window_section_ids(self, sized: GridGeometry) -> List[int]:
        """Home-box section ids that lie inside a sized window.

        The window's own grid squares do NOT travel on the wire - the
        client re-derives its sub-grid from the LAYOUT (both sides
        derive geometry from the LAYOUT alone). But the DATA behind the
        answer (active nodes, packets, routes) is bucketed into HOME
        sections, so a sized answer needs the home sections that
        overlap the window, in home numbering."""
        ids: List[int] = []
        for sid in range(1, self.geometry.section_count + 1):
            s = self.geometry.section(sid)
            # STRICT overlap: edge-touching (s.east == window.west)
            # shares a line, not an area - a square that only touches
            # the window border carries no inside data.
            if not (s.east > sized.west and s.west < sized.east and
                    s.north > sized.south and s.south < sized.north):
                continue
            ids.append(sid)
        return ids

    def build_refresh_response(self, kind: int, target: int, *,
                               now: Optional[float] = None,
                               span_km: float = 0.0,
                               sync_marker: int = 0) -> List[OutPacket]:
        """Packets answering one REFRESH_REQ (section or route).

        PROTOCOL v1.2: section targets are 1-based (1 = NW .. 9 = SE)
        and 0 = WHOLE-AREA refresh whatever the kind string: LAYOUT (grid
        geometry so the client can draw the map immediately) + one
        summary per section + node names. (History: target 0 once fell
        through the section range check as section 0, so "Refresh map"
        fetched only the NW square; 2026-09-18 made 0 mean whole-area;
        2026-09-20 made the whole numbering 1-based so the wire, the
        logs, and the screens all agree.)

        v1.3 (MAP-SIZE-DESIGN, Brett 2026-09-23): span_km trims the
        answer to the client's window (20/40/60, snapped; 0/absent =
        the whole home box exactly as before). The LAYOUT carries the
        window's size, so the client draws only that. Airtime saved is
        the honest reward for asking small; the BUDGET cost of the
        request is unchanged (service-side, one limiter)."""
        now = time.time() if now is None else now
        sized, _km = self._sized_geometry(span_km)
        out: List[OutPacket] = []
        if kind == codec.REFRESH_KIND_SECTION:
            if target == codec.REFRESH_WHOLE_AREA:
                out.append(self._build_layout_for(sized, now=now))
                for sid in self._window_section_ids(sized):
                    out.append(self.build_sect_sum(sid, now=now,
                                                   top_routes=[]))
                pkt = self._build_intro_for(sized, now=now,
                                            sync_marker=sync_marker)
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
            pkt = self._build_intro_for(sized, now=now,
                                        sync_marker=sync_marker)
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

    def _build_layout_for(self, sized: GridGeometry, *,
                          now: float) -> OutPacket:
        """A LAYOUT announcing the WINDOW (same center, window span)."""
        area = self.settings.area
        layout = codec.Layout(
            seq=self._next_seq(),
            grid=sized.grid,
            rows=sized.rows,
            center_lat=sized.center_lat,
            center_lon=sized.center_lon,
            span_m=int(sized.span_m),
            origin=self.origin,
            name=area.name[:codec.MAX_NAME],
        )
        return OutPacket(codec.TYPE_LAYOUT, codec.encode_layout(layout),
                         "layout")

    def _ruler_m(self) -> int:
        """THE BIG RULER (Brett's fix, 2026-09-26): one ruler that
        REACHES every node the box has ever heard from the area
        centre - its size travels with the data (wire v1.6), so the
        client rebuilds every position TRUE at any zoom. Nothing is
        pinned at a window edge (the pile-of-dots bug); nothing is
        dropped. Whole km, never smaller than the home box."""
        area = self.settings.area
        needed_deg = 0.0
        for prefix in self.store.known_nodes():
            info = self.store.node_info(prefix) or {}
            lat, lon = info.get("lat"), info.get("lon")
            if lat is None or lon is None:
                continue
            needed_deg = max(needed_deg,
                             abs(float(lat) - area.center_lat),
                             abs(float(lon) - area.center_lon))
        ruler = ((int(needed_deg * 111320.0) // 1000) + 1) * 1000
        return max(ruler, int(area.span_km * 1000.0))

    def _build_intro_for(self, sized: GridGeometry, *,
                         now: float,
                         sync_marker: int = 0) -> Optional[OutPacket]:
        """An INTRO batch whose offsets are relative to the WINDOW's
        center (the client projects names/positions off the LAYOUT it
        just received - sending home-center offsets with a window
        layout would place every dot wrong; the zero-dots lesson).

        VECTORED SYNC (2026-09-24): sync_marker N > 0 filters the batch
        to nodes CHANGED after N (full records for exactly those - the
        phone already holds everything older). marker 0 = every node,
        today's behavior byte-for-byte."""
        area = self.settings.area
        intro = codec.Intro(seq=self._next_seq(), origin=self.origin,
                            center_lat=sized.center_lat,
                            center_lon=sized.center_lon,
                            span_m=self._ruler_m())
        entries: List[codec.IntroEntry] = []
        body_len = 1
        known = sorted(self.store.known_nodes())
        if sync_marker:
            changed = set(self.store.nodes_changed_since(sync_marker))
            known = [p for p in known if p in changed]
        positioned, plain = [], []
        for prefix in known:
            info = self.store.node_info(prefix) or {}
            if info.get("lat") is not None and info.get("lon") is not None:
                positioned.append(prefix)
            else:
                plain.append(prefix)
        ordered_all = positioned + plain
        c = self._intro_cursor % max(1, len(ordered_all))
        ordered = ordered_all[c:] + ordered_all[:c]
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
            while intro.entries and len(codec.encode_intro(intro)) \
                    > codec.TARGET_PAYLOAD:
                intro.entries.pop()
            if not intro.entries:
                return None
            pkt = codec.encode_intro(intro)
        return OutPacket(codec.TYPE_INTRO, pkt, "background")

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

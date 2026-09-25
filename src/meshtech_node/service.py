"""Service orchestrator - ties source, store, builder, budget, client.

Owns the broadcast loop and the multi-host coexistence rules (v1.1):

- PULSE + background section summary on the pulse interval
- LAYOUT on the layout interval, plus a short-interval DISCOVERY
  BEACON in multi-host mode (peers expire after 3 missed beacons)
- every scope packet received goes to on_packet: peer LAYOUTs feed the
  peer table; client REFRESH_REQs are answered by the ELECTED OWNER
  only (deterministic lowest-origin rule; multi_host off = answer all)
- every TX passes the budget limiter FIRST; a refused send is logged
  once and the feed skips that slot (honest gap, never a lie)
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
from typing import Optional

from . import codec
from .budget import BudgetLimiter, RefreshDedupe, RefreshRateLimiter
from .client import CompanionClient
from .config import Settings
from .feedbuilder import FeedBuilder, OutPacket, route_id as route_id_of
from .grid import GridGeometry
from .observations import RollingStore
from .packetsource import (
    DemoSource, contact_row, decode_advert_payload,
    neighbor_of, node_class_of, row_prefix_of,
)
from .peers import PeerTable, SectionOwners

log = logging.getLogger("meshtech-node.service")


class _UnwiredClient:
    """NODE ADAPTATION: what stands in when no radio client is wired.
    Everything refuses honestly: is_connected/has_slot False keep the
    broadcast loop idling (TX OFF, the Gate 1 listen-only default), and
    a direct send logs a refusal and returns False - the same honest
    path a companion refusal takes. Never fakes success."""

    is_connected = False
    has_slot = False

    def send_channel_data(self, data_type: int, payload: bytes) -> bool:
        log.warning("TX refused: no radio client wired (type=%04x) - "
                    "the gap is honest. Wire Adapter B to go on air.",
                    data_type)
        return False

    async def run(self) -> None:
        await asyncio.Event().wait()      # parks until cancelled


def _normalize_ingest(item: object) -> tuple:
    """Normalize one queue item from the packet source into
    (observations, node_rows, extras).

    The 2026-09-20 hilltop lesson: RawPacketSource (Adapter A) puts ONE
    Observation per heard packet, but this drain unpacked a 3-tuple -
    the first real packet raised ValueError, ingest died, its finally
    cancelled the source, and the node went deaf with the radio still
    healthy. The queue contract is now explicit at the boundary:

    - an Observation  -> the real Adapter-A shape (no node rows, no
      extras; advert rows are derived from the observations themselves)
    - a (obs, nodes, extras) triple -> tolerated for future sources
    - anything else   -> logged once and skipped, never fatal
    """
    from .observations import Observation
    if isinstance(item, Observation):
        return [item], None, None
    if isinstance(item, tuple) and len(item) == 3:
        obs, nodes, extras = item
        return (list(obs) if obs else []), nodes, extras
    if isinstance(item, list):
        return item, None, None
    log.warning("ingest dropped an unrecognized queue item: %r", item)
    return [], None, None


class ScopeService:
    def __init__(self, settings: Settings, *, use_demo: bool = False,
                 origin: Optional[int] = None,
                 client: Optional[object] = None):
        self.settings = settings
        self.started_at = time.time()
        self.store = RollingStore(window_seconds=3600.0)
        self.geometry = GridGeometry(
            grid=settings.area.grid,
            center_lat=settings.area.center_lat,
            center_lon=settings.area.center_lon,
            span_m=int(settings.area.span_km * 1000.0),
        )
        # Host identity: explicit origin (tests) > config origin_hex >
        # a random id persisted nowhere (log notes the derivation).
        if origin is not None:
            self.origin = origin & 0xFFFF
        else:
            cfg_hex = (settings.feed.origin_hex or "").lower().removeprefix("0x")
            self.origin = int(cfg_hex, 16) if cfg_hex else \
                secrets.randbits(16)
        self.budget = BudgetLimiter(
            settings.radio, settings.feed.max_packets_per_hour,
            settings.feed.max_duty_percent)
        self.builder = FeedBuilder(settings, self.store, self.geometry,
                                   self.budget, origin=self.origin)
        self.peers = PeerTable()
        self.owners = SectionOwners(self.origin, self.geometry, self.peers)
        self.dedupe = RefreshDedupe(ttl_seconds=600.0)
        self.rate = RefreshRateLimiter(
            settings.feed.refresh_cooldown_seconds,
            settings.feed.refresh_hourly_cap,
            settings.feed.allowed_prefixes)
        # NODE ADAPTATION: the client is INJECTED (Adapter B). No client
        # wired = listen-only, loudly. The plugin's CompanionClient stays
        # seeded (SEED-MAP amendment 2026-09-20): its frame/RX logic is
        # the proven wire reference Adapter B reuses.
        if client is None:
            client = _UnwiredClient()
            log.warning("No radio client wired - TX stays OFF "
                        "(listen-only). The gap is honest.")
        self.client = client
        self.use_demo = use_demo
        self._demo: Optional[DemoSource] = None
        # NODE ADAPTATION: the non-demo ingest source is injected too
        # (Adapter A, RawPacketSource) - the node does not poll anything.
        self.external_source: Optional[object] = None
        self._stop = asyncio.Event()
        self._tx_log_tail = []
        self._startup_seeded = False
        # NODE: the direct-mode feed tap (WebServe). Set by the shell;
        # when present, EVERY packet the feed builds is offered to it
        # here, in _send_burst - before any TX attempt, whatever the
        # outcome. BENCH TRUTH (WEBSERVE-PROTOCOL.md): the same bytes
        # reach the app regardless of TX success; tx_ok reports the
        # radio's actual answer, and tx_enabled=False (Gate 1) keeps
        # would_tx honestly false. in_reply_to comes from the tap's
        # req-id stack while a wire refresh is being dispatched.
        self.feed_tap = None
        self.bench_no_radio = False
        # REFRESH_REQ uplinks are anonymous: without a key prefix the
        # client cannot be followed across requests. A stale deadline
        # stops repeated orphaned uplinks from re-opening the window.
        self._uplink_open_until = 0.0

    # ------------------------------------------------------------------ demo

    def _seed_demo(self) -> None:
        area = self.settings.area
        span_deg = area.span_km * 1000.0 / 111320.0
        self._demo = DemoSource(seed=42, node_count=40,
                                center_lat=area.center_lat,
                                center_lon=area.center_lon,
                                span_deg=span_deg)
        for prefix, lat, lon, name, node_class in self._demo.positions():
            self.store.add_position(prefix, lat, lon, name,
                                    now=self.started_at)
            self.store.add_node_class(prefix, node_class)

    # ------------------------------------------------------------------ RX

    async def on_packet(self, obj: object, sender_prefix: str, *,
                        via_door: bool = False) -> None:
        """ANY scope packet arrived (client uplink or peer broadcast).

        via_door (Brett's law, 2026-09-24): the ask came through the
        TCP data door - "TCP is not the mesh" - so the answer flows
        through the door only: no radio TX, no budget, no limiter,
        no burst gaps."""
        try:
            if isinstance(obj, codec.Layout):
                self._on_peer_layout(obj)
            elif isinstance(obj, codec.RefreshReq):
                await self._handle_refresh(obj, sender_prefix,
                                           via_door=via_door)
            # Everything else is host->client only; hosts ignore it.
        except Exception as exc:
            log.exception("Scope packet handling failed: %s", exc)

    def _on_peer_layout(self, layout: codec.Layout) -> None:
        if layout.origin == 0 or layout.origin == self.origin:
            return  # anonymous or our own packet echoed back
        peer = self.peers.observe_layout(layout)
        log.info("Peer %04x (%s): %dx%d grid, span %.0f km, %d peer(s) known",
                 peer.origin, peer.name or "?", layout.grid, layout.grid,
                 layout.span_m / 1000.0, self.peers.count())

    def _i_own(self, section_id: int) -> bool:
        if not self.settings.feed.multi_host:
            return True  # single-host mode: answer everything
        owner = self.owners.owner_of(section_id)
        if owner == self.origin:
            return True
        if owner is not None:
            log.info("Section %d owned by peer %04x - staying silent",
                     section_id, owner)
        else:
            log.info("Section %d has no live owner - staying silent "
                     "(client will retry)", section_id)
        return False

    async def _handle_refresh(self, req: codec.RefreshReq,
                              sender_prefix: str, *,
                              via_door: bool = False) -> None:
        """VECTORED SYNC (2026-09-24): the ask may carry the client's
        change-counter marker (v1.6 REFRESH_REQ). marker 0 = "not
        vectored" = the full roster (today's behavior, byte-identical
        for old clients). marker N = the answer's INTRO batches ship
        full records ONLY for nodes changed after N, followed by a
        GONE packet listing retired prefixes (the phone REMOVES those
        dots).

        via_door (Brett's law, 2026-09-24): "TCP is not the mesh" - a
        door ask is answered THROUGH THE DOOR: no radio TX, no
        airtime budget, no per-phone limiter, no burst gaps. The
        RADIO path keeps every limit."""
        await self._handle_refresh_inner(req, sender_prefix,
                                         via_door=via_door)

    async def _handle_refresh_inner(self, req: codec.RefreshReq,
                                    sender_prefix: str, *,
                                    via_door: bool = False) -> None:
        # COMPANION MODE: a companion never answers refreshes - not on
        # the air (it has no TX mandate) and not for its own web app
        # (there is no host data to build). Heard packets fill the map;
        # the app's refresh button gets an honest listen_only refusal
        # instead of a silent no-op.
        if self.settings.feed.companion_mode:
            log.info("COMPANION MODE: refresh (kind=%d target=%d) "
                     "refused - listen-only device", req.kind, req.target)
            return
        if req.host not in (0, self.origin):
            log.debug("Refresh targeted at host %04x, not us", req.host)
            return
        # An orphaned uplink (client with no key prefix) re-opens the
        # rate window for a short grace period so its retry can land.
        if sender_prefix == "unknown":
            if time.time() > self._uplink_open_until:
                log.info("Uplink from unknown client - grace window open")
            self._uplink_open_until = time.time() + 60.0
        if self.dedupe.seen_before(req.kind, req.target, req.nonce):
            log.debug("Duplicate refresh (%d,%d,%d) - dropped",
                      req.kind, req.target, req.nonce)
            return
        target = req.target
        if target == codec.REFRESH_WHOLE_AREA:
            pass  # whole-area (0, any kind): every host may answer;
                  # dedupe/rate-limit still gate the burst
        elif req.kind == codec.REFRESH_KIND_SECTION and \
                not self._i_own(target):
            return  # a real square: only its elected owner answers
        if req.kind == codec.REFRESH_KIND_ROUTE and \
                self.settings.feed.multi_host and \
                not self._route_mine(target):
            return
        # THE DOOR BRANCH (Brett's law): the limiter guards the AIR.
        # A door-borne ask consumes no airtime and gets no cooldown -
        # dedupe above still applies (same ask twice = answered once).
        if not via_door and not self.rate.allowed(sender_prefix,
                                                  span_km=req.span_km):
            log.info("Refresh from %s refused by rate limit "
                     "(kind=%d target=%d span=%s)", sender_prefix[:12],
                     req.kind, req.target,
                     ("host" if not req.span_km else f"{req.span_km}km"))
            return
        if not via_door:
            self.rate.record(sender_prefix, span_km=req.span_km)
        # v1.3 (MAP-SIZE-DESIGN): the request may carry the client's
        # wanted window (span_km, 20/40/60 snapped; 0 = whole home box).
        # The budget was ALREADY spent above (rate.record) - the sized
        # answer changes how many packets fly, never whether the limiter
        # counted. A smaller window costs less airtime; the ask itself
        # is still "one map refresh" to the limiter.
        packets = self.builder.build_refresh_response(
            req.kind, req.target, span_km=req.span_km,
            sync_marker=req.sync_marker)
        # WHOLE-AREA refresh also carries a PULSE (last in the burst):
        # the app's Feed-health card reads it, and Brett's rule is a
        # refresh answers with a LIVE map, never "no pulse yet"
        # (2026-09-21). Section-only refreshes stay cheap.
        if target == codec.REFRESH_WHOLE_AREA:
            packets.append(self.build_pulse_now())
        # VECTORED SYNC: a vectored ask also learns the RETIREMENTS
        # (name-supersede, 30-day prune) - one GONE packet per 8
        # prefixes, after the data. The events are cleared only AFTER
        # the burst is on the wire (a crash re-sends them; honest).
        gone = self.builder.store.gone_since(req.sync_marker) \
            if req.sync_marker else []
        if gone:
            for i in range(0, len(gone), codec.MAX_GONE_PER_PACKET):
                chunk = gone[i:i + codec.MAX_GONE_PER_PACKET]
                packets.append(OutPacket(
                    codec.TYPE_GONE,
                    codec.encode_gone(seq=self.builder._next_seq(),
                                      origin=self.origin, prefixes=chunk),
                    "gone"))
            log.info("Vectored sync: %d node(s) gone -> %d GONE packet(s)",
                     len(gone), (len(gone) + codec.MAX_GONE_PER_PACKET - 1)
                     // codec.MAX_GONE_PER_PACKET)
        log.info("Refresh from %s: kind=%d target=%d span=%s marker=%d "
                 "-> %d packet(s)%s", sender_prefix[:12], req.kind,
                 req.target,
                 ("host" if not req.span_km else f"{req.span_km}km"),
                 req.sync_marker, len(packets),
                 " (door-borne, wire only)" if via_door else "")
        if via_door:
            # THE DOOR'S ANSWER: wire only. No radio TX, no airtime
            # budget, no artificial gaps - the FeedTap serves every
            # packet straight to the connected clients.
            for out in packets:
                if self.feed_tap is not None:
                    self._tap(out, would_tx=False, tx_ok=False,
                              in_reply_to=self.feed_tap.current_req_id)
            if gone:
                self.builder.store.clear_gone(gone)
            return
        await self._send_burst(packets)
        if gone:
            self.builder.store.clear_gone(gone)

    def build_pulse_now(self) -> OutPacket:  # noqa: F821 (feedbuilder)
        """A PULSE with the service's HONEST uptime (not the builder's
        placeholder). One builder for every pulse: the cadence loop,
        connect-time answers, and whole-area refresh bursts all speak
        the same truth."""
        pulse = self.builder.build_pulse()
        now = time.time()
        # payload = type(2)+len(1)+ver(1)+seq(2)+origin(2)+uptime(2)
        # -> uptime bytes at [8:10]
        pulse.payload = bytearray(pulse.payload)
        pulse.payload[8:10] = (int((now - self.started_at) // 60)
                               & 0xFFFF).to_bytes(2, "little")
        pulse.payload = bytes(pulse.payload)
        return pulse

    async def pulse_now(self, *, reason: str = "pulse_now",
                        with_layout: bool = False) -> None:
        """Build and send a PULSE immediately. Called when a new web
        client connects (Brett, 2026-09-21: a fresh app must see the
        Feed-health card fill right away, not wait up to one cadence).
        Airtime-honest: one ~20 B packet through the usual budget; with
        TX off it is refused on the air and STILL served on the wire
        tap - exactly the cadence pulse's behavior.

        with_layout=True (the CONNECT case, Brett 2026-09-21): the burst
        opens with the LAYOUT (map frame) first, so a freshly opened app
        draws the area grid the instant it connects - no refresh press,
        no waiting for the hourly cadence. Layout-then-pulse order
        matches the whole-area refresh burst (map frame before data)."""
        burst = []
        if with_layout:
            burst.append(self.builder.build_layout())
            # SECTION SUMMARIES on connect (Brett, 2026-09-24: "routes
            # appear immediately on every fresh connect"): one summary
            # per home square, each carrying its route stubs (the ID
            # bookmarks a section screen lists - the trail detail still
            # downloads only when a route is tapped). Without these a
            # fresh connect draws dots but shows "No routes yet" until
            # the next cadence beat or a manual refresh.
            for sid in range(1, self.geometry.section_count + 1):
                if self._i_own(sid):
                    burst.append(self.builder.build_sect_sum(sid))
        burst.append(self.build_pulse_now())
        log.info("PULSE on demand (%s%s) - uptime %d min",
                 reason, " + LAYOUT" if with_layout else "",
                 int(time.time() - self.started_at) // 60)
        await self._send_burst(burst, gap=0.0)
        if with_layout:
            # NODE ROSTER (Brett, 2026-09-21): the connect burst also
            # carries the full INTRO roster (names/positions/classes),
            # so the app's mapped-nodes count fills on CONNECT - not
            # only after a refresh press.
            await self._send_intro_roster()

    async def _send_intro_roster(self) -> None:
        """Send INTRO batches until every known node has gone out once.

        build_intro_batch cycles forever (the cadence uses that to
        rotate which nodes ride along), so a naive cursor count both
        over- and under-shoots (proven in the sandbox: mid-cycle
        batches repeat the positioned prefix and skip plain ones).
        The honest stop condition: track the PREFIXES actually sent
        and stop when no batch adds anything new. Typically 2-4 small
        packets (~100 B body each); the 32-batch cap is belt-and-braces
        against a pathological roster."""
        total = len(self.builder.store.known_nodes())
        if total == 0:
            return
        seen: set = set()
        batches = 0
        while batches < 32:
            pkt = self.builder.build_intro_batch()
            if pkt is None:
                break
            fresh = [e for e in codec.decode_any(pkt.payload).entries
                     if e.prefix not in seen]
            await self._send_burst([pkt], gap=0.0)
            if not fresh:
                break        # this batch added nothing new - roster done
            seen.update(e.prefix for e in fresh)
            batches += 1
            if len(seen) >= total:
                break

    def _route_mine(self, route_id: int) -> bool:
        """Multi-host route check: any section I own contains this route."""
        for sid in range(1, self.geometry.section_count + 1):
            if not self._i_own(sid):
                continue
            for path, _count, _delays, _last in \
                    self.builder._routes_for_section(sid):
                if route_id_of(path) == route_id:
                    return True
        return False

    # ------------------------------------------------------------------ TX

    async def _send_burst(self, packets, *, gap: Optional[float] = None) -> None:
        gap = self.settings.feed.burst_gap_seconds if gap is None else gap
        for i, out in enumerate(packets):
            if i > 0:
                # jittered gap (DM-saga lesson: never machine-gun the air)
                await asyncio.sleep(gap + random.uniform(0, gap * 0.5))
            if not self.budget.allow(len(out.payload)):
                log.warning("Budget cap reached - dropping %s packet "
                            "(type %04x). The gap is honest.",
                            out.reason, out.data_type)
                if self.feed_tap is not None:
                    self._tap(out, would_tx=self.tx_enabled, tx_ok=False,
                              in_reply_to=self.feed_tap.current_req_id)
                return
            ok = await self.client.send_channel_data(out.data_type, out.payload)
            if ok:
                tx_ms = self.budget.record(len(out.payload))
                log.info("TX %s type=%04x %dB est. %.0fms",
                         out.reason, out.data_type, len(out.payload), tx_ms)
            else:
                log.warning("TX refused by companion (%s type=%04x)",
                            out.reason, out.data_type)
            if self.feed_tap is not None:
                self._tap(out, would_tx=self.tx_enabled, tx_ok=bool(ok),
                          in_reply_to=self.feed_tap.current_req_id)

    def _tap(self, out, *, would_tx: bool, tx_ok: bool,
             in_reply_to: Optional[str]) -> None:
        """Offer one built packet to the direct-mode tap. Never raises:
        a broken tap must not take the feed down (listener rule)."""
        try:
            self.feed_tap.on_built_packet(
                out.data_type, bytes(out.payload),
                would_tx=would_tx, tx_ok=tx_ok,
                in_reply_to=in_reply_to)
        except Exception:
            log.exception("feed_tap raised - packet not served over WS")

    # ------------------------------------------------------------------ loops

    def _ingest_node_rows(self, nodes: object) -> None:
        """Enrich the store from advert rows: position, name, class.

        SOURCE TRUTH (2026-09-18): the repeater has NO /nodes endpoint
        - node data arrives as rows of the `adverts` table (served at
        /api/adverts_by_contact_type): pubkey (full hex, first 2 chars
        = prefix), node_name, latitude/longitude, contact_type string,
        is_repeater, last_seen. Stale entries (adverts are periodic,
        hours apart) are filtered by the caller's configured window so
        the feed never publishes long-silent nodes as active.
        Class uses node_class_of; UNKNOWN never overwrites a known
        class (store rule).
        """
        if not isinstance(nodes, list):
            return
        cutoff = time.time() - self._advert_fresh_seconds()
        for row in nodes:
            if not isinstance(row, dict):
                continue
            prefix = row_prefix_of(row)
            if prefix is None:
                continue
            # Freshness: last_seen is epoch seconds; rows without it are
            # kept (honest) - the API always sets it on real rows.
            last_seen = row.get("last_seen")
            try:
                if last_seen and float(last_seen) < cutoff:
                    continue  # stale: not published as active
            except (TypeError, ValueError):
                pass
            name = row.get("node_name") or row.get("name")
            lat, lon = row.get("latitude"), row.get("longitude")
            if lat is not None and lon is not None:
                try:
                    lat_f, lon_f = float(lat), float(lon)
                except (TypeError, ValueError):
                    pass  # malformed position - skip, never fabricate
                else:
                    if lat_f != 0.0 or lon_f != 0.0:
                        self.store.add_position(prefix, lat_f, lon_f, name)
            node_class = node_class_of(row)
            if node_class:
                self.store.add_node_class(prefix, node_class)

    def _advert_fresh_seconds(self) -> float:
        """How old an advert row may be and still count as active.

        MeshCore adverts are periodic (hours apart by default). The
        window defaults generous (24 h) so a mesh that advertises a
        few times a day still shows; config can tighten it.
        """
        raw = getattr(self.settings.feed, "advert_fresh_seconds", 86400.0)
        try:
            return max(60.0, float(raw))
        except (TypeError, ValueError):
            return 86400.0

    def _ingest_advert_rows(self, observations) -> None:
        """Node identity from ADVERT packet rows (the live node table).

        LIVE TRUTH (2026-09-18, finding C1): hilltop's adverts TABLE is
        empty (the API requires contact_type ints and nothing persists
        there), but adverts flow in the packet stream as type-0x04
        rows (~190 per 1000 on hilltop). Their payloads carry name,
        lat/lon and the class nibble (openhop_core layout,
        live-confirmed rows 105713/105778) - so the observations the
        source already decoded are the node table. Staleness: an
        advert observation is by definition last_seen=now within the
        rolling window; the class/position store rules (unknown never
        overwrites known; no position fabricated) do the honesty work.
        """
        for obs in observations:
            if getattr(obs, "node_name", None):
                self.store.add_name(obs.prefix, obs.node_name)
            if getattr(obs, "node_class", 0):
                self.store.add_node_class(obs.prefix, obs.node_class)
            if getattr(obs, "lat", None) is not None and \
                    getattr(obs, "lon", None) is not None:
                try:
                    lat_f, lon_f = float(obs.lat), float(obs.lon)
                except (TypeError, ValueError):
                    continue  # never fabricate a position
                if lat_f != 0.0 or lon_f != 0.0:
                    self.store.add_position(obs.prefix, lat_f, lon_f,
                                            obs.node_name)

    def _ingest_extras(self, extras: object) -> None:
        """Backbone neighbors + companion contacts (bench probes C2/D).

        neighbor_links rows become the store's BackboneNeighbor table -
        the repeater MEASURES these links itself, so "top backbone
        neighbors" is re-published, not re-derived from paths.
        contacts rows SUPPLEMENT the node table: they can carry names,
        classes and positions for nodes whose adverts were not heard
        in the current window. Honesty rules: a contact's last_advert
        must clear the SAME advert-freshness window (stale 2024 rows
        exist on hilltop); 0.0/0.0 GPS means no position; class only
        from the adv_type ints we recognise; unknown never overwrites
        known (store rule).
        """
        if not isinstance(extras, dict):
            return
        neighbors = extras.get("neighbors")
        if isinstance(neighbors, list):
            for row in neighbors:
                if isinstance(row, dict):
                    nb = neighbor_of(row)
                    if nb is not None:
                        self.store.add_backbone_neighbor(nb)
        contacts = extras.get("contacts")
        if isinstance(contacts, list):
            cutoff = time.time() - self._advert_fresh_seconds()
            for row in contacts:
                if not isinstance(row, dict):
                    continue
                c = contact_row(row)
                if c is None:
                    continue
                if c["last_advert"] is not None and c["last_advert"] < cutoff:
                    continue  # stale contact: not published as current
                if c["name"]:
                    self.store.add_name(c["prefix"], c["name"])
                if c["node_class"]:
                    self.store.add_node_class(c["prefix"], c["node_class"])
                if c["lat"] is not None and c["lon"] is not None:
                    self.store.add_position(c["prefix"], c["lat"], c["lon"],
                                            c["name"])

    async def ingest_loop(self) -> None:
        """Keeps the rolling store fed (demo ticks or API polling)."""
        if self.use_demo:
            self._seed_demo()
            while not self._stop.is_set():
                obs = self._demo.tick(time.time(), packets=6)
                for o in obs:
                    self.store.add(o)
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    pass
            return
        source = self.external_source
        if source is None:
            log.warning("No packet source wired - ingest idles. "
                        "Wire Adapter A (RawPacketSource) or use_demo. "
                        "The gap is honest.")
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
            return
        queue: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        task = asyncio.create_task(source.run(queue, stop))
        try:
            while not self._stop.is_set():
                item = await queue.get()
                # ADAPTER-A SEAM (the 2026-09-20 hilltop silent death):
                # the source puts ONE Observation per packet (a real
                # heard advert). Accept that shape, and be tolerant of
                # a list/triple shape only if a future source produces
                # it - never crash the ingest chain on a shape drift.
                obs_list, nodes, extras = _normalize_ingest(item)
                for o in obs_list:
                    self.store.add(o)
                self._ingest_node_rows(nodes)
                self._ingest_advert_rows(obs_list)
                self._ingest_extras(extras)
        finally:
            stop.set()
            task.cancel()

    async def broadcast_loop(self) -> None:
        """PULSE + LAYOUT + discovery beacon + background summaries.

        Waits for the companion link (and the scope channel slot) before
        the first broadcast: at startup the client connects asynchronously
        and every early TX would otherwise be dropped locally. Timers are
        seeded on link-up so the first PULSE goes out one full interval
        later, not the moment the link opens.

        COMPANION MODE (2026-09-20): parked entirely. A companion device
        has no host feed - its map comes from what it HEARS. Parked
        before the first seed tick, so nothing is ever built, budgeted,
        or (if TX were ever misconfigured on) sent.
        """
        feed = self.settings.feed
        if feed.companion_mode:
            log.info("COMPANION MODE: host feed parked - the map builds "
                     "from heard packets only. No pulses, no layout, "
                     "no on-air TX (there is nothing of ours to send).")
            await asyncio.Event().wait()      # parks until cancelled
            return
        while not self._stop.is_set():
            if not (self.client.is_connected and self.client.has_slot):
                if self._startup_seeded:
                    self._startup_seeded = False  # link dropped: re-seed
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                continue
            # SEATBELT (2026-09-21): the whole tick runs guarded. A
            # single bad packet (or any builder error) used to raise
            # straight out of the loop and kill the feed SILENTLY - the
            # app stayed connected while pulses/layouts/summaries all
            # stopped (the 16:13 section-0 feed-killer). Now: the error
            # is logged loudly and the cadence continues.
            try:
                if not self._startup_seeded:
                    now = time.time()
                    self.builder._last_layout = now
                    self.builder._last_beacon = now
                    self.builder._last_pulse = now
                    self._startup_seeded = True
                    log.info("Companion link ready - feed cadence starts now "
                             "(first PULSE in ~%ds)", feed.pulse_interval_seconds)
                    # NODE (FEEDBUILDER-CHANGES #1): the first LAYOUT goes
                    # out AT link-ready, not a full interval later. The
                    # direct-mode app's map draws the moment it connects,
                    # and a restarted node repaints stale client maps in
                    # seconds instead of an hour.
                    pkt = self.builder.build_layout()
                    await self._send_burst([pkt], gap=0.0)
                now = time.time()
                if now - self.builder._last_layout >= feed.layout_interval_seconds:
                    # C3: prune the node table on the layout cadence (rare):
                    # 14d silent -> stale (off maps), 30d -> forgotten.
                    counts = self.store.prune_nodes(now=now)
                    # REPEATER PRUNE (2026-09-21): the table's own expiry
                    # (30d silent tags) existed but was NEVER CALLED - the
                    # one RAM structure that could grow forever in a
                    # long-running process. Same rare cadence, disk mirror
                    # included. (Found in Brett's RAM-bloat audit.)
                    pruned_tags = self.external_source.repeaters.prune(now=now) \
                        if self.external_source is not None else 0
                    if counts["stale"] or counts["forgotten"] or pruned_tags:
                        log.info("node table pruned: %d stale, %d forgotten, "
                                 "%d repeater tag(s) expired (table: %d nodes)",
                                 counts["stale"], counts["forgotten"],
                                 pruned_tags, self.store.active_nodes_ever())
                    pkt = self.builder.build_layout()
                    await self._send_burst([pkt], gap=0.0)
                    self.builder._last_layout = now
                    self.builder._last_beacon = now
                elif feed.multi_host and \
                        now - self.builder._last_beacon >= feed.layout_beacon_seconds:
                    # Discovery beacon: an identical LAYOUT, sent on the short
                    # multi-host cadence so peers keep us alive between the
                    # hourly full LAYOUTs. Costs one ~30 B packet per beacon.
                    pkt = self.builder.build_layout()
                    await self._send_burst([pkt], gap=0.0)
                    self.builder._last_beacon = now
                    log.debug("Discovery beacon sent (origin %04x)", self.origin)
                if now - self.builder._last_pulse >= feed.pulse_interval_seconds:
                    pulse = self.build_pulse_now()
                    sect = self.builder.build_sect_sum(self.builder._background_section)
                    self.builder._background_section = \
                        (self.builder._background_section %
                         self.geometry.section_count) + 1  # rotate 1..9, never 0
                    await self._send_burst([pulse, sect], gap=feed.burst_gap_seconds)
                    self.builder._last_pulse = now
            except Exception:
                log.exception("broadcast tick FAILED - the feed keeps "
                              "running (seatbelt), next tick in 5s")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        tasks = [
            asyncio.create_task(self.client.run(), name="companion"),
            asyncio.create_task(self.ingest_loop(), name="ingest"),
            asyncio.create_task(self.broadcast_loop(), name="broadcast"),
        ]
        log.info("Scope feed running: channel %s, origin %04x, %dx%d grid, "
                 "pulse %.0fs, beacon %.0fs, multi_host=%s, "
                 "budget %d pkt/h @ %.1f%% duty",
                 self.settings.channel.name, self.origin,
                 self.settings.area.grid,
                 self.settings.area.grid,
                 self.settings.feed.pulse_interval_seconds,
                 self.settings.feed.layout_beacon_seconds,
                 self.settings.feed.multi_host,
                 self.settings.feed.max_packets_per_hour,
                 self.settings.feed.max_duty_percent)
        try:
            await self._stop.wait()
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def stop(self) -> None:
        self._stop.set()

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
from dataclasses import dataclass
from typing import Deque, Dict, List, Optional, Tuple

log = logging.getLogger("meshtech-node.observations")

# Routes cap their path at the same width the wire's ROUTE packet can
# carry (MAX_PREFIXES_PER_ROUTE in feedbuilder is 8; importing it here
# would make a loop, so the number lives in both places with a test
# pinning them together).
MAX_PREFIXES_PER_ROUTE = 8


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

    def __init__(self, window_seconds: float = 3600.0,
                 *, disk: Optional["object"] = None):
        self.window_seconds = window_seconds
        self._obs: Deque[Observation] = deque()
        # node prefix -> (lat, lon, name, last_advert_ts)
        self._nodes: Dict[int, Dict[str, object]] = {}
        # prefix -> BackboneNeighbor (direct RF links of the host box)
        self._neighbors: Dict[int, BackboneNeighbor] = {}
        # ROUTE MEMORY (2026-09-24, Brett: routes were never meant to be
        # RAM-only): path tuple -> {first, last, count, delays}.
        # The 1-hour packet window stays RAM-only (scope rule); the
        # routes DERIVED from it get the nodes' own disk memory.
        self._routes: Dict[Tuple[int, ...], Dict[str, object]] = {}
        # Optional NodeStore (disk write-through + boot refill).
        self.disk = disk
        # SECTION PLUMBING for routes: the store itself has no grid;
        # the ingest seam sets `section_of` (an Observation -> square)
        # so every heard packet can stamp WHERE it was heard. None =
        # routes carry section -1 (honestly unknown).
        self.section_of = None
        # STORED-ROUTE RESOLVER (v00.000.047): the ingest seam sets
        # `path_section_of` (a path -> square lookup through the node
        # facts) so a boot refill can re-anchor routes whose square
        # was unknown when first heard. None = no re-check.
        self.path_section_of = None
        # VECTORED SYNC (2026-09-24, VECTORED-SYNC-DESIGN.md): the RAM
        # mirror of the disk counter (the disk copy is the authority;
        # this keeps RAM-only stores honest too). Private _sig/_seq
        # keys ride on each node dict: the facts signature detects a
        # REAL change (name/position/class) and _seq is the node's
        # change stamp.
        self._sync_seq = 0

    # ------------------------------------------------------------ input

    def add(self, obs: Observation, *, now: Optional[float] = None) -> None:
        now = time.time() if now is None else now
        self._obs.append(obs)
        self._prune(now)
        # ROUTE MEMORY: EVERY heard packet forms a route. A packet with
        # repeaters = the repeater trail; a packet heard DIRECT (no
        # repeaters) = a one-hop route anchored on the sender (Brett,
        # 2026-09-24: "a route is any persistent path between two nodes
        # - not just multi-hop traffic"). prefix=0 (identity unknown)
        # can prove no path endpoint, so it forms no route.
        if obs.prefix != 0:
            direct = not obs.path_prefixes
            path = tuple(obs.path_prefixes[:MAX_PREFIXES_PER_ROUTE]) \
                if obs.path_prefixes else (obs.prefix,)
            self.add_route(path, recv_ts=obs.recv_ts, delay_s=obs.delay_s,
                           now=now, sender_prefix=obs.prefix,
                           direct=direct,
                           section_id=self._last_section(obs))

    # ------------------------------------------------- route memory

    # Brett's fade law (2026-09-24), by route kind:
    #   DIRECT (one hop, heard straight from the sender):
    #     silent 3 days -> STALE (kept, listed yellow),
    #     silent 7 days -> DEAD (deleted from RAM and disk).
    #   MULTI-HOP (a repeater trail):
    #     silent 7 days -> STALE, 14 days -> DEAD.
    DIRECT_STALE_S = 3 * 86400.0
    DIRECT_DEAD_S = 7 * 86400.0
    MULTIHOP_STALE_S = 7 * 86400.0
    MULTIHOP_DEAD_S = 14 * 86400.0
    MAX_ROUTES = 512   # the RAM table's ceiling; disk mirrors via delete

    def _last_section(self, obs: Observation) -> int:
        """The square an observation was heard in, via the injected
        section function (-1 = unknown / not wired)."""
        if self.section_of is None:
            return -1
        try:
            return int(self.section_of(obs))
        except Exception:
            return -1

    @staticmethod
    def route_is_direct(entry: Dict[str, object]) -> bool:
        """DIRECT = the packet was heard straight from its sender (no
        repeater relayed it). Marked EXPLICITLY at the hearing (wire
        truth) - a path tuple of length 1 cannot distinguish "heard
        direct" from "relayed through one repeater" (both are one
        hop on the wire's ROUTE packet), and Brett's fade law gives
        the two DIFFERENT lifetimes (3/7 vs 7/14)."""
        return bool(entry.get("direct"))

    def add_route(self, path: Tuple[int, ...], *, recv_ts: float,
                  delay_s: Optional[float], now: float,
                  sender_prefix: int = 0, direct: bool = False,
                  section_id: int = -1) -> None:
        """Record one use of a route (write-through to disk, best-effort
        the same way the node table is: a database hiccup never takes
        the RX path down).

        section_id: the home square the traffic was HEARD in (the
        observation's own section, -1 = unknown). Stored WITH the
        route - a trail's hops are repeater tags, not nodes, so a
        restored route's section cannot be re-derived from its path
        after a restart; the honest memory is where it was heard.

        Delay samples are CAPPED: the disk row stores count + median,
        and a route with a hundred thousand uses must not drag a
        hundred-thousand-element list through RAM forever. Keeping the
        newest 64 keeps the median honest to recent behavior.
        """
        entry = self._routes.setdefault(
            path, {"first": recv_ts, "last": recv_ts, "count": 0,
                   "delays": [], "sender": 0, "direct": False,
                   "section": section_id})
        entry["last"] = max(float(entry["last"]), recv_ts)
        entry["count"] = int(entry["count"]) + 1
        if section_id >= 0:
            entry["section"] = section_id
        if direct:
            entry["direct"] = True
        if sender_prefix:
            entry["sender"] = sender_prefix
        if delay_s is not None:
            delays = entry["delays"]
            delays.append(float(delay_s))
            if len(delays) > 64:
                del delays[:-64]
        self._disk_route(path, entry, now=now)

    def _disk_route(self, path: Tuple[int, ...], entry: Dict[str, object],
                    *, now: float) -> None:
        if self.disk is None:
            return
        try:
            delays = sorted(float(d) for d in entry["delays"])
            med = int(round(delays[len(delays) // 2])) if delays else 0
            self.disk.upsert_route(
                path_bytes=bytes(p & 0xFF for p in path),
                first_heard=float(entry["first"]),
                last_heard=float(entry["last"]),
                packet_count=int(entry["count"]),
                delay_med_s=med,
                sender_prefix=int(entry.get("sender") or 0),
                is_direct=bool(entry.get("direct")),
                section_id=int(entry.get("section") or -1),
                ts=now)
        except Exception:
            log.exception("route disk write-through failed (path %s) "
                          "- RAM keeps the truth",
                          ".".join(f"{p:02x}" for p in path))

    def refill_routes(self, rows: List[dict]) -> int:
        """Boot refill: restore the disk route table into RAM with their
        ORIGINAL first/last times (a week-silent route is still a week
        silent). RAM wins where both exist. Returns rows restored."""
        restored = 0
        for row in rows:
            try:
                path = tuple(byte for byte in bytes.fromhex(
                    str(row.get("path_hex") or "")))
            except ValueError:
                continue          # corrupt row: skip, never crash boot
            if not path or path in self._routes:
                continue
            self._routes[path] = {
                "first": float(row.get("first_heard") or 0.0),
                "last": float(row.get("last_heard") or 0.0),
                "count": int(row.get("packet_count") or 0),
                "sender": int(row.get("sender_prefix") or 0),
                "direct": bool(row.get("is_direct")),
                "section": int(row.get("section_id") or -1),
                "delays": ([float(row["delay_med_s"])]
                           if row.get("delay_med_s") else []),
            }
            restored += 1
        if restored:
            log.info("route table refilled from disk: %d route(s) "
                     "restored (RAM table now %d)", restored,
                     len(self._routes))
        return restored

    def reanchor_routes(self) -> int:
        """Startup re-check (Brett's v00.000.047, 2026-09-25): a
        stored route with an unknown square gets one NOW if its facts
        have arrived since (sender, trail end, or a placed hop).
        RAM and disk move together; still-unplaceable routes stay
        honestly held (-1), never guessed. Returns rows placed."""
        if self.path_section_of is None:
            return 0
        placed = 0
        for path, entry in self._routes.items():
            if int(entry.get("section") or -1) >= 0:
                continue
            try:
                section = int(self.path_section_of(path))
            except Exception:
                log.exception("route re-anchor failed (path %s) - the "
                              "route stays held",
                              ".".join(f"{p:02x}" for p in path))
                continue
            if section < 0:
                continue
            entry["section"] = section
            self._disk_route(path, entry, now=time.time())
            placed += 1
        if placed:
            log.info("route re-anchor: %d route(s) gained their square "
                     "at boot", placed)
        return placed

    def prune_routes(self, *, now: Optional[float] = None) -> Tuple[int, int]:
        """Brett's route fade (the mirror of prune_nodes): a DIRECT
        route silent 3 days goes STALE (kept, listed yellow) and 7 days
        DEAD (deleted); a MULTI-HOP route 7/14. Returns the counts for
        honest logging. A route's death never touches the node table -
        nodes keep their own 14/30 law."""
        now = time.time() if now is None else now
        stale = dead = 0
        dead_paths = []
        for path, entry in self._routes.items():
            age = now - float(entry["last"])
            if self.route_is_direct(entry):
                stale_after, dead_after = (self.DIRECT_STALE_S,
                                           self.DIRECT_DEAD_S)
            else:
                stale_after, dead_after = (self.MULTIHOP_STALE_S,
                                           self.MULTIHOP_DEAD_S)
            if age > dead_after:
                dead_paths.append(path)
            elif age > stale_after:
                entry["stale"] = True
                stale += 1
        for path in dead_paths:
            del self._routes[path]
            dead += 1
        if self.disk is not None and dead_paths:
            try:
                self.disk.forget_routes(
                    [bytes(p & 0xFF for p in path) for path in dead_paths])
            except Exception:
                log.exception("route disk prune failed - RAM stays the "
                              "truth")
        return stale, dead

    def routes_all(self) -> List[Tuple[Tuple[int, ...], Dict[str, object]]]:
        """Every route held (fresh AND stale - the phone lists stale
        routes in yellow; dead ones are already deleted)."""
        return list(self._routes.items())

    def route_entry(self, path: Tuple[int, ...]) -> Optional[Dict[str, object]]:
        """One route's record, or None."""
        return self._routes.get(path)

    # ------------------------------------------------- vectored sync

    def _fact_sig(self, node: Dict[str, object]) -> tuple:
        """The stored facts a change-counter cares about. Hearing
        again with no new facts is NOT a change (the marker must not
        burn on silence)."""
        return (node.get("name"), node.get("lat"), node.get("lon"),
                node.get("node_class"))

    def sync_seq(self) -> int:
        """The table-wide change counter (disk-backed authority)."""
        if self.disk is not None:
            try:
                return self.disk.sync_seq()
            except Exception:
                log.exception("sync counter read failed - RAM mirror")
        return self._sync_seq

    def node_change_seq(self, prefix: int) -> int:
        """The change stamp one node row carries (0 = unstamped)."""
        node = self._nodes.get(prefix)
        return int(node.get("_seq") or 0) if node else 0

    def nodes_changed_since(self, marker: int) -> List[int]:
        """Prefixes whose stored facts changed after the marker (the
        vectored answer ships FULL records for exactly these)."""
        if self.disk is not None:
            try:
                return self.disk.changed_node_rows_since(marker)
            except Exception:
                log.exception("changed-since query failed - empty answer")
                return []
        return [p for p, n in self._nodes.items()
                if int(n.get("_seq") or 0) > int(marker)]

    def gone_since(self, marker: int = 0) -> List[int]:
        """Retired prefixes queued for the phone (name-supersede and
        the 30-day prune both land here). Consumed by the asker via
        clear_gone AFTER they are on the wire."""
        if self.disk is None:
            return []
        try:
            return self.disk.pending_gone()
        except Exception:
            log.exception("gone query failed - empty answer")
            return []

    def clear_gone(self, prefixes: List[int]) -> None:
        if self.disk is not None and prefixes:
            try:
                self.disk.clear_gone(prefixes)
            except Exception:
                log.exception("gone clear failed - events stay queued")

    def _disk_node(self, prefix: int, node: Dict[str, object], *,
                   now: float) -> None:
        """Write one node's current facts through to disk (best-effort:
        a database hiccup never takes the RX path down; the RAM table
        stays the working truth, retried on the next fact).

        VECTORED SYNC: the SINGLE bump point. A write whose facts
        actually changed (name/position/class differ from the last
        write) advances the table-wide counter and stamps the row;
        a re-heard silence writes last_seen and costs the marker
        nothing. Order is bump -> upsert-with-stamp so a crash can
        only ever leave the row looking OLD (resent later), never
        NEW-and-missed."""
        sig = self._fact_sig(node)
        changed = sig != node.get("_sig")
        if changed:
            if self.disk is not None:
                try:
                    self._sync_seq = self.disk.bump_sync_seq()
                except Exception:
                    log.exception("sync counter bump failed (prefix %02x) "
                                  "- writing facts without a stamp", prefix)
                    changed = False
            else:
                self._sync_seq += 1
        if self.disk is None:
            if changed:
                node["_sig"] = sig
                node["_seq"] = self._sync_seq
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
                ts=now,
                change_seq=self._sync_seq if changed else None)
            if changed:
                node["_sig"] = sig
                node["_seq"] = self._sync_seq
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
        # NAME SUPERSEDE (2026-09-23, Brett's corrected design): the
        # name is the node's identity on the map, and identities are
        # allowed to MOVE (mobile devices). A fresh advert carrying a
        # name that matches an EXISTING DIFFERENT prefix is a verified
        # change of location: the FRESHEST advert wins and the old dot
        # for that name is retired (RAM + disk). NO distance test, no
        # motion plausibility judgment - the name is the proof. The
        # planet-range and half-fix guards ABOVE already ran, so an
        # implausible (torn/corrupt) advert is filtered before any of
        # this: it keeps its name/hearing evidence but never relocates
        # nor deletes a good dot. A nameless advert cannot prove a
        # name match and never supersedes.
        if name:
            victim = self.node_with_name(name, exclude_prefix=prefix)
            if victim is not None:
                old = self._nodes.pop(victim)
                # Inherit the retired identity's extra facts (class)
                # only where the fresh advert is silent - unknown
                # never overwrites known.
                for k, v in old.items():
                    if k not in ("name", "lat", "lon", "last_advert_ts",
                                 "stale", "_sig", "_seq"):
                        node.setdefault(k, v)
                node.update({"lat": lat, "lon": lon,
                             "last_advert_ts": now})
                node["name"] = name
                node.pop("stale", None)
                log.info("name superseded: %02x -> %02x (fresh advert "
                         "for '%s'; freshest wins, old dot retired)",
                         victim, prefix, name)
                if self.disk is not None:
                    try:
                        self.disk.forget_node(victim)
                    except Exception:
                        log.exception("superseded disk removal failed "
                                      "(prefix %02x) - RAM stays the "
                                      "truth", victim)
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
            # NAME SUPERSEDE at boot (2026-09-23): disk rows written
            # before the name rule existed can hold BOTH identities of
            # one node - and by Brett's rule ANY two rows sharing a
            # name are one node (the name is the operator's identity).
            # Restoring both would redraw the double dots after every
            # restart. The FRESHER identity is kept (the older row's
            # class rides along where the fresher is silent) and the
            # OLDER row is deleted from disk - in BOTH directions, so
            # one pass converges the table to one row per name.
            nm = node.get("name")
            if nm is not None:
                twin = self.node_with_name(str(nm), exclude_prefix=prefix)
                if twin is not None:
                    old_ts = float(self._nodes[twin].get("last_advert_ts",
                                                         0))
                    new_ts = float(node["last_advert_ts"])
                    if new_ts <= old_ts:
                        # the restored row IS the older identity: it
                        # loses now and must never come back either
                        if self.disk is not None:
                            try:
                                self.disk.forget_node(prefix)
                            except Exception:
                                log.exception("older twin disk removal "
                                              "failed (prefix %02x)",
                                              prefix)
                        continue
                    old = self._nodes.pop(twin)
                    # The retired twin was itself restored earlier in
                    # this boot pass, so the honest count is the table
                    # that REMAINS (one row per name), not rows read.
                    restored -= 1
                    for k, v in old.items():
                        if k not in ("name", "lat", "lon",
                                     "last_advert_ts", "stale"):
                            node.setdefault(k, v)
                    if now - new_ts > self.STALE_AFTER_S:
                        node["stale"] = True
                    else:
                        node.pop("stale", None)
                    self._nodes[prefix] = node
                    restored += 1
                    if self.disk is not None:
                        try:
                            self.disk.forget_node(twin)
                        except Exception:
                            log.exception("superseded disk removal "
                                          "failed (prefix %02x) - RAM "
                                          "stays the truth", twin)
                    log.info("name superseded at boot refill: "
                             "%02x -> %02x", twin, prefix)
                    continue
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
                # VECTORED SYNC: the phone must learn the deletion
                # (queued on disk; consumed by the next vectored ask).
                if self.disk is not None:
                    try:
                        self.disk.remember_gone(prefix)
                    except Exception:
                        log.exception("gone queue failed for pruned "
                                      "prefix %02x", prefix)
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

    def node_with_name(self, name: str, *,
                       exclude_prefix: Optional[int] = None,
                       ) -> Optional[int]:
        """Find a DIFFERENT known identity holding this exact name -
        one physical node under two radio prefixes (names are what the
        operator sees and trusts: one name = one dot). Returns the OLD
        prefix (the row to retire), or None. Name matching is exact:
        the name travels verbatim in the advert payload."""
        for prefix, node in self._nodes.items():
            if prefix == exclude_prefix:
                continue
            if node.get("name") == name:
                return prefix
        return None

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

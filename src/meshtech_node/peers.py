"""Peer table + owner election for multi-host scope feeds (v1.1).

Every host overhears every peer's broadcasts (shared channel + shared
secret = listening is free, zero extra airtime). LAYOUT doubles as the
discovery beacon: origin, name, grid geometry, last heard.

Owner election (who answers a client's REFRESH_REQ) is a pure function
of what each host knows - no negotiation chatter, no boundary
re-drawing (the classic thrash trap). Rules:

- a host owns a section iff its own area contains it
- if peer areas also contain it, the LOWEST origin prefix wins (a
  deterministic total order every host computes identically)
- a host that has heard NO peers answers as before (single-host mode
  is just the degenerate case)
- unknown/stale peers never win an election; a stale owner simply
  means nobody answers until a fresh host claims it
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional

from . import codec
from .grid import GridGeometry

log = logging.getLogger("meshtech-scope.peers")


@dataclass
class Peer:
    origin: int
    name: str
    layout: codec.Layout
    last_heard: float
    seq: int


class PeerTable:
    """Peers learned from overhearing, with expiry."""

    def __init__(self, *, expire_after_seconds: float = 1800.0):
        self.expire_after_seconds = expire_after_seconds
        self._peers: Dict[int, Peer] = {}

    def observe_layout(self, pkt: codec.Layout, *, now: Optional[float] = None,
                       ) -> Peer:
        now = time.time() if now is None else now
        if pkt.origin == 0:
            raise ValueError("LAYOUT without origin is not a peer beacon")
        peer = Peer(origin=pkt.origin, name=pkt.name or "",
                    layout=pkt, last_heard=now, seq=pkt.seq)
        self._peers[pkt.origin] = peer
        return peer

    def prune(self, *, now: Optional[float] = None) -> List[Peer]:
        """Drop expired peers; returns those dropped (for logging)."""
        now = time.time() if now is None else now
        expired = [p for p in self._peers.values()
                   if now - p.last_heard > self.expire_after_seconds]
        for peer in expired:
            del self._peers[peer.origin]
            log.info("Peer %04x (%s) expired - last heard %.0fs ago",
                     peer.origin, peer.name or "?", now - peer.last_heard)
        return expired

    def peers(self, *, now: Optional[float] = None) -> List[Peer]:
        self.prune(now=now)
        return sorted(self._peers.values(), key=lambda p: p.origin)

    def count(self, *, now: Optional[float] = None) -> int:
        self.prune(now=now)
        return len(self._peers)

    def get(self, origin: int, *, now: Optional[float] = None) -> Optional[Peer]:
        self.prune(now=now)
        return self._peers.get(origin)


def _geometry_of(layout: codec.Layout) -> GridGeometry:
    # rows=0 (a legacy LAYOUT) makes the geometry square again
    # (grid x grid) - GridGeometry's own rule.
    return GridGeometry(grid=layout.grid,
                        rows=layout.rows,
                        center_lat=layout.center_lat,
                        center_lon=layout.center_lon,
                        span_m=int(layout.span_m))


def _covering(geometries: Dict[int, GridGeometry], section_id: int,
              ref_geometry: GridGeometry) -> List[int]:
    """Origins whose AREA contains the section's centre (lowest first).

    Containment is physical: section ids are relative to each host's
    own LAYOUT centre, so the section's centre is located in the
    ASKING host's grid (ref_geometry) and each candidate covers it
    iff that point falls inside the candidate's area square. That is
    what makes election work across hosts whose areas only partially
    overlap.
    """
    try:
        section = ref_geometry.section(section_id)
    except ValueError:
        return []  # section id outside the asking host's grid
    centre_lat = (section.north + section.south) / 2.0
    centre_lon = (section.west + section.east) / 2.0
    out = [origin for origin, geometry in geometries.items()
           if geometry.section_for(centre_lat, centre_lon) != -1]
    out.sort()
    return out


class SectionOwners:
    """Deterministic owner election over self + peer areas."""

    def __init__(self, self_origin: int, self_geometry: GridGeometry,
                 peers: PeerTable):
        self.self_origin = self_origin
        self.self_geometry = self_geometry
        self.peers = peers

    def owner_of(self, section_id: int, *, now: Optional[float] = None,
                 ) -> Optional[int]:
        """The single elected owner, or None when nobody covers it.

        'Nobody' can mean genuinely uncovered, or the owner went
        stale - the client's refresh retry covers both.
        """
        geometries: Dict[int, GridGeometry] = {
            self.self_origin: self.self_geometry,
        }
        for peer in self.peers.peers(now=now):
            geometries.setdefault(peer.origin, _geometry_of(peer.layout))
        candidates = _covering(geometries, section_id, self.self_geometry)
        return candidates[0] if candidates else None

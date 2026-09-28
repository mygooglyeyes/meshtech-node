"""MESH CLINIC - peer attribution (CLINIC-WIRE.md record kind 4).

ONE place where what OTHER boxes said is folded in - tagged with
which box said it. A box hears the data bursts of its peers over the
air (PULSE, SECT_SUM, ROUTE, INTRO) and keeps those facts as
SECOND-HAND: source = the peer's origin, never ours, never merged
into first-hand charts and never re-worded as first-hand.

What is NOT folded (counted out loud, dropped):
  - origin 0 (anonymous): no box to attribute to - never invented.
  - my own echoes (origin = mine): not a peer.
  - CLINIC packets from other boxes (Phase 1: the four burst types).

Anti-stomping rules are untouched - this book is passive listening.
Isolation baseline: zero peers = an empty book = a complete clinic
from first-hand facts alone. Peer reports only ADD.
"""

import logging
import time
from typing import Dict, List, Optional, Tuple

from .codec import (CLINIC_MAX_NAME, REPORT_INTRO, REPORT_PULSE,
                    REPORT_ROUTE, REPORT_SECT_SUM, ClinicPeerFact,
                    Intro, Pulse, Route, SectSum, _age_minutes)

log = logging.getLogger(__name__)

# Bounded like every table here (size cap, oldest out first): a
# time-based fade law for peer words is Brett's call, not a guess.
PEER_MAX_ROWS = 5000


class PeerFacts:
    """What the peer boxes said, keyed by (source, report, subject)."""

    def __init__(self, origin: int = 0, *,
                 disk: Optional[object] = None) -> None:
        self.origin = int(origin) & 0xFFFF
        self.disk = disk
        self.entries: Dict[Tuple[int, int, int, Tuple[int, ...]],
                           Dict[str, object]] = {}
        # Counted out loud: the silent classes must stay visible.
        self.own_echo = 0
        self.anonymous = 0
        self.unreportable = 0
        self._peer_writes = 0

    # ------------------------------------------------------------ input

    def observe(self, obj: object, *, now: Optional[float] = None
                ) -> bool:
        """Fold one heard data burst from a peer. Returns False when
        the packet was dropped (own echo / anonymous) - counted, never
        silently gone."""
        now = time.time() if now is None else now
        origin = int(getattr(obj, "origin", 0) or 0) & 0xFFFF
        if origin == 0:
            self.anonymous += 1
            log.debug("peer burst %s dropped - anonymous origin (%d "
                      "so far)", type(obj).__name__, self.anonymous)
            return False
        if origin == self.origin:
            self.own_echo += 1
            log.debug("peer burst %s dropped - own echo (%d so far)",
                      type(obj).__name__, self.own_echo)
            return False
        if isinstance(obj, Pulse):
            self._fold((origin, REPORT_PULSE, 0, ()), origin, REPORT_PULSE,
                       0, now, values=(int(obj.uptime_min),
                                       int(obj.rx_per_hour),
                                       int(obj.active_total),
                                       int(obj.feed_airtime_s_per_h)))
        elif isinstance(obj, SectSum):
            self._fold((origin, REPORT_SECT_SUM, int(obj.section_id), ()),
                       origin, REPORT_SECT_SUM, int(obj.section_id), now,
                       values=(int(obj.active_nodes), int(obj.packet_count),
                               int(obj.delay_p50_s), int(obj.delay_p90_s)))
        elif isinstance(obj, Route):
            path = tuple(int(p) & 0xFF for p in obj.prefixes)
            if not 1 <= len(path) <= 8:
                # The wire trail is 1..8: a shape it cannot carry is a
                # loud gap, never a truncated path pretending to be it.
                self.unreportable += 1
                log.warning("peer ROUTE from %04x carries %d prefixes - "
                            "not foldable (wire trail is 1..8); "
                            "counted, dropped", origin, len(path))
                return False
            self._fold((origin, REPORT_ROUTE, int(obj.route_id), path),
                       origin, REPORT_ROUTE, int(obj.route_id), now,
                       values=(int(obj.packet_count), int(obj.delay_med_s),
                               int(obj.last_heard_min), 0), path=path)
        elif isinstance(obj, Intro):
            for entry in obj.entries:
                prefix = int(entry.prefix) & 0xFF
                self._fold((origin, REPORT_INTRO, prefix, ()), origin,
                           REPORT_INTRO, prefix, now,
                           cls=int(entry.node_class) & 0xFF,
                           lat=entry.lat, lon=entry.lon,
                           name=entry.name or "")
        else:
            return False
        return True

    def _fold(self, key: Tuple[int, int, int, Tuple[int, ...]], source: int,
              report: int, subject: int, now: float, *,
              values: Tuple[int, int, int, int] = (0, 0, 0, 0),
              path: Tuple[int, ...] = (), cls: int = 0,
              lat: Optional[float] = None, lon: Optional[float] = None,
              name: str = "") -> None:
        entry = self.entries.get(key)
        if entry is None:
            entry = self.entries[key] = {"first": now, "last": now,
                                         "warned": False}
        entry["last"] = max(float(entry["last"]), now)
        entry["values"] = tuple(int(v) for v in values)
        entry["path"] = path
        entry["cls"] = cls
        entry["lat"] = lat
        entry["lon"] = lon
        entry["name"] = name
        self._disk_write(key, entry)

    def _disk_write(self, key: Tuple[int, int, int, Tuple[int, ...]],
                    entry: Dict[str, object]) -> None:
        if self.disk is None:
            return
        try:
            self.disk.upsert_peer_report({
                "source": key[0], "report": key[1], "subject": key[2],
                "path_hex": bytes(key[3]).hex(),
                "first": float(entry["first"]), "last": float(entry["last"]),
                "values": entry.get("values") or (0, 0, 0, 0),
                "cls": int(entry.get("cls") or 0),
                "lat": entry.get("lat"), "lon": entry.get("lon"),
                "name": entry.get("name") or "",
            })
            self._peer_writes += 1
            if self._peer_writes % 256 == 0:
                self.disk.trim_peer_reports(PEER_MAX_ROWS)
        except Exception:
            log.exception("peer report disk write-through failed - RAM "
                          "keeps the truth")

    # ------------------------------------------------------- derivations

    def records(self, now: float) -> List[ClinicPeerFact]:
        """Peer reports as second-hand records (source = the peer).
        A name the wire cannot carry refuses just the name-bearing
        record loudly - never a truncated name."""
        out: List[ClinicPeerFact] = []
        for key in sorted(self.entries):
            source, report, subject, path = key
            entry = self.entries[key]
            values = tuple(int(v) for v in entry.get("values")
                           or (0, 0, 0, 0))
            name = str(entry.get("name") or "")
            if report == REPORT_INTRO and \
                    len(name.encode("utf-8")) > CLINIC_MAX_NAME:
                if not entry.get("warned"):
                    entry["warned"] = True
                    log.warning("peer intro name for %02x too long for "
                                "the clinic wire (%d B > %d) - the "
                                "record is not minted, never truncated",
                                subject, len(name.encode("utf-8")),
                                CLINIC_MAX_NAME)
                continue
            out.append(ClinicPeerFact(
                source=source,
                report=report,
                subject=subject,
                heard_age_min=_age_minutes(max(0.0, now
                                               - float(entry.get("last")
                                                       or 0.0))),
                values=(values + (0, 0, 0, 0))[:4],
                path=tuple(path),
                cls=int(entry.get("cls") or 0),
                lat=entry.get("lat"),
                lon=entry.get("lon"),
                name=name,
            ))
        return out

    # -------------------------------------------------------- persistence

    def refill(self, rows: List[dict]) -> int:
        restored = 0
        for row in rows or []:
            try:
                source = int(row.get("source") or 0)
                report = int(row.get("report") or 0)
                subject = int(row.get("subject") or 0)
                path = tuple(bytes.fromhex(str(row.get("path_hex") or "")))
                values_raw = row.get("values") or (0, 0, 0, 0)
                values = tuple(int(v) for v in values_raw)[:4]
                values = (values + (0, 0, 0, 0))[:4]
                entry = {
                    "first": float(row.get("first") or 0.0),
                    "last": float(row.get("last") or 0.0),
                    "values": values, "path": path,
                    "cls": int(row.get("cls") or 0),
                    "lat": row.get("lat"), "lon": row.get("lon"),
                    "name": str(row.get("name") or ""),
                    "warned": False,
                }
            except (TypeError, ValueError):
                log.exception("peer report row unreadable - skipped")
                continue
            key = (source, report, subject, path)
            if key in self.entries:
                continue                # RAM wins where both exist
            self.entries[key] = entry
            restored += 1
        if restored:
            log.info("peer reports refilled from disk: %d report(s) "
                     "restored (book now %d)", restored, len(self.entries))
        return restored

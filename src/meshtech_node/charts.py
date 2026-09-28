"""MESH CLINIC - chart derivations (CLINIC-WIRE.md record kinds 1-2).

ONE place where what the store holds becomes the clinic's charts:
per-node facts (availability strip, signal stats as THIS box heard
them, hop histogram, 24 h traffic share, ages) and per-route facts
(usage, direct vs multi-hop, delay spread, ages).

Derivations only. No wire bytes (codec owns those), no verdicts
(flags.py owns those), no peer words (peerfacts.py owns those).
Nothing here ever pins or fabricates: a value the wire cannot carry
is returned out of range and the batch builder refuses to mint the
record loudly. Missing numbers stay missing (codec sentinels).
"""

import logging
import math
import struct
import time
from typing import Dict, List, Optional, Tuple

from .codec import (SHARE_UNKNOWN_PCT, SIGNAL_SD_UNKNOWN, SIGNAL_UNKNOWN,
                    ClinicNodeFact, ClinicRouteFact, _age_minutes)

log = logging.getLogger(__name__)

# Signal smoothing rate: the openhop_repeater neighbour tracker's
# proven EWMA alpha (neighbour_links.py). Signal is never distance.
EWMA_ALPHA = 0.20

# A chart lives exactly as long as its node's own forget law (the node
# table: 14 d silent -> stale, 30 d -> forgotten). Charts are removed
# only when the node is dead or gone - never earlier.
FORGET_AFTER_S = 30.0 * 86400.0

HOP_BUCKETS = 16          # hop counts 0..15 (0 = unknown / never minted)
STRIP_HOURS = 24


class SigStat:
    """One signal kind's facts: EWMA + best/worst + Welford variance.

    n = 0 means the box has NO reading for this kind (radio reported
    nothing) - the wire fields then carry codec sentinels, never a
    plausible constant. sd exists only from 2+ samples.
    """

    def __init__(self) -> None:
        self.n = 0
        self.ewma = 0.0
        self.best: Optional[float] = None
        self.worst: Optional[float] = None
        self.mean = 0.0
        self.m2 = 0.0

    def observe(self, value: float) -> None:
        value = float(value)
        self.n += 1
        self.ewma = value if self.n == 1 else \
            self.ewma + EWMA_ALPHA * (value - self.ewma)
        self.best = value if self.best is None else max(self.best, value)
        self.worst = value if self.worst is None else min(self.worst, value)
        delta = value - self.mean
        self.mean += delta / self.n
        self.m2 += delta * (value - self.mean)

    @property
    def sd(self) -> Optional[float]:
        if self.n < 2:
            return None
        return math.sqrt(self.m2 / (self.n - 1))

    def to_row(self, prefix: str) -> Dict[str, object]:
        return {
            f"{prefix}_n": self.n,
            f"{prefix}_ewma": self.ewma,
            f"{prefix}_best": self.best,
            f"{prefix}_worst": self.worst,
            f"{prefix}_mean": self.mean,
            f"{prefix}_m2": self.m2,
        }

    @classmethod
    def from_row(cls, row: Dict[str, object], prefix: str) -> "SigStat":
        stat = cls()
        stat.n = int(row.get(f"{prefix}_n") or 0)
        stat.ewma = float(row.get(f"{prefix}_ewma") or 0.0)
        best = row.get(f"{prefix}_best")
        worst = row.get(f"{prefix}_worst")
        stat.best = None if best is None else float(best)
        stat.worst = None if worst is None else float(worst)
        stat.mean = float(row.get(f"{prefix}_mean") or 0.0)
        stat.m2 = float(row.get(f"{prefix}_m2") or 0.0)
        return stat


class NodeChart:
    """One identified node's chart, as THIS box heard it."""

    def __init__(self, prefix: int, ts: float) -> None:
        self.prefix = int(prefix) & 0xFF
        self.first = float(ts)
        self.last = float(ts)
        self.anchor_hour = int(ts // 3600)
        self.buckets = [0] * STRIP_HOURS   # per-hour hear counts
        self.hops = [0] * HOP_BUCKETS      # hop count histogram (1..15)
        self.sig = {"rssi": SigStat(), "snr": SigStat()}

    # ------------------------------------------------------------ input

    def observe(self, obs: object) -> None:
        ts = float(getattr(obs, "recv_ts", 0.0) or 0.0)
        if ts <= 0.0:
            ts = time.time()
        self.first = min(self.first, ts)
        self.last = max(self.last, ts)
        self._bump(ts)
        # typical radio hops = repeater trail length + 1
        hop = len(getattr(obs, "path_prefixes", None) or []) + 1
        if hop >= HOP_BUCKETS:
            hop = HOP_BUCKETS - 1   # ruler end: "this far or farther"
        self.hops[hop] += 1
        for kind in ("rssi", "snr"):
            value = getattr(obs, kind, None)
            if value is not None:
                self.sig[kind].observe(float(value))

    def _bump(self, ts: float) -> None:
        """Count one hear in its hourly slot (bit 0 = oldest hour)."""
        hour = int(ts // 3600)
        self._roll(hour)
        idx = STRIP_HOURS - 1 - (self.anchor_hour - hour)
        if 0 <= idx < STRIP_HOURS:
            self.buckets[idx] += 1

    def _roll(self, hour: int) -> None:
        """Shift the strip so the newest slot is `hour` (zeros slide
        in for unheard hours - an honest gap, never a guessed count)."""
        if hour <= self.anchor_hour:
            return
        shift = hour - self.anchor_hour
        if shift >= STRIP_HOURS:
            self.buckets = [0] * STRIP_HOURS
        else:
            self.buckets = self.buckets[shift:] + [0] * shift
        self.anchor_hour = hour

    # ------------------------------------------------------- derivations

    def strip(self, now: float) -> int:
        """24-bit availability: bit i set = heard at least once in the
        hour that is (23 - i) hours before the current one."""
        self._roll(int(now // 3600))
        return sum(1 << i for i, count in enumerate(self.buckets) if count)

    def count24(self, now: float) -> int:
        self._roll(int(now // 3600))
        return sum(self.buckets)

    def hops_typ(self) -> int:
        """The most common radio hop count (ties -> the smaller); 0 =
        unknown only when no sample ever carried a path."""
        best_hop, best_count = 0, 0
        for hop in range(1, HOP_BUCKETS):
            if self.hops[hop] > best_count:
                best_hop, best_count = hop, self.hops[hop]
        return best_hop

    def _signal_wire(self, kind: str) -> Tuple[int, int, int, int]:
        """(ewma, best, worst, sd) on the wire's rulers - SNR in
        quarter-dB, RSSI in dBm, sd in the same units. No clamping:
        a value past the ruler refuses the record downstream instead
        of pinning a fake. n = 0 -> sentinels; sd needs 2+ samples."""
        stat = self.sig[kind]
        scale = 4.0 if kind == "snr" else 1.0
        if stat.n == 0:
            return (SIGNAL_UNKNOWN, SIGNAL_UNKNOWN, SIGNAL_UNKNOWN,
                    SIGNAL_SD_UNKNOWN)
        sd = stat.sd
        sd_wire = SIGNAL_SD_UNKNOWN if sd is None else int(round(sd * scale))
        return (int(round(stat.ewma * scale)),
                int(round(stat.best * scale)),
                int(round(stat.worst * scale)),
                sd_wire)

    def fact(self, *, source: int, now: float,
             share_pct: int) -> ClinicNodeFact:
        snr = self._signal_wire("snr")
        rssi = self._signal_wire("rssi")
        return ClinicNodeFact(
            source=int(source) & 0xFFFF,
            prefix=self.prefix,
            last_age_min=_age_minutes(max(0.0, now - self.last)),
            age_days=min(0xFFFF, int(max(0.0, now - self.first)
                                     // 86400)),
            strip=self.strip(now),
            hops_typ=self.hops_typ(),
            share_pct=int(share_pct) & 0xFF,
            snr_ewma=snr[0], snr_best=snr[1], snr_worst=snr[2], snr_sd=snr[3],
            rssi_ewma=rssi[0], rssi_best=rssi[1], rssi_worst=rssi[2],
            rssi_sd=rssi[3],
        )

    # -------------------------------------------------------- persistence

    def to_row(self) -> Dict[str, object]:
        row: Dict[str, object] = {
            "prefix": self.prefix,
            "first_heard": self.first,
            "last_heard": self.last,
            "anchor_hour": self.anchor_hour,
            "buckets": struct.pack(f"<{STRIP_HOURS}I", *self.buckets),
            "hops": struct.pack(f"<{HOP_BUCKETS}I", *self.hops),
        }
        row.update(self.sig["rssi"].to_row("rssi"))
        row.update(self.sig["snr"].to_row("snr"))
        return row

    @classmethod
    def from_row(cls, row: Dict[str, object]) -> "NodeChart":
        chart = cls(int(row.get("prefix") or 0),
                    float(row.get("first_heard") or 0.0))
        chart.first = float(row.get("first_heard") or 0.0)
        chart.last = float(row.get("last_heard") or 0.0)
        chart.anchor_hour = int(row.get("anchor_hour") or 0)
        try:
            buckets = struct.unpack(f"<{STRIP_HOURS}I",
                                    bytes(row.get("buckets") or b"")[:96])
            chart.buckets = list(buckets)
        except struct.error:
            pass                      # corrupt row: keep zeros, honest
        try:
            hops = struct.unpack(f"<{HOP_BUCKETS}I",
                                 bytes(row.get("hops") or b"")[:64])
            chart.hops = list(hops)
        except struct.error:
            pass
        chart.sig["rssi"] = SigStat.from_row(row, "rssi")
        chart.sig["snr"] = SigStat.from_row(row, "snr")
        return chart


def route_fact(path: Tuple[int, ...], entry: Dict[str, object], *,
               source: int, now: float) -> Optional[ClinicRouteFact]:
    """One route's fact record (CLINIC-WIRE.md kind 2).

    Delay facts come only from HONEST non-negative samples: a negative
    "delay" is clock skew, not a delay fact, and never rides the wire
    (0 = unknown there). The store's own delay bookkeeping is untouched.
    """
    path = tuple(int(p) & 0xFF for p in path)
    if not 1 <= len(path) <= 8:
        return None                    # the wire trail is 1..8 bytes
    valid = sorted(float(d) for d in entry.get("delays") or []
                   if float(d) >= 0.0)
    med = int(round(valid[len(valid) // 2])) if valid else 0
    dmin = int(round(float(entry.get("dmin") or 0.0)))
    dmax = int(round(float(entry.get("dmax") or 0.0)))
    return ClinicRouteFact(
        source=int(source) & 0xFFFF,
        path=path,
        uses=min(0xFFFF, max(0, int(entry.get("count") or 0))),
        direct=1 if entry.get("direct") else 0,
        delay_min_s=_cap_u16(dmin),
        delay_med_s=_cap_u16(med),
        delay_max_s=_cap_u16(dmax),
        last_age_min=_age_minutes(max(0.0, now - float(entry.get("last")
                                                       or 0.0))),
        age_days=min(0xFFFF, int(max(0.0, now - float(entry.get("first")
                                                      or 0.0)) // 86400)),
    )


def _cap_u16(value: int) -> int:
    """The u16 ruler's end = 'this much or more' - the wire counts no
    higher, the true value stays in the store."""
    return min(0xFFFF, max(0, int(value)))


class NodeChartBook:
    """Every chart this box holds, with its disk write-through.

    Ingestion is ONE call (observe) from the store's ingest seam; the
    book never sees raw packets, only Observations. Second-hand facts
    (peer reports) never land here - this book is first-hand only.
    """

    def __init__(self, origin: int = 0, *, disk: Optional[object] = None
                 ) -> None:
        self.origin = int(origin) & 0xFFFF
        self.charts: Dict[int, NodeChart] = {}
        self.disk = disk

    def observe(self, obs: object) -> None:
        """Fold one identified observation into its node's chart."""
        prefix = int(getattr(obs, "prefix", 0) or 0) & 0xFF
        if not prefix:
            return                     # anonymous traffic: no identity
        ts = float(getattr(obs, "recv_ts", 0.0) or time.time())
        chart = self.charts.get(prefix)
        if chart is None:
            chart = self.charts[prefix] = NodeChart(prefix, ts)
        chart.observe(obs)
        self._disk_write(chart)

    def _disk_write(self, chart: NodeChart) -> None:
        if self.disk is None:
            return
        try:
            self.disk.upsert_clinic_node(chart.to_row())
        except Exception:
            log.exception("clinic chart disk write-through failed (node "
                          "%02x) - RAM keeps the truth", chart.prefix)

    def records(self, store: object, now: float) -> List[object]:
        """Node facts for every chart + route facts for every route the
        store holds (fresh AND stale; dead routes are already gone)."""
        total = sum(chart.count24(now) for chart in self.charts.values())
        out: List[object] = []
        for prefix in sorted(self.charts):
            chart = self.charts[prefix]
            count = chart.count24(now)
            share = SHARE_UNKNOWN_PCT if not total else \
                min(0xFF, int(round(count * 100.0 / total)))
            out.append(chart.fact(source=self.origin, now=now,
                                  share_pct=share))
        routes = getattr(store, "routes_all", None)
        for path, entry in (routes() if routes else []):
            fact = route_fact(tuple(path), entry, source=self.origin, now=now)
            if fact is not None:
                out.append(fact)
        return out

    def refill(self, rows: List[dict]) -> int:
        """Boot refill: disk -> RAM (RAM wins where both exist)."""
        restored = 0
        for row in rows or []:
            try:
                chart = NodeChart.from_row(row)
            except Exception:
                log.exception("clinic chart row unreadable - skipped")
                continue
            if chart.prefix in self.charts:
                continue
            self.charts[chart.prefix] = chart
            restored += 1
        if restored:
            log.info("clinic charts refilled from disk: %d chart(s) "
                     "restored (book now %d)", restored, len(self.charts))
        return restored

    def prune(self, now: float) -> int:
        """Charts die only with their node: 30 d silent = gone (the
        node table's forget law), mirrored to disk."""
        cutoff = now - FORGET_AFTER_S
        gone = [p for p, c in self.charts.items() if c.last < cutoff]
        for prefix in gone:
            del self.charts[prefix]
        if self.disk is not None:
            try:
                self.disk.forget_clinic_nodes_before(cutoff)
            except Exception:
                log.exception("clinic chart disk prune failed - RAM "
                              "stays the truth")
        return len(gone)

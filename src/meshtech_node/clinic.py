"""MESH CLINIC - observation ingestion (CLINIC-WIRE.md, Phase 1).

ONE place where an Observation fans out to the clinic books. The
RollingStore's ingest seam calls observe() for every packet the store
keeps (service.py wires it once); the raw radio pipeline calls
note_frame()/note_sig_fail() for every frame heard. Nothing else
feeds the clinic - a second ingestion path would fork the truth.

The books it fans out to:
  charts.py   - per-node / per-route derivations (first-hand facts)
  flags.py    - trouble flags (facts with evidence, never verdicts)
"""

import logging
from typing import List

log = logging.getLogger(__name__)


class ClinicIngest:
    """The clinic's single ingestion choke point."""

    def __init__(self, charts: object, trouble: object,
                 health: object = None) -> None:
        self.charts = charts
        self.trouble = trouble
        # THE HEALTH BOOK (HEALTH-DEFINITIONS.md, Brett 2026-09-29):
        # duplicates, airtime, seq gaps, flaps, collisions - minted
        # only from what the radio pipeline proves.
        self.health = health

    def observe(self, obs: object) -> None:
        """One Observation the store is keeping: fold it into the
        node's chart and let the flags look at it. Never raises into
        the ingest chain (the store's own tolerance rule)."""
        try:
            self.charts.observe(obs)
            self.trouble.observe(obs)
        except Exception:
            log.exception("clinic ingest raised - the store keeps the "
                          "truth")

    def note_frame(self, corrupt: bool) -> None:
        """One frame from the radio pipeline (every frame heard)."""
        try:
            self.trouble.note_frame(corrupt)
        except Exception:
            log.exception("clinic frame note raised - the listener "
                          "continues")

    def note_sig_fail(self, claimed_prefix: int) -> None:
        """One advert whose signature check failed (the gate's call)."""
        try:
            self.trouble.note_sig_fail(claimed_prefix)
        except Exception:
            log.exception("clinic sig-fail note raised - the listener "
                          "continues")

    # ---- the health book's feed (the SAME single choke point) ----

    def note_air(self, frame_bytes: int, *, now: float = 0.0) -> None:
        """One frame heard: the occupancy airtime's raw material."""
        if self.health is None:
            return
        try:
            self.health.note_frame(frame_bytes, now=now or None)
        except Exception:
            log.exception("health air note raised - listener continues")

    def note_dup(self, payload: bytes = b"", *, now: float = 0.0) -> None:
        """One extra copy of an already-heard flood packet."""
        if self.health is None:
            return
        try:
            self.health.note_dup(payload=payload or None, now=now or None)
        except Exception:
            log.exception("health dup note raised - listener continues")

    def note_scope(self, sender: int, seq: int, *, payload: bytes = b"",
                   now: float = 0.0) -> None:
        """One identified scope packet (loss/reorder/flaps material)."""
        if self.health is None:
            return
        try:
            self.health.note_scope(sender, seq,
                                   payload=payload or None, now=now or None)
        except Exception:
            log.exception("health scope note raised - listener continues")

    def note_collision(self, tag: bytes, key_a: bytes, key_b: bytes) -> None:
        """One proven tag collision (one short tag, two keys)."""
        if self.health is None:
            return
        try:
            self.health.note_collision(tag, key_a, key_b)
        except Exception:
            log.exception("health collision note raised - listener "
                          "continues")

    def prune(self, now: float) -> int:
        """The rare-cadence mirror of the removal ages (CLINIC-WIRE.md):
        charts die only with their node (30 d), flags expire 30 d
        after their most recent event. Returns facts dropped."""
        try:
            return self.charts.prune(now) + self.trouble.prune(now)
        except Exception:
            log.exception("clinic prune failed - the books keep the "
                          "truth")
            return 0

    def refill(self, disk: object) -> int:
        """Boot refill: every clinic store back into RAM (restarts
        lose nothing). RAM wins where both exist."""
        total = 0
        total += self.charts.refill(disk.clinic_node_rows())
        total += self.trouble.refill(disk.clinic_flag_rows())
        return total

    def records(self, store: object, now: float) -> List[object]:
        """Every first-hand clinic record this box can mint."""
        out = self.charts.records(store, now) + self.trouble.records(now)
        if self.health is not None:
            out += self.health.records(now)
        return out

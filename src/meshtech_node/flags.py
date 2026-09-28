"""MESH CLINIC - trouble flags (CLINIC-WIRE.md record kind 3).

ONE place where trouble is MEASURED. A flag is a fact with evidence,
never a verdict: it never says who is "bad", only what was measured.
Corrupt packets are mesh-level noise (flag 4, subject ALWAYS 0) and
are never blamed on a sender.

Triggers (the wire page's table, constants named there):

  1 FLAG_SIG_FAIL      every advert whose Ed25519 check failed
                       ("broken node OR impersonation - the flag does
                       not pick"); subject = the CLAIMED key prefix.
  2 FLAG_TS_BACKWARDS  a verified advert's timestamp went backwards
                       more than TS_BACK_JUMP_S behind the best seen
                       (replay, reset, or drift); detail = worst jump.
  3 FLAG_RATE_STORM    >= RATE_STORM_MIN identity-bearing packets in
                       RATE_WINDOW_S from one key (far faster than
                       advert cadence); detail = peak packets/min.
  4 FLAG_CORRUPT_SHARE >= CORRUPT_MIN_SHARE of heard traffic corrupt
                       over 10 min (>= CORRUPT_MIN_PACKETS frames in
                       the window); detail = share per-mille. Subject
                       0 ALWAYS - never blamed on a sender.

Window state (deques, best-stamp map) is process memory - honest, and
the minted flag rows themselves survive restarts via the disk store.
"""

import logging
import time
from collections import deque
from typing import Deque, Dict, List, Optional, Tuple

from .codec import (FLAG_CORRUPT_SHARE, FLAG_RATE_STORM, FLAG_SIG_FAIL,
                    FLAG_TS_BACKWARDS, ClinicFlagFact, _age_minutes)

log = logging.getLogger(__name__)

# Removal age (CLINIC-WIRE.md): a flag expires 30 days after its
# most recent event - evidence goes stale and stops being shown.
FORGET_AFTER_S = 30.0 * 86400.0

RATE_WINDOW_S = 60.0            # the rate-storm window
RATE_STORM_MIN = 20             # identity-bearing packets inside it
TS_BACK_JUMP_S = 300.0          # how far back counts "backwards"
CORRUPT_WINDOW_S = 600.0        # the corrupt-share window (10 min)
CORRUPT_MIN_SHARE = 0.10        # 10% corrupt to flag
CORRUPT_MIN_PACKETS = 20        # ...but only with real traffic in view
REPEAT_LIMIT_S = 60.0           # one re-mint per flag per minute


class FlagState:
    """One (flag, subject) row: what was measured, and when."""

    def __init__(self, first: float) -> None:
        self.events = 0
        self.first = first
        self.last = first
        self.detail = 0
        self.last_mint = 0.0


class TroubleFlags:
    """The clinic's flag book (first-hand: what THIS box measured)."""

    def __init__(self, origin: int = 0, *,
                 disk: Optional[object] = None) -> None:
        self.origin = int(origin) & 0xFFFF
        self.disk = disk
        self.flags: Dict[Tuple[int, int], FlagState] = {}
        # rate storms: subject -> recv stamps inside the window
        self._rate: Dict[int, Deque[float]] = {}
        # ts-backwards: subject -> best (newest) verified advert stamp
        self._best_ts: Dict[int, float] = {}
        # corrupt share: (ts, corrupt) for every frame inside the window
        self._frames: Deque[Tuple[float, bool]] = deque()

    # ------------------------------------------------------------ input

    def observe(self, obs: object) -> None:
        """One IDENTIFIED observation (the ingest seam calls this for
        every packet the store keeps). Anonymous traffic cannot be
        attributed to a key and is never flagged."""
        prefix = int(getattr(obs, "prefix", 0) or 0) & 0xFF
        if not prefix:
            return
        now = float(getattr(obs, "recv_ts", 0.0) or time.time())
        self._check_rate_storm(prefix, now)
        self._check_ts_backwards(prefix, obs, now)

    def note_frame(self, corrupt: bool, *, now: Optional[float] = None
                   ) -> None:
        """One heard frame from the radio pipeline (every frame, clean
        or corrupt) - the corrupt-share window's raw material."""
        now = time.time() if now is None else now
        self._frames.append((float(now), bool(corrupt)))
        self._trim_frames(now)
        if len(self._frames) < CORRUPT_MIN_PACKETS:
            return
        corrupt_n = sum(1 for _ts, bad in self._frames if bad)
        if corrupt_n < CORRUPT_MIN_SHARE * len(self._frames):
            return
        detail = int(round(corrupt_n * 1000.0 / len(self._frames)))
        self._mint(FLAG_CORRUPT_SHARE, 0, detail, now)

    def note_sig_fail(self, claimed_prefix: int, *,
                      now: Optional[float] = None) -> None:
        """One advert whose signature check FAILED. Every check counts
        (the wire page's trigger). The claimed prefix is evidence of
        what the bytes said - not proof of who sent them."""
        now = time.time() if now is None else now
        self._mint(FLAG_SIG_FAIL, int(claimed_prefix) & 0xFF, 0, now,
                   count_each=True)

    # ---------------------------------------------------------- triggers

    def _check_rate_storm(self, prefix: int, now: float) -> None:
        window = self._rate.setdefault(prefix, deque())
        window.append(now)
        while window and now - window[0] > RATE_WINDOW_S:
            window.popleft()
        if len(window) < RATE_STORM_MIN:
            return
        self._mint(FLAG_RATE_STORM, prefix, len(window), now)

    def _check_ts_backwards(self, prefix: int, obs: object, now: float
                            ) -> None:
        origin_ts = getattr(obs, "origin_ts", None)
        if origin_ts is None:
            return                     # no honest stamp: nothing to say
        stamp = float(origin_ts)
        best = self._best_ts.get(prefix)
        if best is not None and stamp < best - TS_BACK_JUMP_S:
            self._mint(FLAG_TS_BACKWARDS, prefix,
                       min(0xFFFF, int(round(best - stamp))), now)
        if best is None or stamp > best:
            self._best_ts[prefix] = stamp

    def _trim_frames(self, now: float) -> None:
        while self._frames and now - self._frames[0][0] > CORRUPT_WINDOW_S:
            self._frames.popleft()

    def _mint(self, flag: int, subject: int, detail: int,
              now: float, *, count_each: bool = False) -> None:
        key = (flag, subject)
        state = self.flags.get(key)
        if state is None:
            state = self.flags[key] = FlagState(now)
        elif not count_each and now - state.last_mint < REPEAT_LIMIT_S:
            return                     # windows overlap: one mint a minute
        state.events += 1
        state.last = now
        state.last_mint = now
        state.detail = max(state.detail, int(detail))
        if state.events == 1:
            log.info("TROUBLE FLAG %d on subject %d (detail %d) - "
                     "evidence only, never a verdict", flag, subject,
                     detail)
        else:
            log.debug("trouble flag %d subject %d again (events %d)",
                      flag, subject, state.events)
        if self.disk is not None:
            try:
                self.disk.upsert_clinic_flag({
                    "kind": flag, "subject": subject,
                    "events": state.events, "first": state.first,
                    "last": state.last, "detail": state.detail,
                })
            except Exception:
                log.exception("trouble flag disk write-through failed "
                              "- RAM keeps the truth")

    # ------------------------------------------------------- derivations

    def records(self, now: float) -> List[ClinicFlagFact]:
        out: List[ClinicFlagFact] = []
        for (flag, subject) in sorted(self.flags):
            state = self.flags[(flag, subject)]
            out.append(ClinicFlagFact(
                source=self.origin,
                flag=flag,
                subject=subject,
                events=min(0xFFFF, state.events),
                first_age_min=_age_minutes(max(0.0, now - state.first)),
                last_age_min=_age_minutes(max(0.0, now - state.last)),
                detail=min(0xFFFF, state.detail),
            ))
        return out

    def prune(self, now: float) -> int:
        """The stated removal age (CLINIC-WIRE.md): flags expire 30 d
        after their most recent event, mirrored to disk. Returns rows
        dropped."""
        cutoff = now - FORGET_AFTER_S
        gone = [key for key, state in self.flags.items()
                if state.last < cutoff]
        for key in gone:
            del self.flags[key]
        if self.disk is not None:
            try:
                self.disk.forget_clinic_flags_before(cutoff)
            except Exception:
                log.exception("trouble flag disk prune failed - RAM "
                              "stays the truth")
        return len(gone)

    # -------------------------------------------------------- persistence

    def refill(self, rows: List[dict]) -> int:
        restored = 0
        for row in rows or []:
            try:
                key = (int(row.get("kind") or 0), int(row.get("subject")
                                                      or 0))
                state = FlagState(float(row.get("first") or 0.0))
                state.events = int(row.get("events") or 0)
                state.last = float(row.get("last") or 0.0)
                state.detail = int(row.get("detail") or 0)
            except (TypeError, ValueError):
                log.exception("trouble flag row unreadable - skipped")
                continue
            if key in self.flags:
                continue                # RAM wins where both exist
            self.flags[key] = state
            restored += 1
        if restored:
            log.info("trouble flags refilled from disk: %d flag(s) "
                     "restored (book now %d)", restored, len(self.flags))
        return restored

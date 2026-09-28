"""Mesh Clinic v2 - trouble flags through the real surfaces.

Under test: flags.py (CLINIC-WIRE.md record kind 3), fed exactly the
way the service feeds it:
  - sig-fail + corrupt share via RawPacketSource.handle_packet (the
    radio pipeline's flag_sink), and
  - rate storms + timestamps-backwards via RollingStore.add()'s ONE
    fan-out hook with real Observation objects.

A flag is evidence, never a verdict; corrupt packets are mesh-level
noise and are NEVER blamed on a sender (flag 4's subject is always 0).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

pytest.importorskip("nacl", reason="the advert signature gate needs "
                                   "pynacl to say a real NO")

from meshtech_node.clinic import ClinicIngest  # noqa: E402
from meshtech_node.charts import NodeChartBook  # noqa: E402
from meshtech_node.codec import (  # noqa: E402
    FLAG_CORRUPT_SHARE, FLAG_RATE_STORM, FLAG_SIG_FAIL, FLAG_TS_BACKWARDS,
    ClinicFlagFact,
)
from meshtech_node.flags import TroubleFlags  # noqa: E402
from meshtech_node.observations import Observation, RollingStore  # noqa: E402
from meshtech_node.rawsource import RawPacketSource, RxPacket  # noqa: E402

PAYLOAD_TYPE_ADVERT = 0x04


def _frame(payload_type: int, payload: bytes) -> bytes:
    header = (0 << 6) | (payload_type << 2) | 0
    return bytes([header, 0x34, 0x12, 0x56, 0x78, 0x00]) + payload


def obs(**kw):
    base = dict(recv_ts=1000.0, origin_ts=None, prefix=0x21,
                lat=None, lon=None, path_prefixes=[])
    base.update(kw)
    return Observation(**base)


def make_clinic():
    charts = NodeChartBook(origin=0xB17E)
    trouble = TroubleFlags(origin=0xB17E)
    return ClinicIngest(charts, trouble), trouble


def flags_of(trouble, now):
    return {(f.flag, f.subject): f for f in trouble.records(now)}


# ------------------------------------------- sig-fail + corrupt (radio)

def test_sig_fail_counts_every_failed_check_claimed_prefix_only():
    """Bytes claiming a key failed the check - the flag names the
    CLAIMED prefix and never picks between broken node and
    impersonation. Every failed check is one event."""
    clinic, trouble = make_clinic()
    src = RawPacketSource(None, channels=[], flag_sink=clinic)
    garbage = bytes([0x42]) + b"\x11" * 31 + b"\x22" * 20   # not signed
    for _ in range(3):
        assert src.handle_packet(
            RxPacket(data=_frame(PAYLOAD_TYPE_ADVERT, garbage))) is None
    assert src.stats.corrupt == 3
    flags = flags_of(trouble, 1000.0)
    fact = flags[(FLAG_SIG_FAIL, 0x42)]
    assert fact.events == 3
    assert fact.source == 0xB17E


def test_corrupt_share_is_mesh_wide_never_blamed_on_a_sender():
    """3 corrupt of 20 frames = 15% >= 10% -> flag 4, subject ALWAYS
    0 (mesh-level noise). Clean frames are counted in the window."""
    clinic, trouble = make_clinic()
    src = RawPacketSource(None, channels=[], flag_sink=clinic)
    for _ in range(17):
        src.handle_packet(RxPacket(data=_frame(0x01, b"ok")))      # clean
    for _ in range(3):
        src.handle_packet(RxPacket(data=b"\x00\x00\x00"))          # malformed
    assert src.stats.malformed == 3
    flags = flags_of(trouble, 1000.0)
    fact = flags[(FLAG_CORRUPT_SHARE, 0)]
    assert fact.subject == 0                # NEVER a sender
    assert fact.events == 1
    assert 140 <= fact.detail <= 160         # ~150 per-mille


# ------------------------------- rate storms + timestamps (ingest seam)

def test_rate_storm_limited_to_one_mint_per_minute():
    """20 identity-bearing packets in 60 s from one key = the storm;
    the follow-on packets inside the minute do not re-mint."""
    clinic, trouble = make_clinic()
    store = RollingStore(window_seconds=3600.0)
    store.on_observed = clinic.observe      # the service's one wiring line
    for i in range(21):
        store.add(obs(recv_ts=1000.0 + i, origin_ts=1000.0 + i))
    flags = flags_of(trouble, 1061.0)
    fact = flags[(FLAG_RATE_STORM, 0x21)]
    assert fact.events == 1                 # one mint, not one per packet
    assert fact.detail == 20                # peak packets in the window


def test_ts_backwards_judged_against_best_stamp():
    """A verified advert stamp more than 300 s behind the best seen is
    a fact worth a look (replay, reset, or drift) - detail = the worst
    jump in seconds. Small wobble and unstamped packets say nothing."""
    clinic, trouble = make_clinic()
    store = RollingStore(window_seconds=3600.0)
    store.on_observed = clinic.observe
    store.add(obs(recv_ts=1000.0, origin_ts=1000.0))
    store.add(obs(recv_ts=1001.0, origin_ts=900.0))     # 100 s: quiet
    store.add(obs(recv_ts=1002.0))                      # no stamp: quiet
    assert flags_of(trouble, 1002.0) == {}
    store.add(obs(recv_ts=1003.0, origin_ts=500.0))     # 500 s behind best
    fact = flags_of(trouble, 1003.0)[(FLAG_TS_BACKWARDS, 0x21)]
    assert fact.events == 1
    assert fact.detail == 500
    assert fact.source == 0xB17E


def test_never_verdicts_and_identity_free_anonymous_traffic():
    """Anonymous traffic cannot be attributed to a key - it never
    becomes a flag about anyone."""
    clinic, trouble = make_clinic()
    store = RollingStore(window_seconds=3600.0)
    store.on_observed = clinic.observe
    for i in range(30):
        store.add(obs(recv_ts=1000.0 + i, prefix=0))
    assert trouble.records(1000.0) == []
    assert all(isinstance(f, ClinicFlagFact)
               for f in trouble.records(1000.0))

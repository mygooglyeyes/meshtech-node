"""Mesh Clinic v2 - chart derivations through the real ingest seam.

Under test: charts.py (CLINIC-WIRE.md record kinds 1-2) fed the way
the service feeds it - RollingStore.add()'s ONE fan-out hook - with
real Observation objects. Signal is never distance; missing numbers
stay missing (sentinels); nothing is ever pinned.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.charts import NodeChartBook  # noqa: E402
from meshtech_node.codec import (  # noqa: E402
    SHARE_UNKNOWN_PCT, SIGNAL_SD_UNKNOWN, SIGNAL_UNKNOWN,
    ClinicNodeFact, ClinicRouteFact,
)
from meshtech_node.observations import Observation, RollingStore  # noqa: E402


def obs(**kw):
    base = dict(recv_ts=1000.0, origin_ts=None, prefix=0x21,
                lat=None, lon=None, path_prefixes=[])
    base.update(kw)
    return Observation(**base)


def make_book():
    book = NodeChartBook(origin=0xB17E)
    store = RollingStore(window_seconds=3600.0)
    store.on_observed = book.observe     # the service's one wiring line
    return book, store


def node_facts(book, store, now):
    return [f for f in book.records(store, now)
            if isinstance(f, ClinicNodeFact)]


def route_facts(book, store, now):
    return [f for f in book.records(store, now)
            if isinstance(f, ClinicRouteFact)]


# ------------------------------------------------------------- node facts

def test_availability_strip_bits_and_ages():
    """Bit 23 = the current hour, bit 0 = the oldest: heard hours set
    their bits, unheard hours stay honestly zero."""
    book, store = make_book()
    now = time.time()
    store.add(obs(recv_ts=now - 3 * 3600.0, path_prefixes=[0x11]))
    store.add(obs(recv_ts=now, path_prefixes=[0x11, 0x12]))
    facts = node_facts(book, store, now)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.source == 0xB17E            # first-hand: the box itself
    assert fact.prefix == 0x21
    assert fact.strip & (1 << 23)           # current hour
    assert fact.strip & (1 << 20)           # 3 hours back
    assert not fact.strip & (1 << 22)       # 1 hour back: no hear
    assert fact.last_age_min == 0           # just heard
    assert fact.age_days == 0


def test_signal_stats_smoothed_best_worst_variance():
    book, store = make_book()
    for i, (rssi, snr) in enumerate([(-90.0, 5.0), (-80.0, 9.0),
                                     (-100.0, 1.0)]):
        store.add(obs(recv_ts=1000.0 + i, rssi=rssi, snr=snr))
    fact = node_facts(book, store, 1000.5)[0]
    # SNR rides the wire in quarter-dB; RSSI in dBm
    assert (fact.snr_best, fact.snr_worst) == (36, 4)      # 9 and 1 dB
    assert (fact.rssi_best, fact.rssi_worst) == (-80, -100)
    # EWMA (alpha 0.20): 5 -> 5.8 -> 4.84 dB = 19.36 quarter-dB
    assert fact.snr_ewma == 19
    # sample sd: SNR 4.0 dB (16 quarter-dB), RSSI 10.0 dB
    assert fact.snr_sd == 16
    assert fact.rssi_sd == 10


def test_missing_signal_stays_missing():
    """No radio reading = sentinels, never a plausible constant."""
    book, store = make_book()
    store.add(obs(recv_ts=1000.0))          # rssi/snr are None
    fact = node_facts(book, store, 1000.0)[0]
    assert (fact.snr_ewma, fact.snr_best, fact.snr_worst) == \
        (SIGNAL_UNKNOWN, SIGNAL_UNKNOWN, SIGNAL_UNKNOWN)
    assert (fact.rssi_ewma, fact.rssi_best, fact.rssi_worst) == \
        (SIGNAL_UNKNOWN, SIGNAL_UNKNOWN, SIGNAL_UNKNOWN)
    assert fact.snr_sd == SIGNAL_SD_UNKNOWN
    assert fact.rssi_sd == SIGNAL_SD_UNKNOWN
    # one sample: ewma/best/worst are real, sd still unknown
    store.add(obs(recv_ts=2000.0, prefix=0x22, rssi=-70.0, snr=2.0))
    fact = [f for f in node_facts(book, store, 2000.0) if f.prefix == 0x22][0]
    assert fact.rssi_best == -70 and fact.snr_best == 8
    assert fact.rssi_sd == SIGNAL_SD_UNKNOWN


def test_hop_typicality_and_traffic_share():
    book, store = make_book()
    now = time.time()
    for _ in range(3):
        store.add(obs(recv_ts=now, path_prefixes=[0x11]))       # 2 hops
    store.add(obs(recv_ts=now, path_prefixes=[0x11, 0x12]))     # 3 hops
    store.add(obs(recv_ts=now, prefix=0x22, path_prefixes=[]))  # 1 hop
    facts = {f.prefix: f for f in node_facts(book, store, now)}
    assert facts[0x21].hops_typ == 2      # most common of the node's hears
    assert facts[0x22].hops_typ == 1      # direct = one radio hop
    assert facts[0x21].share_pct == 80    # 4 of 5 identified packets
    assert facts[0x22].share_pct == 20


def test_share_unknown_when_no_traffic():
    book, store = make_book()
    now = time.time()
    store.add(obs(recv_ts=now))
    # an out-of-window nothing: force the 24 h count to zero by aging
    book.charts[0x21].buckets = [0] * 24
    fact = node_facts(book, store, now)[0]
    assert fact.share_pct == SHARE_UNKNOWN_PCT


# ------------------------------------------------------------ route facts

def test_route_fact_spread_direct_and_ages():
    """delay min/med/max from honest non-negative samples; a negative
    'delay' is clock skew and never becomes a delay fact."""
    book, store = make_book()
    now = time.time()
    for i, delay in enumerate([6.0, 2.0, 10.0, -3.0]):
        ts = now - 120.0 + i
        store.add(obs(recv_ts=ts, origin_ts=ts - delay))   # delay is derived
    facts = route_facts(book, store, now)
    assert len(facts) == 1
    fact = facts[0]
    assert fact.path == (0x21,)          # direct routes anchor on the sender
    assert fact.direct == 1
    assert fact.uses == 4
    assert fact.delay_min_s == 2
    assert fact.delay_med_s == 6         # median of the honest samples
    assert fact.delay_max_s == 10
    assert fact.last_age_min == 1        # ~2 minutes of aging, whole minutes
    assert fact.age_days == 0


def test_route_fact_multihop_shape_and_caps():
    book, store = make_book()
    now = time.time()
    store.add(obs(recv_ts=now, path_prefixes=[0x11, 0x22, 0x33],
                  origin_ts=now - 4.0))
    fact = route_facts(book, store, now)[0]
    assert fact.path == (0x11, 0x22, 0x33)
    assert fact.direct == 0
    assert (fact.delay_min_s, fact.delay_med_s, fact.delay_max_s) == (4, 4, 4)


# ------------------------------------------------------ isolation baseline

def test_isolation_baseline_needs_no_peers():
    """Zero peers heard = a complete clinic from direct facts alone."""
    book, store = make_book()
    now = time.time()
    store.add(obs(recv_ts=now, rssi=-80.0, snr=4.0))
    facts = book.records(store, now)
    kinds = {type(f) for f in facts}
    assert ClinicNodeFact in kinds and ClinicRouteFact in kinds
    assert all(f.source == 0xB17E for f in facts)


# ------------------------------------------------------------ persistence

def test_chart_rows_round_trip():
    """The disk row shape is stable: a chart written and re-read keeps
    its strip, stats and histogram exactly."""
    book, store = make_book()
    now = time.time()
    for i in range(3):
        store.add(obs(recv_ts=now - i * 60.0, rssi=-80.0 - i, snr=3.0,
                      path_prefixes=[0x11]))
    row = book.charts[0x21].to_row()
    restored = type(book.charts[0x21]).from_row(row)
    assert restored.buckets == book.charts[0x21].buckets
    assert restored.hops == book.charts[0x21].hops
    assert restored.sig["rssi"].n == 3
    assert restored.sig["rssi"].best == -80.0
    assert restored.sig["snr"].sd == book.charts[0x21].sig["snr"].sd

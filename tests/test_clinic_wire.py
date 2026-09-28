"""Mesh Clinic v2 - the CLINIC wire itself (CLINIC-WIRE.md).

Under test: codec.py record kinds 1-4 + the envelope caps, and
feedbuilder.build_clinic_batch's cursor walk. The wire page governs
these bytes: <= 163 B, <= 7 records, sentinels mean "unknown", and a
fact the wire cannot carry is refused LOUDLY - never pinned.
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from meshtech_node import codec  # noqa: E402
from meshtech_node.budget import BudgetLimiter  # noqa: E402
from meshtech_node.config import AreaCfg, FeedCfg, RadioCfg, Settings  # noqa: E402
from meshtech_node.feedbuilder import FeedBuilder  # noqa: E402
from meshtech_node.grid import geometry_from  # noqa: E402
from meshtech_node.observations import RollingStore  # noqa: E402


def node_fact(**kw):
    base = dict(source=0xB17E, prefix=0x21, last_age_min=3, age_days=5,
                strip=0x800007, hops_typ=2, share_pct=37,
                snr_ewma=20, snr_best=36, snr_worst=4, snr_sd=16,
                rssi_ewma=-90, rssi_best=-80, rssi_worst=-100, rssi_sd=10)
    base.update(kw)
    return codec.ClinicNodeFact(**base)


def route_fact(**kw):
    base = dict(source=0xB17E, path=(0x11, 0x22), uses=56, direct=0,
                delay_min_s=2, delay_med_s=4, delay_max_s=9,
                last_age_min=17, age_days=2)
    base.update(kw)
    return codec.ClinicRouteFact(**base)


# ------------------------------------------------------------- round trips

def test_all_record_kinds_round_trip_byte_identical():
    records = [
        node_fact(),
        route_fact(),
        codec.ClinicFlagFact(source=0xB17E, flag=codec.FLAG_TS_BACKWARDS,
                             subject=0x21, events=2, first_age_min=30,
                             last_age_min=5, detail=500),
        codec.ClinicPeerFact(source=0xBEEF, report=codec.REPORT_PULSE,
                             subject=0, heard_age_min=4,
                             values=(1234, 4, 40, 9)),
    ]
    raw = codec.encode_clinic(records, seq=7, origin=0xB17E)
    assert codec.peek_data_type(raw) == codec.TYPE_CLINIC
    decoded = codec.decode_any(raw)
    assert isinstance(decoded, codec.Clinic)
    assert decoded.seq == 7 and decoded.origin == 0xB17E
    assert codec.encode_clinic(decoded.records, seq=decoded.seq,
                               origin=decoded.origin) == raw
    # provenance: source rides every record (first-hand = packet origin)
    assert decoded.records[0].source == 0xB17E == decoded.origin
    assert decoded.records[3].source == 0xBEEF != decoded.origin


def test_peer_report_shapes_round_trip():
    reports = [
        codec.ClinicPeerFact(source=0xBEEF, report=codec.REPORT_SECT_SUM,
                             subject=3, heard_age_min=1,
                             values=(2, 99, 5, 44)),
        codec.ClinicPeerFact(source=0xBEEF, report=codec.REPORT_ROUTE,
                             subject=0xABCD, heard_age_min=2,
                             values=(56, 4, 17, 0), path=(0x11, 0x22, 0x33)),
        codec.ClinicPeerFact(source=0xBEEF, report=codec.REPORT_INTRO,
                             subject=0x42, heard_age_min=3,
                             cls=1, lat=37.125, lon=-122.5, name="Valley"),
    ]
    raw = codec.encode_clinic(reports, seq=9, origin=0xB17E)
    decoded = codec.decode_any(raw)
    sect, route, intro = decoded.records
    assert sect.values == (2, 99, 5, 44)
    assert route.values == (56, 4, 17, 0) and route.path == (0x11, 0x22, 0x33)
    assert intro.cls == 1 and intro.name == "Valley"
    assert abs(intro.lat - 37.125) < 1e-6 and abs(intro.lon + 122.5) < 1e-6
    for record in decoded.records:
        assert record.source == 0xBEEF


def test_peer_no_position_is_null_island_rule():
    """0/0 = the peer reported NO position - never a real (0, 0)."""
    record = codec.ClinicPeerFact(source=1, report=codec.REPORT_INTRO,
                                  subject=2, heard_age_min=0,
                                  lat=None, lon=None, name="")
    decoded = codec.decode_any(codec.encode_clinic([record], seq=1))
    assert decoded.records[0].lat is None
    assert decoded.records[0].lon is None


def test_sentinels_survive_the_wire():
    record = node_fact(snr_ewma=codec.SIGNAL_UNKNOWN,
                       snr_best=codec.SIGNAL_UNKNOWN,
                       snr_worst=codec.SIGNAL_UNKNOWN,
                       snr_sd=codec.SIGNAL_SD_UNKNOWN,
                       rssi_ewma=codec.SIGNAL_UNKNOWN,
                       rssi_best=codec.SIGNAL_UNKNOWN,
                       rssi_worst=codec.SIGNAL_UNKNOWN,
                       rssi_sd=codec.SIGNAL_SD_UNKNOWN,
                       share_pct=codec.SHARE_UNKNOWN_PCT,
                       hops_typ=0, last_age_min=codec.AGE_UNKNOWN_MIN)
    decoded = codec.decode_any(codec.encode_clinic([record], seq=1))
    fact = decoded.records[0]
    assert fact.snr_ewma == codec.SIGNAL_UNKNOWN
    assert fact.rssi_sd == codec.SIGNAL_SD_UNKNOWN
    assert fact.share_pct == codec.SHARE_UNKNOWN_PCT
    assert fact.hops_typ == 0
    assert fact.last_age_min == codec.AGE_UNKNOWN_MIN


# ---------------------------------------------------------- refusals (loud)

def test_wire_refuses_what_it_cannot_carry_never_pins():
    with pytest.raises(codec.CodecError):
        codec.encode_clinic([node_fact(snr_ewma=1000)], seq=1)   # past i8
    with pytest.raises(codec.CodecError):
        codec.encode_clinic([node_fact(prefix=999)], seq=1)      # past u8
    with pytest.raises(codec.CodecError):
        codec.encode_clinic([route_fact(path=())], seq=1)        # empty trail
    long_name = "x" * (codec.CLINIC_MAX_NAME + 1)
    with pytest.raises(codec.CodecError):
        codec.encode_clinic([codec.ClinicPeerFact(
            source=1, report=codec.REPORT_INTRO, subject=2,
            heard_age_min=0, name=long_name)], seq=1)            # never cut


def test_envelope_caps_seven_records_and_163_bytes():
    with pytest.raises(codec.CodecError):
        codec.encode_clinic([node_fact(prefix=i) for i in range(8)], seq=1)
    fat = [route_fact(path=(0x11,) * 8) for _ in range(7)]
    with pytest.raises(codec.CodecError):
        codec.encode_clinic(fat, seq=1)                          # 191 B
    ok = codec.encode_clinic(fat[:5], seq=1)                     # 139 B
    assert len(ok) <= codec.MAX_CHANNEL_DATA


def test_malformed_clinic_raises_not_crashes():
    raw = codec.encode_clinic([node_fact(), route_fact()], seq=3)
    for cut in range(3, len(raw)):
        with pytest.raises(codec.CodecError):
            codec.decode_any(raw[:cut])


# ------------------------------------------------------ the cursor batches

def make_builder():
    settings = Settings(area=AreaCfg(name="Test", center_lat=37.0,
                                     center_lon=-122.0, span_km=40.0,
                                     grid=3),
                        feed=FeedCfg(burst_gap_seconds=0.0))
    store = RollingStore(window_seconds=3600.0)
    geo = geometry_from(37.0, -122.0, 40000.0, 3)
    budget = BudgetLimiter(RadioCfg(), 24, 1.0)
    return FeedBuilder(settings, store, geo, budget, origin=0xB17E), store


class FakeBook:
    def __init__(self, records):
        self._records = records

    def records(self, store, now):
        return list(self._records)


class FakePeers:
    def __init__(self, records=()):
        self._records = list(records)

    def records(self, now):
        return list(self._records)


def test_cursor_batches_cycle_every_fact():
    """More facts than one batch carries: the cursor walks, and every
    fact shows up inside a few batches."""
    builder, store = make_builder()
    facts = [node_fact(prefix=0x30 + i) for i in range(10)]
    clinic = FakeBook(facts)
    seen = set()
    for _ in range(2):
        pkt = builder.build_clinic_batch(clinic, FakePeers(), now=time.time())
        assert pkt is not None
        assert pkt.data_type == codec.TYPE_CLINIC
        assert len(pkt.payload) <= codec.MAX_CHANNEL_DATA
        decoded = codec.decode_any(pkt.payload)
        assert len(decoded.records) <= codec.CLINIC_MAX_RECORDS
        assert decoded.origin == 0xB17E          # the sending box
        seen.update(r.prefix for r in decoded.records)
    assert seen == {0x30 + i for i in range(10)}


def test_batch_fills_by_size_not_just_count():
    builder, store = make_builder()
    fat = [route_fact(path=(0x11,) * 8) for _ in range(7)]
    pkt = builder.build_clinic_batch(FakeBook(fat), FakePeers(),
                                     now=time.time())
    assert len(codec.decode_any(pkt.payload).records) < 7
    assert len(pkt.payload) <= codec.MAX_CHANNEL_DATA


def test_unwireable_fact_is_refused_loudly_and_alone():
    """A fact the wire cannot carry is skipped with a warning; the
    mintable facts around it still ride."""
    builder, store = make_builder()
    clinic = FakeBook([node_fact(prefix=0x21, snr_ewma=1000),
                       node_fact(prefix=0x22)])
    pkt = builder.build_clinic_batch(clinic, FakePeers(), now=time.time())
    decoded = codec.decode_any(pkt.payload)
    assert [r.prefix for r in decoded.records] == [0x22]


def test_no_facts_no_packet():
    builder, store = make_builder()
    assert builder.build_clinic_batch(FakeBook([]), FakePeers(),
                                      now=time.time()) is None


def test_peer_reports_ride_the_same_batch_tagged():
    """First-hand and second-hand records share a batch; provenance is
    the record's source field."""
    builder, store = make_builder()
    clinic = FakeBook([node_fact()])
    peers = FakePeers([codec.ClinicPeerFact(
        source=0xBEEF, report=codec.REPORT_PULSE, subject=0,
        heard_age_min=4, values=(1, 2, 3, 4))])
    pkt = builder.build_clinic_batch(clinic, peers, now=time.time())
    decoded = codec.decode_any(pkt.payload)
    by_source = {r.source for r in decoded.records}
    assert by_source == {0xB17E, 0xBEEF}

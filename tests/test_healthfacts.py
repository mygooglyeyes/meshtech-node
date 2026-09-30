"""MESH HEALTH FACTS (HEALTH-DEFINITIONS.md, Brett 2026-09-29).

The minters count only what the radio proves: flood duplicates,
airtime occupancy, duty headroom, sequence gaps/reordering, flaps,
proven hash collisions, and ask->answer exchanges. Every window
reports its own HONEST length - a partial window never claims a full
span - and no evidence means no record, never a pinned zero.
"""

import pytest

from meshtech_node import codec
from meshtech_node.clinic import ClinicIngest
from meshtech_node.healthfacts import HealthTracker
from meshtech_node.repeaters import RepeaterTable

T0 = 1_000_000.0


def _by_kind(records, cls):
    return [r for r in records if isinstance(r, cls)]


# ------------------------------------------------------------- kind 5


def test_airtime_fact_counts_duplicates_occupancy_and_duty():
    tracker = HealthTracker(0xB17E,
                            airtime_ms_fn=lambda n: 100.0,
                            duty_fn=lambda now: (45, 3600))
    # Production feeding (rawsource): EVERY heard copy passes
    # note_frame - including repeats - and a repeat also gets
    # note_dup. The ratio is the share of everything heard.
    tracker.note_frame(50, now=T0)    # copy 1
    tracker.note_frame(50, now=T0)    # copy 2
    tracker.note_frame(50, now=T0)    # copy 3 - a repeat of copy 1
    tracker.note_dup(now=T0)          # the repeat's verdict
    facts = _by_kind(tracker.records(T0 + 60), codec.ClinicAirtimeFact)
    assert len(facts) == 1
    f = facts[0]
    assert f.window_min == 1          # honest partial window (a minute)
    assert f.dup_per_mille == 333     # 1 repeat of 3 copies heard
                                     # (never 1/4: no double-count)
    assert f.tx_used_s == 45
    assert f.duty_headroom_s == 3555  # allowance minus sent


def test_window_reports_its_real_length_then_closes():
    tracker = HealthTracker(0xB17E, airtime_ms_fn=lambda n: 1.0)
    tracker.note_frame(10, now=T0)
    early = _by_kind(tracker.records(T0 + 60), codec.ClinicAirtimeFact)[0]
    assert early.window_min == 1      # partial: never claims 60
    # The window closes at the hour: the CLOSED hour is reported.
    tracker.note_frame(10, now=T0 + 4000)
    late = _by_kind(tracker.records(T0 + 4000), codec.ClinicAirtimeFact)[0]
    assert late.window_min == 60
    assert late.occupancy_per_mille == 0  # 10 ms in a whole hour


def test_nothing_counted_means_no_airtime_evidence():
    tracker = HealthTracker(0xB17E)   # no airtime fn, no duty fn
    assert tracker.records(T0) == []  # no evidence, no record - ever


# ------------------------------------------------------------- kind 6


def test_seq_loss_reordering_and_reboot_are_counted_honestly():
    tracker = HealthTracker(0xB17E)
    for seq in (10, 11, 12):
        tracker.note_scope(0x1234, seq, now=T0)
    tracker.note_scope(0x1234, 15, now=T0 + 1)    # 13, 14 never heard
    tracker.note_scope(0x1234, 13, now=T0 + 2)    # a late arrival
    tracker.note_scope(0x1234, 500, now=T0 + 3)   # reboot: count NOTHING
    facts = _by_kind(tracker.records(T0 + 60), codec.ClinicSenderFact)
    assert len(facts) == 1
    f = facts[0]
    assert f.sender == 0x1234
    assert f.lost == 2
    assert f.reordered == 1
    assert f.flaps == 0


def test_a_flap_is_heard_silent_30min_heard():
    tracker = HealthTracker(0xB17E)
    tracker.note_scope(0x1234, 1, now=T0)
    tracker.note_scope(0x1234, 2, now=T0 + 60)          # still there
    tracker.note_scope(0x1234, 3, now=T0 + 60 + 1800)   # back after 30 min
    facts = _by_kind(tracker.records(T0 + 3600),
                     codec.ClinicSenderFact)
    assert facts[0].flaps == 1


def test_duplicates_attribute_only_with_remembered_evidence():
    tracker = HealthTracker(0xB17E)
    tracker.note_dup(payload=b"never seen", now=T0)   # anonymous: no blame
    assert _by_kind(tracker.records(T0 + 60),
                    codec.ClinicSenderFact) == []
    tracker.note_scope(0x1234, 1, now=T0, payload=b"packet one")
    tracker.note_dup(payload=b"packet one", now=T0 + 1)
    facts = _by_kind(tracker.records(T0 + 60), codec.ClinicSenderFact)
    assert facts[0].dup_per_mille > 0


# ------------------------------------------------------------- kind 7


def test_ask_answer_scoring_counts_only_air_exchanges():
    tracker = HealthTracker(0xB17E)
    ask = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                           target=5, nonce=1, origin=0x0042, host=0xB17E)
    tracker.observe_packet(ask, now=T0)
    tracker.note_own_answer(codec.REFRESH_KIND_SECTION, 5, now=T0 + 9)
    tracker.observe_packet(ask, now=T0 + 10)     # never answered
    facts = _by_kind(tracker.records(T0 + 60), codec.ClinicExchangeFact)
    assert len(facts) == 1
    assert facts[0].asked == 2
    assert facts[0].answered == 1
    assert facts[0].median_answer_s == 9


def test_an_overheard_answer_counts_too():
    tracker = HealthTracker(0xB17E)
    tracker.observe_packet(codec.RefreshReq(
        seq=1, kind=codec.REFRESH_KIND_SECTION, target=5, nonce=1,
        origin=0x0042, host=0xB17E), now=T0)
    tracker.observe_packet(codec.SectSum(
        seq=2, section_id=5, active_nodes=1, packet_count=2,
        delay_p50_s=0, delay_p90_s=0), now=T0 + 5)
    facts = _by_kind(tracker.records(T0 + 60), codec.ClinicExchangeFact)
    assert facts[0].answered == 1


# ------------------------------------------------------------- kind 8


def test_collisions_are_proven_pairs_and_capped():
    tracker = HealthTracker(0xB17E)
    tag = b"\x21\x33"
    key_a = bytes.fromhex("0102030405060708")
    key_b = bytes.fromhex("a1a2a3a4a5a6a7a8")
    tracker.note_collision(tag, key_a, key_b, now=T0)
    tracker.note_collision(tag, key_b, key_a, now=T0)  # same pair, orderless
    tracker.note_collision(tag, key_a, key_a, now=T0)  # one key: not a pair
    facts = _by_kind(tracker.records(T0), codec.ClinicCollisionFact)
    assert len(facts) == 1
    assert facts[0].tag == (0x21, 0x33)
    assert facts[0].key_a == key_a
    assert facts[0].key_b == key_b
    for i in range(20):
        tracker.note_collision(bytes([i]), key_a, bytes([i]) * 8, now=T0)
    assert len(_by_kind(tracker.records(T0),
                        codec.ClinicCollisionFact)) <= 8


def test_the_repeater_table_proves_the_collision_on_air():
    table = RepeaterTable()
    seen = []
    table.collision_sink = lambda tag, a, b: seen.append((tag, a, b))
    tag = b"\x21\x33"
    key_a = tag + bytes(30)
    key_b = tag + bytes([1] * 30)
    table.observe_tag(tag, now=T0)
    table.observe_advert(key_a, "A", 1, now=T0)   # promotes the tag
    assert seen == []
    table.observe_advert(key_b, "B", 1, now=T0)   # SAME tag, OTHER key
    assert seen == [(tag, key_a, key_b)]


# -------------------------------------------------- wire + the walk


def test_health_records_ride_the_clinic_walk():
    class _FakeBook:
        def records(self, *args):
            return []

    tracker = HealthTracker(0xB17E, airtime_ms_fn=lambda n: 1.0)
    tracker.note_frame(10, now=T0)
    ingest = ClinicIngest(_FakeBook(), _FakeBook(), health=tracker)
    records = ingest.records(None, T0 + 60)
    assert _by_kind(records, codec.ClinicAirtimeFact)


def test_health_records_round_trip_through_the_wire():
    records = [
        codec.ClinicAirtimeFact(
            source=0xB17E, window_min=60, dup_per_mille=400,
            occupancy_per_mille=12, duty_headroom_s=3500, tx_used_s=100),
        codec.ClinicSenderFact(
            source=0xB17E, sender=0x1234, window_min=120,
            dup_per_mille=250, lost=2, reordered=1, flaps=3),
        codec.ClinicExchangeFact(
            source=0xB17E, window_min=60, asked=4, answered=3,
            median_answer_s=9),
        codec.ClinicCollisionFact(
            source=0xB17E, tag=(0x21, 0x33),
            key_a=bytes.fromhex("0102030405060708"),
            key_b=bytes.fromhex("a1a2a3a4a5a6a7a8"), last_age_min=7),
    ]
    raw = codec.encode_clinic(records, seq=0x0114, origin=0xB17E)
    back = codec.decode_clinic(raw[3:])
    assert back.records == records


def test_the_wire_refuses_loudly_never_pads_a_key():
    with pytest.raises(codec.CodecError):
        codec.encode_clinic_record(codec.ClinicCollisionFact(
            source=1, tag=(0x21,), key_a=b"short", key_b=b"alsoshort"))
    with pytest.raises(codec.CodecError):
        codec.encode_clinic_record(codec.ClinicCollisionFact(
            source=1, tag=(), key_a=b"12345678", key_b=b"12345678"))

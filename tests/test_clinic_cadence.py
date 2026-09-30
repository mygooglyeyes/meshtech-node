"""Mesh Clinic v2 - the three live paths, through the real surfaces.

Phase 1's own grade named these gaps: the clinic batch riding the
broadcast_loop pulse beat, the removal-age prune, and forget_node's
disk cleanup were wired but unexercised. Here each is driven the way
a caller hits it - the REAL broadcast loop with its real ticks, the
REAL on_packet surfaces, the REAL NodeStore.

Pinned here:
1. A pulse beat emits a cursor-rotated CLINIC batch that respects the
   163 B / 7-record caps - and ONLY while an over-the-air audience is
   proven (hard rule 6). The AIR's request/answer behavior stays
   clinic-free - the DOOR's answers carry the book now (Brett,
   2026-09-29; test_clinic_door_dump.py pins that side).
2. The prune drops expired chart/flag/peer facts at the ages
   CLINIC-WIRE.md states (30 d) and keeps fresh evidence.
3. forget_node removes everything the clinic held FOR THAT NODE
   across all the new tables - no orphan rows survive in SQLite.
"""
import asyncio
import logging
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node import codec  # noqa: E402
from meshtech_node.charts import NodeChart  # noqa: E402
from meshtech_node.config import Settings  # noqa: E402
from meshtech_node.observations import Observation  # noqa: E402
from meshtech_node.service import ScopeService  # noqa: E402

TICK_SECONDS = 5.0   # broadcast_loop's between-ticks sleep (hardcoded)


class FakeRadio:
    """Stands in for CompanionClient's link: records every send."""

    is_connected = True
    has_slot = True

    def __init__(self):
        self.sent = []

    async def send_channel_data(self, data_type: int, payload: bytes) -> bool:
        self.sent.append((data_type, bytes(payload)))
        return True


def make_clinic_service(**feed_overrides):
    settings = Settings()
    settings.area.center_lat = 37.0
    settings.area.center_lon = -122.0
    settings.feed.burst_gap_seconds = 0.0
    for key, value in feed_overrides.items():
        setattr(settings.feed, key, value)
    svc = ScopeService(settings, use_demo=False)   # gate LIVE, facts ours
    radio = FakeRadio()
    svc.client = radio
    return svc, radio


def obs(**kw):
    base = dict(recv_ts=1000.0, origin_ts=None, prefix=0x21,
                lat=None, lon=None, path_prefixes=[])
    base.update(kw)
    return Observation(**base)


async def _wait_until(cond, timeout=15.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(step)
    return False


def clinic_batches(radio):
    return [payload for dt, payload in radio.sent
            if dt == codec.TYPE_CLINIC]


# ---------------------------------------------- 1. the pulse beat's batch

@pytest.mark.asyncio
async def test_pulse_beat_emits_capped_cursor_clinic_batch():
    """The real loop's pulse beat carries pulse + sect + ONE clinic
    batch: <= 163 B, <= 7 records, cursor-rotated so every fact
    cycles through successive beats."""
    svc, radio = make_clinic_service(pulse_interval_seconds=0.0,
                                     layout_interval_seconds=3600.0)
    now = time.time()
    for i in range(10):
        svc.store.add(obs(recv_ts=now, prefix=0x30 + i, rssi=-80.0, snr=3.0))
    # the real audience sign: a heartbeat from an app, through on_packet
    await svc.on_packet(codec.Heartbeat(seq=1), "7eb1abc")
    svc.builder._last_pulse = time.time() - 999
    task = asyncio.create_task(svc.broadcast_loop())
    try:
        assert await _wait_until(lambda: len(clinic_batches(radio)) >= 1), \
            "no clinic batch on the first pulse beat"
        assert await _wait_until(lambda: len(clinic_batches(radio)) >= 2), \
            "no clinic batch on the second pulse beat"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # the beat is a trio: pulse, section summary, clinic
    types = [dt for dt, _ in radio.sent]
    assert codec.TYPE_PULSE in types and codec.TYPE_SECT_SUM in types
    seen = set()
    for payload in clinic_batches(radio)[:2]:
        assert len(payload) <= codec.MAX_CHANNEL_DATA
        batch = codec.decode_any(payload)
        assert batch.origin == svc.origin          # our box sent it
        assert 1 <= len(batch.records) <= codec.CLINIC_MAX_RECORDS
        seen.update(r.prefix for r in batch.records
                    if isinstance(r, codec.ClinicNodeFact))
    assert seen == {0x30 + i for i in range(10)}, \
        "the cursor must cycle every fact through the batches"


@pytest.mark.asyncio
async def test_no_air_audience_no_cadence_no_clinic():
    """Hard rule 6 unchanged: with no over-the-air sign nothing flies
    - then one heartbeat opens the window and the batch appears."""
    svc, radio = make_clinic_service(pulse_interval_seconds=0.0)
    now = time.time()
    for i in range(3):
        svc.store.add(obs(recv_ts=now, prefix=0x30 + i))
    svc.builder._last_pulse = time.time() - 999
    task = asyncio.create_task(svc.broadcast_loop())
    try:
        await asyncio.sleep(TICK_SECONDS + 2.0)    # >= one full gated tick
        assert radio.sent == [], \
            "the audience gate must keep the whole cadence grounded"
        await svc.on_packet(codec.Heartbeat(seq=1), "7eb1abc")
        assert await _wait_until(
            lambda: any(dt == codec.TYPE_CLINIC for dt, _ in radio.sent)), \
            "the heartbeat must open the window"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_request_answers_carry_no_clinic():
    """The AIR's request/answer behavior is unchanged: air connect
    pulses and radio refresh answers are exactly what they were -
    and never clinic packets. Dedupe still answers once."""
    svc, radio = make_clinic_service()
    now = time.time()
    for i in range(5):
        svc.store.add(type("O", (), {
            "recv_ts": now, "origin_ts": now - 1.5, "prefix": 0x21,
            "lat": 37.0, "lon": -122.0,
            "path_prefixes": [0x11, 0x12], "channel_name": None,
            "delay_s": 1.5})())
    await svc.pulse_now(reason="connect", with_layout=True)
    assert codec.TYPE_CLINIC not in [dt for dt, _ in radio.sent], \
        "pulse_now stays pulse(+layout) only"
    req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                           target=5, nonce=99)     # centre square
    await svc.on_packet(req, "aabbccddeeff")
    types = [dt for dt, _ in radio.sent]
    assert codec.TYPE_CLINIC not in types, \
        "refresh answers never carry clinic packets"
    assert codec.TYPE_SECT_SUM in types and codec.TYPE_ROUTE in types
    before = len(radio.sent)
    await svc.on_packet(req, "aabbccddeeff")       # duplicate ask
    assert len(radio.sent) == before, "dedupe must still answer once"


# ------------------------------------------------- 2. the removal ages

@pytest.mark.asyncio
async def test_prune_drops_expired_clinic_facts_at_their_ages(tmp_path):
    """CLINIC-WIRE.md's stated ages: charts die only with their node
    (30 d without a hear), flags expire 30 d after their most recent
    event, peer reports expire 30 d after the peer last said it -
    fresh evidence survives. Driven through the REAL loop's rare
    prune cadence, RAM and disk together."""
    from meshtech_node.node_store import NodeStore

    db = NodeStore(str(tmp_path / "n.db"))
    svc, radio = make_clinic_service(pulse_interval_seconds=999999.0,
                                     layout_interval_seconds=0.0)
    svc.store.disk = db
    svc.charts.disk = db
    svc.trouble.disk = db
    svc.peer_facts.disk = db
    now = time.time()
    old = now - 31.0 * 86400.0
    # expired evidence: an old chart + an old rate storm (minted from
    # old hears) + an old peer report
    for i in range(20):
        svc.store.add(obs(recv_ts=old + i, origin_ts=old + i, prefix=0x21))
    svc.peer_facts.observe(codec.Pulse(seq=1, uptime_min=1, rx_per_hour=1,
                                       feed_airtime_s_per_h=1,
                                       active_total=1, origin=0xBEEF),
                           now=old)
    # fresh evidence: a fresh chart + a fresh flag + a fresh peer report
    svc.store.add(obs(recv_ts=now, prefix=0x22, rssi=-70.0, snr=2.0))
    svc.clinic.note_sig_fail(0x42)
    svc.peer_facts.observe(codec.SectSum(seq=2, section_id=3,
                                         active_nodes=2, packet_count=99,
                                         delay_p50_s=5, delay_p90_s=44,
                                         origin=0xBEEF),
                           now=now)
    assert set(svc.charts.charts) == {0x21, 0x22}
    assert len(svc.trouble.flags) == 2              # storm + sig-fail
    assert len(svc.peer_facts.entries) == 2         # pulse + sect_sum
    # the real audience sign so the loop reaches its prune block
    await svc.on_packet(codec.Heartbeat(seq=1), "7eb1abc")
    task = asyncio.create_task(svc.broadcast_loop())
    try:
        assert await _wait_until(lambda: 0x21 not in svc.charts.charts), \
            "the 31-day-old chart never expired"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # charts: the old one dropped (RAM + disk), the fresh one kept
    assert set(svc.charts.charts) == {0x22}
    assert [r["prefix"] for r in db.clinic_node_rows()] == [0x22]
    # flags: the old storm expired, the fresh sig-fail evidence stayed
    flags = {(f.flag, f.subject) for f in svc.trouble.records(now)}
    assert flags == {(codec.FLAG_SIG_FAIL, 0x42)}
    assert [(r["kind"], r["subject"]) for r in db.clinic_flag_rows()] == \
        [(codec.FLAG_SIG_FAIL, 0x42)]
    # peer reports: the old word expired, the fresh one stayed
    assert [r.source for r in svc.peer_facts.records(now)] == [0xBEEF]
    assert [r["report"] for r in db.peer_report_rows()] == \
        [codec.REPORT_SECT_SUM]                     # the fresh one
    db.close()


# ------------------------------------------------- 3. forget_node cleanup

def test_forget_node_leaves_no_orphan_clinic_rows(tmp_path):
    """Everything the clinic held FOR ONE NODE dies with it across all
    the new tables - chart, flags, peer intro reports - with no orphan
    row surviving. A peer ROUTE report whose route id happens to
    equal the node prefix is NOT about the node and must survive."""
    from meshtech_node.node_store import NodeStore

    db = NodeStore(str(tmp_path / "n.db"))
    now = time.time()
    db.upsert_node(0x21, ts=now)
    db.upsert_clinic_node(NodeChart(0x21, now).to_row())
    db.upsert_clinic_flag({"kind": codec.FLAG_SIG_FAIL, "subject": 0x21,
                           "events": 2, "first": now - 60.0, "last": now,
                           "detail": 0})
    db.upsert_peer_report({"source": 0xBEEF, "report": codec.REPORT_INTRO,
                           "subject": 0x21, "path_hex": "",
                           "first": now - 30.0, "last": now,
                           "values": (0, 0, 0, 0), "cls": 1,
                           "lat": 37.1, "lon": -122.1, "name": "Valley"})
    db.upsert_peer_report({"source": 0xBEEF, "report": codec.REPORT_ROUTE,
                           "subject": 0x0021, "path_hex": "1122",
                           "first": now - 30.0, "last": now,
                           "values": (5, 4, 17, 0)})
    assert db.forget_node(0x21) == 1
    # no orphan row about this node anywhere in the clinic tables
    assert db.clinic_node_rows() == []
    assert [r for r in db.clinic_flag_rows() if r["subject"] == 0x21] == []
    assert [r for r in db.peer_report_rows()
            if r["report"] == codec.REPORT_INTRO and r["subject"] == 0x21] \
        == []
    # the route report is about a route, not the node - it stays
    rows = db.peer_report_rows()
    assert len(rows) == 1 and rows[0]["report"] == codec.REPORT_ROUTE
    db.close()


# --------------------------------------- the trim-cap + refusal path

@pytest.mark.asyncio
async def test_batch_cap_overflow_rotates_unmintable_refused_loudly(caplog):
    """The cap is real and honest: 7 node facts fill the 163 B packet
    to the very byte; the overflow ROTATES to the next beat (never
    dropped); a fact the wire can never carry is refused with a loud
    warning - never pinned to a plausible constant."""
    svc, radio = make_clinic_service(pulse_interval_seconds=0.0,
                                     layout_interval_seconds=3600.0)
    now = time.time()
    for i in range(10):
        svc.store.add(obs(recv_ts=now, prefix=0x30 + i, rssi=-80.0, snr=3.0))
    # one chart whose SNR sits far past the wire's i8 ruler
    svc.store.add(obs(recv_ts=now, prefix=0xEE, rssi=-80.0, snr=10000.0))
    await svc.on_packet(codec.Heartbeat(seq=1), "7eb1abc")
    svc.builder._last_pulse = time.time() - 999
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(svc.broadcast_loop())
        try:
            assert await _wait_until(
                lambda: len(clinic_batches(radio)) >= 2), \
                "two pulse beats never fired"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    sizes = [len(p) for p in clinic_batches(radio)[:2]]
    batches = [codec.decode_any(p) for p in clinic_batches(radio)[:2]]
    # the boundary: 7 node facts = exactly 163 B and exactly 7 records
    assert sizes[0] == codec.MAX_CHANNEL_DATA
    assert len(batches[0].records) == codec.CLINIC_MAX_RECORDS
    for size, batch in zip(sizes, batches):
        assert size <= codec.MAX_CHANNEL_DATA
        assert len(batch.records) <= codec.CLINIC_MAX_RECORDS
    # overflow ROTATES: what batch 1 could not carry rides batch 2
    first = {r.prefix for r in batches[0].records
             if isinstance(r, codec.ClinicNodeFact)}
    second = {r.prefix for r in batches[1].records
              if isinstance(r, codec.ClinicNodeFact)}
    assert first == {0x30 + i for i in range(7)}
    assert {0x37, 0x38, 0x39} <= second
    # refused LOUDLY, never pinned: no node fact for 0xEE anywhere
    # (a clamped 127 dB SNR would be a fabricated number)
    for batch in batches:
        assert not any(isinstance(r, codec.ClinicNodeFact)
                       and r.prefix == 0xEE for r in batch.records)
    assert any("NOT minted" in r.message for r in caplog.records), \
        "the refusal must be loud"


@pytest.mark.asyncio
async def test_peer_word_the_wire_cannot_carry_is_refused_not_cut(caplog):
    """A peer word that cannot fit the wire (name past 24 B) is
    refused loudly at mint time - never truncated into a wrong name.
    The fold itself keeps the fact (with a warning), not a deletion."""
    svc, radio = make_clinic_service()
    await svc.on_packet(codec.Intro(
        seq=1, origin=0xBEEF, span_m=40000.0,
        entries=[codec.IntroEntry(prefix=0x42, name="x" * 30,
                                  lat=37.1, lon=-122.1)]), "unknown")
    assert svc.peer_facts.entries, "the fold must keep what it heard"
    now = time.time()
    with caplog.at_level(logging.WARNING):
        # the ONLY fact is unmintable: honestly, no packet at all
        assert svc.builder.build_clinic_batch(svc.clinic, svc.peer_facts,
                                              now=now) is None
        # give the batch something mintable - the bad name still stays
        await svc.on_packet(codec.Pulse(seq=2, uptime_min=1, rx_per_hour=1,
                                        feed_airtime_s_per_h=1,
                                        active_total=1, origin=0xBEEF),
                            "unknown")
        pkt = svc.builder.build_clinic_batch(svc.clinic, svc.peer_facts,
                                             now=now)
    batch = codec.decode_any(pkt.payload)
    assert not any(isinstance(r, codec.ClinicPeerFact)
                   and r.report == codec.REPORT_INTRO
                   for r in batch.records), \
        "an unmintable name must not appear, cut or whole"
    assert any("never truncated" in r.message for r in caplog.records)


def test_peer_reports_table_trim_cap(tmp_path, monkeypatch):
    """The bounded-table trim (the storage twin of the batch cap):
    past the cap the OLDEST reports go first - a phone-less month can
    never grow the table forever. Driven through the real write path
    (observe -> upsert -> trim on every 256th write-through)."""
    import meshtech_node.peerfacts as peerfacts_mod
    from meshtech_node.node_store import NodeStore

    monkeypatch.setattr(peerfacts_mod, "PEER_MAX_ROWS", 10)
    db = NodeStore(str(tmp_path / "n.db"))
    peers = peerfacts_mod.PeerFacts(origin=0xB17E, disk=db)
    now = time.time()
    for i in range(256):
        peers.observe(codec.Pulse(seq=i, uptime_min=i, rx_per_hour=1,
                                  feed_airtime_s_per_h=1, active_total=1,
                                  origin=0x1000 + i),
                      now=now - (256 - i))
    assert len(peers.entries) == 256          # RAM heard every word
    rows = db.peer_report_rows()
    assert len(rows) == 10, "the cap must bound the table"
    assert {r["source"] for r in rows} == {0x1000 + i
                                          for i in range(246, 256)}, \
        "the oldest rows must go first"
    db.close()


# ------------------------------------------------- companion mode clinic

@pytest.mark.asyncio
async def test_companion_mode_clinic_listens_but_never_transmits():
    """A companion box has no host feed: the loop parks before
    anything is built or sent (nothing of ours to send) - even for a
    live app. But the clinic still assembles what the box HEARS."""
    svc, radio = make_clinic_service(companion_mode=True,
                                     pulse_interval_seconds=0.0)
    now = time.time()
    svc.store.add(obs(recv_ts=now, prefix=0x30, rssi=-75.0, snr=4.0))
    await svc.on_packet(codec.Pulse(seq=1, uptime_min=42, rx_per_hour=7,
                                    feed_airtime_s_per_h=3,
                                    active_total=40, origin=0xBEEF),
                        "unknown")
    task = asyncio.create_task(svc.broadcast_loop())
    try:
        await svc.on_packet(codec.Heartbeat(seq=1), "7eb1abc")
        await asyncio.sleep(TICK_SECONDS + 2.0)   # >= one full tick
        assert radio.sent == [], \
            "companion mode must never transmit, clinic included"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    # the picture still builds from hears: first-hand chart + peer word
    assert 0x30 in svc.charts.charts
    assert [r.source for r in svc.peer_facts.records(time.time())] == \
        [0xBEEF]


# ------------------------------- the door tap + budget limiter claims

class RecordingTap:
    """Records every packet the feed offers to the door tap."""

    current_req_id = None

    def __init__(self):
        self.entries = []

    def on_built_packet(self, data_type, payload, *, would_tx,
                        tx_ok=False, in_reply_to=None):
        self.entries.append((data_type, bytes(payload), would_tx, tx_ok))


@pytest.mark.asyncio
async def test_clinic_batch_rides_door_tap_once_audience_opens():
    """CLINIC-WIRE.md's claim, clinic-specifically: every built clinic
    batch goes through the door tap like all others - and while the
    audience gate is closed NOTHING is built, so nothing is tapped.
    Once a live app signs in, the batch reaches the air AND the tap,
    byte-identical."""
    svc, radio = make_clinic_service(pulse_interval_seconds=0.0)
    tap = RecordingTap()
    svc.feed_tap = tap
    now = time.time()
    for i in range(3):
        svc.store.add(obs(recv_ts=now, prefix=0x30 + i))
    svc.builder._last_pulse = time.time() - 999
    task = asyncio.create_task(svc.broadcast_loop())
    try:
        await asyncio.sleep(TICK_SECONDS + 2.0)     # gated ticks
        assert tap.entries == [] and radio.sent == [], \
            "with the audience closed nothing may be built or tapped"
        await svc.on_packet(codec.Heartbeat(seq=1), "7eb1abc")
        assert await _wait_until(
            lambda: any(dt == codec.TYPE_CLINIC for dt, _ in radio.sent)), \
            "the batch never went out after the audience opened"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    sent = [p for dt, p in radio.sent if dt == codec.TYPE_CLINIC]
    tapped = [(p, would_tx, tx_ok) for dt, p, would_tx, tx_ok in tap.entries
              if dt == codec.TYPE_CLINIC]
    assert sent and tapped, "the clinic batch must reach air AND tap"
    assert tapped[0][0] == sent[0], \
        "the tap must see the same bytes that flew"
    assert tapped[0][2] is True, "the radio accepted the send"
    assert tapped[0][1] == svc.tx_enabled, \
        "would_tx must honestly mirror the TX gate state"


@pytest.mark.asyncio
async def test_clinic_batch_denied_by_budget_is_dropped_honestly(caplog):
    """The budget limiter governs clinic packets like always: when
    the REAL limiter refuses it, the batch never reaches the air, the
    door tap is told the truth (built, NOT sent), the refusal is
    loud - and the facts are deferred to a later beat, never lost."""
    # 3 packets/hour: the seed LAYOUT + PULSE + SECT_SUM spend them,
    # so the clinic packet is exactly the one the limiter refuses
    svc, radio = make_clinic_service(pulse_interval_seconds=0.0,
                                     layout_interval_seconds=3600.0,
                                     max_packets_per_hour=3)
    tap = RecordingTap()
    svc.feed_tap = tap
    now = time.time()
    for i in range(10):
        svc.store.add(obs(recv_ts=now, prefix=0x30 + i))
    await svc.on_packet(codec.Heartbeat(seq=1), "7eb1abc")
    svc.builder._last_pulse = time.time() - 999
    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(svc.broadcast_loop())
        try:
            assert await _wait_until(lambda: len(tap.entries) >= 4), \
                "the beat never offered its packets to the tap"
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    assert [dt for dt, _ in radio.sent] == [codec.TYPE_LAYOUT,
                                           codec.TYPE_PULSE,
                                           codec.TYPE_SECT_SUM], \
        "only the three packets the budget allowed may fly"
    assert not any(dt == codec.TYPE_CLINIC for dt, _ in radio.sent), \
        "a budget-denied batch must never reach the air"
    denied = [tx_ok for dt, _p, _w, tx_ok in tap.entries
              if dt == codec.TYPE_CLINIC]
    assert denied and denied[0] is False, \
        "the tap must be told the truth: built, not sent"
    assert any("Budget cap reached" in r.message for r in caplog.records), \
        "the denial must be loud"
    # deferred, not lost: the facts still mint a later batch
    pkt = svc.builder.build_clinic_batch(svc.clinic, svc.peer_facts,
                                         now=time.time())
    assert pkt is not None and codec.decode_any(pkt.payload).records, \
        "the denied facts must still cycle to a later beat"

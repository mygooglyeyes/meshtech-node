"""Mesh Clinic v2 - peer attribution + the restart proof.

Under test: peerfacts.py (CLINIC-WIRE.md record kind 4) fed through
service.on_packet (the real surface where peer bursts arrive), the
peer-attributed fact ROUND-TRIP (fold -> batch -> decode: source is
the peer box, never us), and the RESTART-REFILL promise: every clinic
store writes through SQLite and refills at boot, so restarts lose
nothing.

Anti-stomping rules stay untouched - folding is passive listening.
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node import codec  # noqa: E402
from meshtech_node.charts import NodeChartBook  # noqa: E402
from meshtech_node.clinic import ClinicIngest  # noqa: E402
from meshtech_node.config import Settings  # noqa: E402
from meshtech_node.flags import TroubleFlags  # noqa: E402
from meshtech_node.observations import Observation, RollingStore  # noqa: E402
from meshtech_node.peerfacts import PeerFacts  # noqa: E402
from meshtech_node.rawsource import RawPacketSource, RxPacket  # noqa: E402
from meshtech_node.service import ScopeService  # noqa: E402

PEER = 0xBEEF


def make_service():
    settings = Settings()
    settings.area.center_lat = 37.0
    settings.area.center_lon = -122.0
    settings.feed.burst_gap_seconds = 0.0
    return ScopeService(settings, use_demo=False)


def obs(**kw):
    base = dict(recv_ts=1000.0, origin_ts=None, prefix=0x21,
                lat=None, lon=None, path_prefixes=[])
    base.update(kw)
    return Observation(**base)


# ------------------------------------------------------- folding (service)

def test_peer_bursts_fold_tagged_with_who_said_it():
    async def run():
        svc = make_service()
        await svc.on_packet(codec.Pulse(seq=1, uptime_min=42, rx_per_hour=7,
                                        feed_airtime_s_per_h=3,
                                        active_total=40, origin=PEER),
                            "unknown")
        await svc.on_packet(codec.SectSum(seq=2, section_id=3,
                                          active_nodes=2, packet_count=99,
                                          delay_p50_s=5, delay_p90_s=44,
                                          origin=PEER), "unknown")
        await svc.on_packet(codec.Route(seq=3, section_id=1,
                                        route_id=0xABCD, packet_count=56,
                                        delay_med_s=4, last_heard_min=17,
                                        origin=PEER,
                                        prefixes=[0x11, 0x22]), "unknown")
        await svc.on_packet(codec.Intro(seq=4, origin=PEER, span_m=40000.0,
                                        entries=[codec.IntroEntry(
                                            prefix=0x42, name="Valley",
                                            lat=37.1, lon=-122.1)]),
                            "unknown")
        return svc
    svc = asyncio.run(run())
    reports = {(r.report, r.subject): r for r in
               svc.peer_facts.records(time.time())}
    assert reports[(codec.REPORT_PULSE, 0)].values == (42, 7, 40, 3)
    assert reports[(codec.REPORT_SECT_SUM, 3)].values == (2, 99, 5, 44)
    route = reports[(codec.REPORT_ROUTE, 0xABCD)]
    assert route.values == (56, 4, 17, 0) and route.path == (0x11, 0x22)
    intro = reports[(codec.REPORT_INTRO, 0x42)]
    assert intro.name == "Valley" and intro.cls == 0
    assert all(r.source == PEER for r in reports.values())


def test_own_echo_and_anonymous_are_counted_never_folded():
    async def run():
        svc = make_service()
        await svc.on_packet(codec.Pulse(seq=1, uptime_min=1, rx_per_hour=1,
                                        feed_airtime_s_per_h=1,
                                        active_total=1,
                                        origin=svc.origin), "unknown")
        await svc.on_packet(codec.Pulse(seq=2, uptime_min=1, rx_per_hour=1,
                                        feed_airtime_s_per_h=1,
                                        active_total=1, origin=0), "unknown")
        return svc
    svc = asyncio.run(run())
    assert svc.peer_facts.own_echo == 1
    assert svc.peer_facts.anonymous == 1
    assert svc.peer_facts.records(time.time()) == []


def test_peer_route_the_wire_cannot_carry_is_a_loud_gap():
    """9 prefixes exceed the wire's 1..8 trail: counted and dropped,
    never a truncated path pretending to be the route."""
    peers = PeerFacts(origin=0xB17E)
    long_route = codec.Route(seq=1, section_id=1, route_id=1,
                             packet_count=1, delay_med_s=1,
                             last_heard_min=1, origin=PEER,
                             prefixes=list(range(9)))
    assert peers.observe(long_route) is False
    assert peers.unreportable == 1
    assert peers.records(time.time()) == []


# --------------------------------------------- the peer-attributed round trip

def test_peer_fact_round_trips_through_the_wire():
    """Fold -> CLINIC batch -> decode: the fact comes out tagged with
    the PEER's origin (second-hand) while the packet origin is us."""
    async def run():
        svc = make_service()
        await svc.on_packet(codec.Pulse(seq=1, uptime_min=42, rx_per_hour=7,
                                        feed_airtime_s_per_h=3,
                                        active_total=40, origin=PEER),
                            "unknown")
        return svc
    svc = asyncio.run(run())
    now = time.time()
    pkt = svc.builder.build_clinic_batch(svc.clinic, svc.peer_facts, now=now)
    assert pkt is not None
    decoded = codec.decode_any(pkt.payload)
    assert decoded.origin == svc.origin          # we sent it
    peers = [r for r in decoded.records
             if isinstance(r, codec.ClinicPeerFact)]
    assert len(peers) == 1
    fact = peers[0]
    assert fact.source == PEER                   # the box that SAID it
    assert fact.source != decoded.origin         # visibly second-hand
    assert fact.values == (42, 7, 40, 3)


# ------------------------------------------------------- restart-refill

def test_restart_refill_loses_nothing(tmp_path):
    """The full clinic writes through SQLite as learned and refills at
    boot: charts, trouble flags, peer reports and the route delay
    spread all survive a restart."""
    from meshtech_node.node_store import NodeStore

    db = NodeStore(str(tmp_path / "n.db"))
    charts = NodeChartBook(origin=0xB17E)
    trouble = TroubleFlags(origin=0xB17E)
    clinic = ClinicIngest(charts, trouble)
    peers = PeerFacts(origin=0xB17E)
    store = RollingStore(window_seconds=3600.0)
    store.disk = db
    charts.disk = db
    trouble.disk = db
    peers.disk = db
    store.on_observed = clinic.observe

    # what the box experiences: traffic + a rejected advert + a peer
    for i, delay in enumerate([2.0, 10.0, 6.0]):
        store.add(obs(recv_ts=1000.0 + i, origin_ts=1000.0 + i - delay,
                      rssi=-80.0, snr=3.0,
                      path_prefixes=[0x11, 0x22]))
    src = RawPacketSource(None, channels=[], flag_sink=clinic)
    garbage = bytes([0x42]) + b"\x11" * 31 + b"\x22" * 20
    for _ in range(2):
        src.handle_packet(
            RxPacket(data=bytes([0x10, 0x34, 0x12, 0x56, 0x78, 0x00])
                     + garbage))
    peers.observe(codec.Pulse(seq=1, uptime_min=42, rx_per_hour=7,
                              feed_airtime_s_per_h=3, active_total=40,
                              origin=PEER))
    db.close()

    # the restart: fresh RAM, boot refill from the same file
    db = NodeStore(str(tmp_path / "n.db"))
    charts2 = NodeChartBook(origin=0xB17E)
    trouble2 = TroubleFlags(origin=0xB17E)
    clinic2 = ClinicIngest(charts2, trouble2)
    peers2 = PeerFacts(origin=0xB17E)
    store2 = RollingStore(window_seconds=3600.0)
    assert clinic2.refill(db) == 2              # chart + flag
    assert peers2.refill(db.peer_report_rows()) == 1
    assert store2.refill_routes(db.route_rows()) == 1

    # the chart is whole again (strip, signal facts, hops)
    chart = charts2.charts[0x21]
    assert chart.sig["rssi"].n == 3 and chart.sig["rssi"].best == -80.0
    assert chart.strip(1000.0) & (1 << 23)
    assert chart.hops_typ() == 3                # trail of 2 + 1
    # the flag row survived with its evidence
    flags = {(f.flag, f.subject): f for f in trouble2.records(1000.0)}
    assert flags[(codec.FLAG_SIG_FAIL, 0x42)].events == 2
    # the peer report survived, still tagged with who said it
    reports = peers2.records(1000.0)
    assert reports[0].source == PEER and reports[0].values == (42, 7, 40, 3)
    # the route's delay spread survived
    (path, entry), = store2.routes_all()
    assert entry["dmin"] == 2.0 and entry["dmax"] == 10.0
    assert entry["count"] == 3
    db.close()

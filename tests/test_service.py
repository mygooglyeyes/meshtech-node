"""Service tests with a fake radio link (no meshcore, no network)."""
import asyncio
import struct
import time

import pytest

from meshtech_node import codec
from meshtech_node.config import Settings
from meshtech_node.packetsource import DemoSource
from meshtech_node.service import ScopeService
from meshtech_node.client import CompanionClient


class FakeRadio:
    """Stands in for CompanionClient's link: records every send."""

    def __init__(self):
        self.sent = []

    async def send_channel_data(self, data_type: int, payload: bytes) -> bool:
        self.sent.append((data_type, bytes(payload)))
        return True


def make_service(**feed_overrides):
    settings = Settings()
    settings.area.center_lat = 37.0
    settings.area.center_lon = -122.0
    settings.feed.burst_gap_seconds = 0.0   # tests: no real sleeps
    for key, value in feed_overrides.items():
        setattr(settings.feed, key, value)
    return ScopeService(settings, use_demo=True)


def test_demo_source_is_deterministic():
    a = DemoSource(seed=42)
    b = DemoSource(seed=42)
    ta, tb = a.tick(1000.0), b.tick(1000.0)
    assert [(o.prefix, o.path_prefixes) for o in ta] == \
        [(o.prefix, o.path_prefixes) for o in tb]
    assert len(ta) == 8


def test_demo_delay_is_honest():
    src = DemoSource(seed=42)
    now = 1000.0
    for obs in src.tick(now):
        assert obs.delay_s is not None
        assert 0.0 <= obs.delay_s < 10.0


def test_refresh_request_full_path():
    """Uplink -> dedupe -> rate limit -> response burst, end to end."""
    async def run():
        svc = make_service()
        radio = FakeRadio()
        svc.client = radio
        # seed traffic in the centre section through a known route
        now = time.time()
        for i in range(5):
            svc.store.add(type("O", (), {
                "recv_ts": now, "origin_ts": now - 1.5, "prefix": 0x21,
                "lat": 37.0, "lon": -122.0,
                "path_prefixes": [0x11, 0x12],
                "channel_name": None,
                "delay_s": 1.5})())
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=5, nonce=99)   # centre square (v1.2)
        await svc.on_packet(req, "aabbccddeeff")
        types = [t for t, _ in radio.sent]
        assert codec.TYPE_SECT_SUM in types
        assert codec.TYPE_ROUTE in types
        # duplicate request (same nonce) -> nothing new on air
        before = len(radio.sent)
        await svc.on_packet(req, "aabbccddeeff")
        assert len(radio.sent) == before
    asyncio.run(run())


def test_own_layout_echo_counted_and_spoken_at_debug(caplog):
    """v0.0.051 (the "33 unexplained decodes" hunt): our own cadence
    layout coming back through a companion re-flood (or an anonymous
    one) dropped in silence - counted + DEBUG now, plus a stop line
    stating both totals."""
    import logging as _logging
    async def run():
        svc = make_service()
        mine = codec.Layout(seq=1, grid=3, center_lat=37.0,
                            center_lon=-122.0, span_m=40000,
                            origin=svc.origin)
        theirs = codec.Layout(seq=2, grid=3, center_lat=38.0,
                              center_lon=-121.0, span_m=40000,
                              origin=0xBEEF)
        anon = codec.Layout(seq=3, grid=3, center_lat=37.0,
                            center_lon=-122.0, span_m=40000,
                            origin=0)
        with caplog.at_level(_logging.DEBUG,
                             logger="meshtech-node.service"):
            await svc.on_packet(mine, "unknown")
            await svc.on_packet(theirs, "unknown")
            await svc.on_packet(anon, "unknown")
        assert svc._layout_echo == 2      # own echo + anonymous
        assert svc._peer_layouts == 1     # the real peer observed
        assert any("own echo or anonymous" in r.message
                   for r in caplog.records)
    asyncio.run(run())


def test_rate_limited_client_gets_nothing():
    async def run():
        svc = make_service(refresh_cooldown_seconds=3600.0)
        radio = FakeRadio()
        svc.client = radio
        svc.rate.record("aabbccddeeff")   # just asked
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=4, nonce=123)
        await svc.on_packet(req, "aabbccddeeff")
        assert radio.sent == []
    asyncio.run(run())


def test_acl_blocks_strangers():
    async def run():
        svc = make_service(allowed_prefixes=["aabb"])
        radio = FakeRadio()
        svc.client = radio
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=4, nonce=7)
        await svc.on_packet(req, "ffff00000000")
        assert radio.sent == []
    asyncio.run(run())


def test_budget_refusal_leaves_honest_gap():
    async def run():
        # duty cap so tiny that even one packet's airtime exceeds it
        svc = make_service(max_duty_percent=0.0005, max_packets_per_hour=1000)
        radio = FakeRadio()
        svc.client = radio
        now = time.time()
        for i in range(5):
            svc.store.add(type("O", (), {
                "recv_ts": now, "origin_ts": now - 1.0, "prefix": 0x31,
                "lat": 37.0, "lon": -122.0, "path_prefixes": [0x11],
                "channel_name": None, "delay_s": 1.0})())
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=4, nonce=55)
        await svc.on_packet(req, "aabbccddeeff")
        # The limiter refuses everything -> zero TXs, no fake sends.
        assert radio.sent == []
    asyncio.run(run())


# ---------------------------------------------------------------------------
# SOURCE TRUTH (2026-09-18): advert-row ingest + mapping
# (shapes mined from openhop_repeater-dev; BENCH-CHECKLIST section G)
# ---------------------------------------------------------------------------

# NODE: RepeaterApiSource is NOT seeded (SEED-MAP - the node IS the
# listener). The source-dependent tests below are marked skip with the
# reason; the node-side equivalent proofs live in test_rawsource.py and
# BENCH-CHECKLIST section G.


def _node_row(pubkey="ab12cd34", lat=37.1, lon=-122.2, name="Hilltop",
              extra=None):
    # Advert-row shape: pubkey full hex, node_name, latitude/longitude,
    # contact_type STRING, is_repeater, last_seen (epoch seconds).
    row = {"pubkey": pubkey, "node_name": name,
           "latitude": lat, "longitude": lon,
           "contact_type": "Unknown", "is_repeater": False,
           "last_seen": time.time()}
    if extra:
        row.update(extra)
    return row


def test_ingest_node_rows_maps_class_and_position():
    svc = make_service()
    svc._ingest_node_rows([
        _node_row(extra={"contact_type": "Repeater", "is_repeater": True}),
        # distinct names: the 2026-09-23 twin-merge collapses identities
        # sharing a name AND a position (one node, two prefixes), and
        # these rows are three DIFFERENT test nodes.
        _node_row(pubkey="cd00", name="Chat Node",
                  extra={"contact_type": "Chat Node", "is_repeater": False}),
        # unknown spelling: class stays absent, node still enriched
        _node_row(pubkey="ee01", name="Mystery Node",
                  extra={"contact_type": "Mystery Type"}),
    ])
    info = svc.store.node_info(0xAB)
    assert info is not None and info["lat"] == 37.1
    assert info["node_class"] == 1
    assert svc.store.node_info(0xCD)["node_class"] == 2
    # ee01 got a position but NO fabricated class
    ee = svc.store.node_info(0xEE)
    assert ee["lat"] == 37.1
    assert "node_class" not in ee


def test_ingest_node_rows_unknown_never_overwrites_known_class():
    svc = make_service()
    svc.store.add_node_class(0xAB, 1)          # known from earlier evidence
    svc._ingest_node_rows([_node_row(extra={"contact_type": "Unknown"})])
    assert svc.store.node_info(0xAB)["node_class"] == 1
    # ...but newer real evidence does update
    svc._ingest_node_rows([_node_row(extra={"contact_type": "Chat Node",
                                            "is_repeater": False})])
    assert svc.store.node_info(0xAB)["node_class"] == 2


def test_ingest_node_rows_room_server_is_neither_class():
    # Room servers/sensors are their own MeshCore classes; the feed's
    # two-bit INTRO class must NOT claim them as repeater/companion.
    svc = make_service()
    svc._ingest_node_rows([_node_row(pubkey="ee01",
                                     extra={"contact_type": "Room Server",
                                            "is_repeater": False})])
    assert "node_class" not in svc.store.node_info(0xEE)


def test_ingest_node_rows_survives_garbage():
    svc = make_service()
    svc._ingest_node_rows(None)
    svc._ingest_node_rows([None, "not-a-row", 42])
    # malformed position: no crash, no fabricated position, class still kept
    svc._ingest_node_rows([_node_row(lat="not-a-number",
                                     extra={"contact_type": "Repeater",
                                            "is_repeater": True})])
    info = svc.store.node_info(0xAB)
    assert info is not None and "lat" not in info
    assert info["node_class"] == 1


def test_ingest_node_rows_stale_advert_not_published():
    svc = make_service()
    stale = _node_row(pubkey="99aa",
                      extra={"last_seen": time.time() - 7 * 86400})
    svc._ingest_node_rows([stale])
    # 7-day-old advert: no position published as active
    assert svc.store.node_info(0x99) in (None, {}) or \
        "lat" not in (svc.store.node_info(0x99) or {})


def test_row_prefix_shared_by_packet_and_advert_rows():
    from meshtech_node.packetsource import row_prefix_of
    # Packet rows: src_hash = 2-char UPPERCASE hex (source truth).
    assert row_prefix_of({"src_hash": "AB"}) == 0xAB
    assert row_prefix_of({"src_hash": "0x3c"}) == 0x3C
    # Advert rows: full-hex pubkey, first 2 chars = prefix.
    assert row_prefix_of({"pubkey": "AB12"}) == 0xAB
    assert row_prefix_of({"pubkey": "cd"}) == 0xCD
    assert row_prefix_of({"pubkey": "z"}) is None
    assert row_prefix_of({}) is None


class _FakeResp:
    def __init__(self, status):
        self.status = status

    async def json(self, content_type=None):
        return {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, status):
        self._status = status

    def get(self, url, headers=None, timeout=None):
        return _FakeResp(self._status)


@pytest.mark.skip(reason="RepeaterApiSource not seeded (plugin-life); "
                         "node equivalent = Adapter A tests")
def test_token_read_failure_warns_once_then_drops_to_debug(
        tmp_path, caplog, monkeypatch):
    """2026-09-18 hilltop lesson: an unreadable token file re-logged the
    same WARNING every poll cycle. Pin: first failure warns once, later
    failures log at DEBUG, and recovery re-arms so a LATER break warns
    fresh (never silent)."""
    import logging
    from meshtech_node import packetsource
    missing = tmp_path / "missing_token"
    monkeypatch.setattr(packetsource, "_TOKEN_WARNED", set())
    src = RepeaterApiSource(Settings(), lambda o: 0)
    src._cfg.token_file = str(missing)
    with caplog.at_level(logging.DEBUG, logger="meshtech-scope.source"):
        assert packetsource._read_token(str(missing)) is None
        assert packetsource._read_token(str(missing)) is None
        assert packetsource._read_token(str(missing)) is None
    warns = [r for r in caplog.records if r.levelno == logging.WARNING
             and "token file" in r.getMessage()]
    debugs = [r for r in caplog.records if r.levelno == logging.DEBUG
              and "token file" in r.getMessage()]
    assert len(warns) == 1
    assert len(debugs) >= 1
    # Recovery re-arms the warning for a later break.
    good = tmp_path / "good_token"
    good.write_text("tok123\n", encoding="utf-8")
    assert packetsource._read_token(str(good)) == "tok123"
    assert str(good) not in packetsource._TOKEN_WARNED
    caplog.clear()
    (tmp_path / "good_token").unlink()
    with caplog.at_level(logging.DEBUG, logger="meshtech-scope.source"):
        assert packetsource._read_token(str(good)) is None
        assert packetsource._read_token(str(good)) is None
    warns2 = [r for r in caplog.records if r.levelno == logging.WARNING
              and "token file" in r.getMessage()]
    assert len(warns2) == 1  # fresh break warns fresh


@pytest.mark.skip(reason="RepeaterApiSource not seeded (plugin-life); "
                         "node equivalent = Adapter A tests")
def test_auth_failure_warns_once_then_drops_to_debug(caplog):
    import logging
    src = RepeaterApiSource(Settings(), lambda o: 0)
    with caplog.at_level(logging.DEBUG,
                         logger="meshtech-scope.source"):
        import asyncio
        async def two_auth_failures():
            await src._get(_FakeSession(403), "/recent_packets")
            await src._get(_FakeSession(403), "/recent_packets")
        asyncio.run(two_auth_failures())
    warnings = [r for r in caplog.records
                if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "auth failed" in warnings[0].getMessage()


# ---------------------------------------------------------------------------
# LIVE TRUTH (2026-09-18, hilltop rows 105713/105778): the five bench
# fixes, each pinned against the shape the real box serves.
# ---------------------------------------------------------------------------

from meshtech_node.packetsource import (  # noqa: E402
    decode_advert_payload, hop_prefix, row_prefix_of)


def _advert_row(payload_hex, **overrides):
    row = {"id": 900, "timestamp": time.time(), "src_hash": "11",
           "type": 0x04, "route": 1, "payload": payload_hex,
           "payload_length": len(payload_hex) // 2,
           "is_duplicate": 0, "drop_reason": None}
    row.update(overrides)
    return row


def _full_advert_payload(prefix=0x11, sender_ts=None, name="Hilltop",
                         lat=38.0945, lon=-122.5757, node_type=2):
    import struct
    ts = int(sender_ts if sender_ts is not None else time.time())
    app = bytes([0x10 | 0x80 | (node_type & 0x0F)])
    app += struct.pack("<ii", int(round(lat * 1e6)), int(round(lon * 1e6)))
    app += name.encode("utf-8")
    payload = bytes([prefix]) + bytes([0x11] * 31)
    payload += struct.pack("<I", ts) + bytes([0x22] * 64) + app
    return payload.hex()


def test_hop_prefix_uses_last_two_hex_chars():
    # LIVE row 105713: 4-char hop hashes, prefix byte = LAST 2 chars.
    assert hop_prefix("3E3C") == 0x3C
    assert hop_prefix("dead") == 0xAD
    assert hop_prefix("bede") == 0xDE
    # 2-char hashes and 0x-prefixes keep working; junk is honestly None.
    assert hop_prefix("AB") == 0xAB
    assert hop_prefix("0x3c") == 0x3C
    assert hop_prefix("z") is None
    assert hop_prefix(None) is None


def test_row_prefix_upstream_hash_fallback_for_null_src():
    # LIVE: flood rows have src_hash NULL + upstream_hash populated
    # (4-char, last-2 = prefix). pubkey stays FIRST-2 (full hex).
    assert row_prefix_of({"src_hash": None, "upstream_hash": "be33"}) == 0x33
    assert row_prefix_of({"upstream_hash": "3E3C"}) == 0x3C
    assert row_prefix_of({"src_hash": "AB"}) == 0xAB
    assert row_prefix_of({"pubkey": "AB12..."}) == 0xAB
    assert row_prefix_of({"src_hash": None, "upstream_hash": None}) is None


@pytest.mark.skip(reason="RepeaterApiSource not seeded (plugin-life); "
                         "node equivalent = Adapter A tests")
def test_duplicate_rows_skipped_in_mapping():
    src = RepeaterApiSource(Settings(), lambda o: 0)
    rows = [_advert_row(_full_advert_payload(), id=1),
            _advert_row(_full_advert_payload(), id=2,
                        is_duplicate=1, drop_reason="Duplicate")]
    obs = src.observations_from_packets(rows)
    assert len(obs) == 1  # the twin never becomes a second observation


def test_advert_payload_decode_full_and_partial():
    # Full layout decodes name/position/class/sender stamp.
    d = decode_advert_payload(_full_advert_payload(sender_ts=1_789_700_503))
    assert d["prefix"] == 0x11
    assert d["sender_ts"] == 1_789_700_503.0
    assert d["node_class"] == 1            # nibble 2 repeater -> class 1
    assert abs(d["latitude"] - 38.0945) < 1e-6
    assert abs(d["longitude"] - -122.5757) < 1e-6
    assert d["node_name"] == "Hilltop"
    # Live row 105778 was a room server: class stays 0 (honest).
    room = decode_advert_payload(
        _full_advert_payload(prefix=0x5E, node_type=3, name="Rockymon"))
    assert room["node_class"] == 0 and room["node_name"] == "Rockymon"
    # Garbage never decodes - honest skip, never a partial guess.
    assert decode_advert_payload(None) is None
    assert decode_advert_payload("zz") is None
    assert decode_advert_payload("abcd") is None  # too short


@pytest.mark.skip(reason="RepeaterApiSource not seeded (plugin-life); "
                         "node equivalent = Adapter A tests")
def test_advert_rows_get_sender_delay_and_position_and_class():
    svc = make_service()
    src = RepeaterApiSource(Settings(), lambda o: 0)
    now = time.time()
    rows = [_advert_row(_full_advert_payload(sender_ts=int(now - 10)),
                        timestamp=now)]
    obs = src.observations_from_packets(rows)
    assert len(obs) == 1
    o = obs[0]
    assert o.delay_s is not None and 5.0 <= o.delay_s <= 15.0
    assert abs(o.lat - 38.0945) < 1e-6
    assert o.node_class == 1 and o.node_name == "Hilltop"
    # Ingest lands all of it in the store.
    svc._ingest_advert_rows(obs)
    info = svc.store.node_info(0x11)
    assert info["lat"] == 38.0945 and info["name"] == "Hilltop"
    assert info["node_class"] == 1


@pytest.mark.skip(reason="RepeaterApiSource not seeded (plugin-life); "
                         "node equivalent = Adapter A tests")
def test_future_sender_stamp_clamps_delay_to_unknown():
    # Clock skew honesty: a sender stamp AHEAD of receipt must never
    # publish a negative delay - it shows as unknown.
    now = time.time()
    src = RepeaterApiSource(Settings(), lambda o: 0)
    obs = src.observations_from_packets(
        [_advert_row(_full_advert_payload(sender_ts=int(now + 300)),
                     timestamp=now)])
    assert obs[0].delay_s is None


# ---------------------------------------------------------------------------
# BENCH WIRING (2026-09-18, probes C2 + D): neighbor_links -> backbone
# neighbor table; companion/contacts -> node-table supplement. Shapes
# are the LIVE ones from hilltop.
# ---------------------------------------------------------------------------

from meshtech_node.packetsource import (  # noqa: E402
    contact_row, neighbor_of)


def _neighbor_row(peer="3E", **overrides):
    row = {"peer_hash": peer, "path_hash_size": 1,
           "sample_count": 455, "duplicate_sample_count": 257,
           "first_seen": time.time() - 27000,
           "last_seen": time.time() - 8.6, "age_seconds": 8.6,
           "active": True, "last_rssi": -90.0, "last_snr": 3.75,
           "last_score": 1.0, "ewma_rssi": -88.2, "ewma_snr": 4.73,
           "ewma_score": 0.84, "best_score": 1.0, "worst_score": 0.285}
    row.update(overrides)
    return row


def _contact_row(pubkey="1a62c6aed16c2fe25d16fe39214be3947e1ed067c495f25825c4a1b485b3bb0e",
                 **overrides):
    row = {"public_key": pubkey, "name": "KO6IFX-R6", "adv_type": 2,
           "flags": 0, "out_path_len": -1,
           "last_advert_timestamp": time.time() - 2767,
           "lastmod": time.time() - 2760,
           "gps_lat": 37.663329, "gps_lon": -122.462422}
    row.update(overrides)
    return row


def test_neighbor_rows_map_to_backbone_table():
    nb = neighbor_of(_neighbor_row())
    assert nb is not None and nb.prefix == 0x3E
    assert nb.sample_count == 455 and nb.active
    assert nb.last_rssi == -90.0 and abs(nb.ewma_snr - 4.73) < 1e-9
    assert nb.best_score == 1.0 and nb.worst_score == 0.285
    # junk rows are honestly skipped
    assert neighbor_of({"peer_hash": "zz"}) is None
    assert neighbor_of({}) is None


def test_backbone_neighbors_sorted_strongest_first_and_inactive_kept():
    svc = make_service()
    svc.store.add_backbone_neighbor(neighbor_of(
        _neighbor_row("3E", ewma_rssi=-88.2)))
    svc.store.add_backbone_neighbor(neighbor_of(
        _neighbor_row("66", ewma_rssi=-70.9)))
    # inactive link: kept listed (client fades it), never dropped
    svc.store.add_backbone_neighbor(neighbor_of(
        _neighbor_row("22", ewma_rssi=-95.0, active=False)))
    listed = svc.store.backbone_neighbors()
    assert [n.prefix for n in listed] == [0x66, 0x3E, 0x22]
    assert listed[-1].active is False


def test_contact_rows_normalize_with_live_rules():
    c = contact_row(_contact_row())
    assert c["prefix"] == 0x1A
    assert c["name"] == "KO6IFX-R6" and c["node_class"] == 1
    assert abs(c["lat"] - 37.663329) < 1e-6
    # 0.0/0.0 GPS = no position, never published
    nogps = contact_row(_contact_row(gps_lat=0.0, gps_lon=0.0))
    assert nogps["lat"] is None and nogps["lon"] is None
    # room server (adv_type 3) maps to class-unknown (honest)
    room = contact_row(_contact_row(adv_type=3))
    assert room["node_class"] == 0
    # unrecognised adv_type / junk stays unknown or is skipped
    assert contact_row(_contact_row(adv_type="weird"))["node_class"] == 0
    assert contact_row({"name": "no key here"}) is None


def test_stale_contacts_rejected_fresh_ingested():
    svc = make_service()
    now = time.time()
    stale_2024 = 1731679254   # East Peak 2's real last-advert on hilltop
    extras = {"neighbors": None, "contacts": [
        _contact_row(last_advert_timestamp=now - 2767),        # fresh
        _contact_row(pubkey="c981c562" * 8,
                     last_advert_timestamp=stale_2024),        # 2024!
    ]}
    svc._ingest_extras(extras)
    fresh = svc.store.node_info(0x1A)
    assert fresh is not None and fresh.get("name") == "KO6IFX-R6"
    # stale: nothing about it may appear in the store
    assert svc.store.node_info(0xC9) in (None, {})


def test_ingest_extras_survives_garbage_and_missing_keys():
    svc = make_service()
    svc._ingest_extras(None)
    svc._ingest_extras({})
    svc._ingest_extras({"neighbors": [None, "junk", {"peer_hash": "3E"}],
                        "contacts": [None, 42, {"name": "no key"}]})
    assert [n.prefix for n in svc.store.backbone_neighbors()] == [0x3E]
    assert svc.store.known_nodes() == []   # nothing fabricated


@pytest.mark.skip(reason="poll_once is RepeaterApiSource machinery "
                         "(plugin-life); node listens, it does not poll")
def test_poll_once_unpacks_neighbor_links_envelope():
    """The live endpoint wraps rows as data.links - poll_once unwraps."""
    async def run():
        calls = []

        async def fake_get(session, path):
            calls.append(path)
            if path.startswith(RepeaterApiSource.PATH_NEIGHBORS):
                return {"links": [_neighbor_row()]}
            if path.startswith(RepeaterApiSource.PATH_CONTACTS):
                return [_contact_row()]
            return []

        src = RepeaterApiSource(Settings(), lambda o: 0)
        src._get = fake_get
        packets, nodes, extras = await src.poll_once()
        # paths are polled with ?limit= appended - match on prefixes
        assert any(c.startswith(RepeaterApiSource.PATH_NEIGHBORS)
                   for c in calls)
        assert any(c.startswith(RepeaterApiSource.PATH_CONTACTS)
                   for c in calls)
        # the {links: ...} wrapper is unwrapped into a plain list
        assert isinstance(extras["neighbors"], list)
        assert extras["neighbors"][0]["peer_hash"] == "3E"
        assert isinstance(extras["contacts"], list)
    asyncio.run(run())


# ---------------------------------------------------------------------
# TX frame truth (regression for the v0.2.2 shipped-frame bug: the box
# parsed our [0x3E, 0x00, slot, 0xFF, ...] frame as channel 0 /
# path_len=1 and sent DIRECT on the wrong channel).


def test_send_channel_frame_matches_reference_parser():
    """Frame bytes must match openhop_core _cmd_send_channel_data:
    [62, slot, path_len] + data_type(2 LE) + payload - one-byte cmd,
    no payload-length byte. Mirrors the reference test
    test_cmd_send_channel_data_valid_direct_path byte-for-byte."""
    frame = CompanionClient.build_send_channel_frame(1, 0x5301, b"\xDE\xAD\xBE")
    assert frame == bytes([62, 1, 0xFF, 0x01, 0x53, 0xDE, 0xAD, 0xBE])
    # Field extraction exactly as the reference frame server does it:
    assert frame[0] == 62                       # cmd
    assert frame[1] == 1                        # channel_idx
    assert frame[2] == 0xFF                     # path_len = flood
    data_type = int.from_bytes(frame[3:5], "little")
    assert data_type == 0x5301
    assert frame[5:] == b"\xDE\xAD\xBE"          # payload, no length byte


def test_send_channel_frame_flood_shape_no_stray_zero():
    """The old bug: a 2-byte cmd code shifted every field by one byte.
    Slot 1 flood send must NOT contain the [0x3E, 0x00] prefix pair."""
    frame = CompanionClient.build_send_channel_frame(1, 0x5302, b"x")
    assert not frame.startswith(bytes([0x3E, 0x00]))
    assert frame[1] == 1    # slot in the channel_idx position
    assert frame[2] == 0xFF  # flood marker in the path_len position


def test_send_channel_frame_rejects_bad_inputs():
    with pytest.raises(ValueError):
        CompanionClient.build_send_channel_frame(1, 0, b"x")     # type 0
    with pytest.raises(ValueError):
        CompanionClient.build_send_channel_frame(1, 0x5301, b"x" * 200)
    with pytest.raises(ValueError):
        CompanionClient.build_send_channel_frame(99, 0x5301, b"x")


# ---------------------------------------------------------------------
# Double-wrap regression (2026-09-18): the plugin passed the FULL
# scope plaintext to the radio, but the reference radio code wraps the
# command payload in its OWN type/len envelope
# (openhop_core companion_base.send_channel_data:
#  plaintext = pack('<HB', data_type, len(payload)) + payload).
# Result: two envelopes on air, every client field read 3 bytes
# shifted (the 12801-nodes / 54897-s health-card garbage).


def test_send_channel_data_strips_envelope_like_reference():
    """The command must carry the BODY only - the radio re-wraps it.
    Reproduces the exact bytes seen on air at 17:17:15 on 2026-09-18
    (outer 01531a wrapping an inner 015317) and proves the fix.
    """
    from meshtech_node.client import scope_wire_body

    full = codec.encode_pulse(codec.Pulse(
        seq=7, uptime_min=10, rx_per_hour=50, feed_airtime_s_per_h=1,
        active_total=40, origin=0x42, section_counts=[1, 2, 3]))
    assert full[:2] == b"\x01\x53" and full[2] == len(full) - 3
    body = scope_wire_body(codec.TYPE_PULSE, full)
    assert body == full[3:]                     # envelope stripped
    frame = CompanionClient.build_send_channel_frame(5, codec.TYPE_PULSE,
                                                     body)
    # Reference parser rebuild (frame_server._cmd_send_channel_data):
    data_type = int.from_bytes(frame[3:5], "little")
    cmd_payload = frame[5:]
    on_air = struct.pack("<HB", data_type, len(cmd_payload)) + cmd_payload
    assert on_air == full                       # ONE envelope, exactly ours


def test_send_channel_data_rejects_mismatched_framing():
    from meshtech_node.client import scope_wire_body

    with pytest.raises(ValueError):
        scope_wire_body(codec.TYPE_PULSE, codec.encode_layout(
            codec.Layout(seq=1, grid=3, center_lat=37.0, center_lon=-122.0,
                         span_m=8000, name="x")))


def test_rx_accepts_body_only_companion_payload():
    """The SDK delivers BODY-only in CHANNEL_DATA_RECV (reader.py:
    payload = data_len bytes; the radio strips its own envelope).
    decode_body must decode it, and the old full-plaintext path must
    still work for robustness."""
    from meshtech_node.client import CompanionClient as _C

    pulse = codec.Pulse(seq=1, uptime_min=5, rx_per_hour=10,
                        feed_airtime_s_per_h=1, active_total=42,
                        origin=0x42, section_counts=[4, 5, 6])
    full = codec.encode_pulse(pulse)
    body_only = full[3:]

    async def run():
        seen = []

        class FakeMC:
            commands = None

        client = _C.__new__(_C)
        client._slot = 5
        client._on_packet = None

        async def fake_handler(_obj, _prefix):
            seen.append(_obj)

        client._on_packet = fake_handler

        async def deliver(payload_hex, data_type):
            class E:
                payload = {"channel_idx": 5, "payload": payload_hex,
                           "data_type": data_type}
            await client._on_channel_data(E())

        # Body-only (what meshcore reader.py actually delivers).
        await deliver(body_only.hex(), codec.TYPE_PULSE)
        assert len(seen) == 1 and seen[0].active_total == 42

        # Full-plaintext shape still tolerated.
        await deliver(full.hex(), codec.TYPE_PULSE)
        assert len(seen) == 2 and seen[1].active_total == 42

    asyncio.run(run())

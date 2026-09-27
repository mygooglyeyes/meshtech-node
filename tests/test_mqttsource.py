"""MQTT collector tests (lab plan 2026-09-26, Ch5).

The seam under test: topic parse + message -> heard-by row. No broker
and no paho-mqtt needed - `handle_message` is the pure path `run()`
feeds. The wire format was verified against openhop_repeater's
PacketRecord/mqtt_handler (every field a STRING).

Honesty contract: no hash = no row (counted, never fudged); missing
RSSI/SNR stays NULL; one bad message never kills the collection.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.mqttsource import MqttCollector  # noqa: E402
from meshtech_node.node_store import NodeStore  # noqa: E402


class _Cfg:
    """Minimal MqttCfg stand-in."""

    def __init__(self, **kw):
        self.enabled = kw.get("enabled", True)
        self.host = kw.get("host", "127.0.0.1")
        self.port = kw.get("port", 1883)
        self.topic = kw.get("topic", "meshcore/#")
        self.regions = kw.get("regions", [])


def _collector(tmp_path):
    store = NodeStore(str(tmp_path / "mqtt.db"))
    return MqttCollector(_Cfg(), store), store


def _packet_msg(**over):
    """One PacketRecord-shaped payload (all fields strings)."""
    msg = {
        "origin": "HilltopObs",
        "origin_id": "aabbccdd",
        "timestamp": "2026-09-26T12:00:00",
        "type": "PACKET",
        "direction": "rx",
        "time": "12:00:00",
        "date": "26/9/2026",
        "len": "48",
        "packet_type": "4",
        "route": "F",
        "payload_len": "32",
        "raw": "11223344",
        "SNR": "9.5",
        "RSSI": "-81",
        "score": "1234",
        "duration": "52",
        "hash": "AB12CD",
    }
    msg.update(over)
    return json.dumps(msg).encode("utf-8")


# ----------------------------------------------------------- topic parse --

def test_parse_topic_extracts_the_triple():
    assert MqttCollector.parse_topic(
        "meshcore/SFO/aabbccdd/packets") == ("SFO", "aabbccdd", "packets")
    assert MqttCollector.parse_topic(
        "meshcore/LAX/eeff/packet") == ("LAX", "eeff", "packet")


def test_parse_topic_skips_everything_that_is_not_observer_coverage():
    for topic in ("meshcore/status", "meshcore/events/connection",
                  "meshcore/client/eeff/packets",     # mobile client path
                  "meshcore/client/eeff/rf",
                  "other/SFO/aabb/packets",           # wrong family
                  "meshcore/SFO",                     # too short
                  ""):
        assert MqttCollector.parse_topic(topic) is None, topic


# -------------------------------------------------------- handle_message --

def test_packet_message_becomes_a_heard_by_row(tmp_path):
    col, store = _collector(tmp_path)
    ok = col.handle_message("meshcore/SFO/obs1/packets", _packet_msg())
    assert ok
    rows = store.heard_by_rows("AB12CD")
    assert len(rows) == 1
    row = rows[0]
    assert row["observer_id"] == "obs1"     # from the TOPIC (authority)
    assert row["region"] == "SFO"
    assert row["rssi"] == -81.0
    assert row["snr"] == 9.5
    assert row["hear_count"] == 1
    assert row["last_heard"] > 0


def test_legacy_singular_topic_counts_too(tmp_path):
    col, store = _collector(tmp_path)
    assert col.handle_message("meshcore/SFO/obs1/packet", _packet_msg())
    assert store.heard_by_count() == 1


def test_same_packet_two_observers_is_the_coverage(tmp_path):
    col, store = _collector(tmp_path)
    col.handle_message("meshcore/SFO/obs1/packets", _packet_msg())
    col.handle_message("meshcore/LAX/obs2/packets", _packet_msg(
        RSSI="-95", SNR="1.5"))
    assert store.heard_by_count() == 2      # one row per (hash, observer)
    coverage = store.coverage_rows(min_hearers=2)
    assert len(coverage) == 1
    assert {h["observer_id"] for h in coverage[0]["hearers"]} == \
        {"obs1", "obs2"}


def test_message_without_hash_is_counted_not_fudged(tmp_path):
    col, store = _collector(tmp_path)
    ok = col.handle_message("meshcore/SFO/obs1/packets",
                            _packet_msg(hash=""))
    assert not ok
    assert store.heard_by_count() == 0      # no identity, no row
    assert col.stats["no_hash"] == 1


def test_missing_signal_stays_null(tmp_path):
    col, store = _collector(tmp_path)
    msg = _packet_msg()
    payload = json.loads(msg)
    del payload["RSSI"]
    del payload["SNR"]
    assert col.handle_message("meshcore/SFO/obs1/packets",
                              json.dumps(payload).encode())
    row = store.heard_by_rows("AB12CD")[0]
    assert row["rssi"] is None
    assert row["snr"] is None               # gap, never a constant


def test_bad_messages_never_kill_the_collector(tmp_path):
    col, store = _collector(tmp_path)
    assert not col.handle_message("meshcore/SFO/obs1/packets", b"{not json")
    assert not col.handle_message("meshcore/SFO/obs1/packets", b"[1,2,3]")
    assert not col.handle_message("meshcore/SFO/obs1/packets",
                                  b"\xff\xfe broken")
    assert col.stats["bad_payload"] == 3
    # and the collector still collects
    assert col.handle_message("meshcore/SFO/obs1/packets", _packet_msg())
    assert store.heard_by_count() == 1


def test_metadata_topics_never_enter_the_table(tmp_path):
    col, store = _collector(tmp_path)
    status = json.dumps({"noise_floor": -104}).encode()
    assert not col.handle_message("meshcore/SFO/obs1/status", status)
    assert not col.handle_message("meshcore/SFO/obs1/neighbors", b"{}")
    assert not col.handle_message("meshcore/client/eeff/packets",
                                  _packet_msg())
    assert store.heard_by_count() == 0
    assert col.stats["skipped"] == 3


# --------------------------------------------- Ch6: the region filter ----

def test_region_filter_keeps_only_the_listed_regions(tmp_path):
    store = NodeStore(str(tmp_path / "mqtt.db"))
    col = MqttCollector(_Cfg(regions=["SFO", "LAX"]), store)
    # wide view subscribed whole; the filter decides what is RECORDED
    assert col.handle_message("meshcore/SFO/obs1/packets", _packet_msg())
    assert col.handle_message("meshcore/lax/obs2/packets",
                              _packet_msg(hash="ZZ99"))
    assert not col.handle_message("meshcore/SEA/obs3/packets",
                                  _packet_msg(hash="YY88"))
    assert store.heard_by_count() == 2
    assert col.stats["region_dropped"] == 1
    # the dropped region warns ONCE, then only counts
    assert not col.handle_message("meshcore/SEA/obs4/packets",
                                  _packet_msg(hash="YY88"))
    assert col.stats["region_dropped"] == 2


def test_region_filter_empty_list_accepts_every_region(tmp_path):
    col, store = _collector(tmp_path)     # regions=[] = no filter
    assert col.handle_message("meshcore/SEA/obs3/packets", _packet_msg())
    assert col.handle_message("meshcore/XXX/obs4/packets",
                              _packet_msg(hash="Q1"))
    assert store.heard_by_count() == 2
    assert col.stats["region_dropped"] == 0

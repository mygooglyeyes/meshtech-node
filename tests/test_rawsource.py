"""Adapter A tests - the pipeline as the brain will consume it.

The scripted set: N nodes, M duplicates, K distinct - the counters
must match the script exactly (BENCH-CHECKLIST.md dedupe script).
Crypto-backed cases rely on the reference library (conftest.py path);
they use the same golden vectors as test_packets.py.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from meshtech_node.packets import (  # noqa: E402
    PAYLOAD_TYPE_GRP_DATA, FloodDedupe,
)
from meshtech_node.rawsource import (  # noqa: E402
    RawPacketSource, ReplayTransport, RxPacket,
)

pymc = pytest.importorskip("pymc_core", reason="reference library needed "
                                               "for crypto-backed cases")

from meshtech_node.packets import ChannelKeys  # noqa: E402, F811

SCOPE_HASH = 0x39
GOLDEN_CT = bytes.fromhex(
    "d28e60ccfdf9984883ba5953e9c550aa409864a430c85667419ffa975edbf943")
GOLDEN_MAC = bytes.fromhex("ca92")

ADVERT_BODY = (bytes([0x42]) + b"\x11" * 31
               + (1234567890).to_bytes(4, "little")
               + b"\x22" * 64
               + bytes([0x90])
               + (41700000).to_bytes(4, "little", signed=True)
               + (-111800000).to_bytes(4, "little", signed=True)
               + b"LoganPeak")

# A REAL signed advert (generated at import with the reference-signed
# recipe): the signature gate must let THIS through.
from nacl.signing import SigningKey  # noqa: E402
_ADVERT_KEY = SigningKey(b"\x01" * 32)   # deterministic seed
_SIGNED_PUBKEY = bytes(_ADVERT_KEY.verify_key)

def _signed_advert_body() -> bytes:
    appdata = bytes([0x90]) + (41700000).to_bytes(4, "little", signed=True) \
        + (-111800000).to_bytes(4, "little", signed=True) + b"LoganPeak"
    ts = (1234567890).to_bytes(4, "little")
    sig = _ADVERT_KEY.sign(_SIGNED_PUBKEY + ts + appdata).signature
    return _SIGNED_PUBKEY + ts + sig + appdata


ADVERT_BODY_SIGNED = _signed_advert_body()


def _frame(payload_type: int, route: int, payload: bytes) -> bytes:
    header = (0 << 6) | (payload_type << 2) | route
    out = bytes([header])
    if route in (0, 3):
        out += (0x1122).to_bytes(2, "little") + (0x3344).to_bytes(2, "little")
    return out + bytes([0x00]) + payload       # path_len 0, no path


def _scope_group_payload() -> bytes:
    return bytes([SCOPE_HASH]) + GOLDEN_MAC + GOLDEN_CT


def _source(items, **kw):
    channels = [ChannelKeys.from_secret("#scope", "#scope")]
    return RawPacketSource(ReplayTransport(items), channels=channels, **kw)


# ----------------------------------------------------------- scripted ----

def test_scripted_set_counters_exact():
    """N=3 distinct adverts, each heard M=3 times -> decoded=3, dup=6."""
    # three DISTINCT signed identities (three real keypairs), each
    # heard per the script - dedupe math is what's under test.
    keys = [SigningKey(bytes([i]) * 32) for i in (1, 2, 3)]
    def _body(k: SigningKey) -> bytes:
        pk = bytes(k.verify_key)
        appdata = bytes([0x90]) + (41700000).to_bytes(4, "little",
                                                      signed=True) \
            + (-111800000).to_bytes(4, "little", signed=True) + b"LoganPeak"
        ts = (1234567890).to_bytes(4, "little")
        return pk + ts + k.sign(pk + ts + appdata).signature + appdata
    b1, b2, b3 = (_body(k) for k in keys)
    frames = [_frame(0x04, 0, b1),
              _frame(0x04, 0, b1),
              _frame(0x04, 0, b1),
              _frame(0x04, 1, b2),
              _frame(0x04, 1, b2),
              _frame(0x04, 1, b3)]
    src = _source([RxPacket(data=f) for f in frames])
    obs = [src.handle_packet(RxPacket(data=f)) for f in frames]
    assert [o is not None for o in obs] == [True, False, False,
                                            True, False, True]
    s = src.stats
    assert (s.received, s.decoded, s.duplicates, s.malformed) == (6, 3, 3, 0)


def test_advert_observation_fields():
    src = _source([])
    obs = src.handle_packet(RxPacket(data=_frame(0x04, 0, ADVERT_BODY_SIGNED),
                                     rssi=-77, snr=9.5))
    assert obs is not None
    assert obs.prefix == _SIGNED_PUBKEY[0]
    assert obs.node_name == "LoganPeak"
    assert obs.node_class == 0
    assert abs(obs.lat - 41.7) < 0.001
    assert abs(obs.lon - (-111.8)) < 0.001
    assert obs.origin_ts == 1234567890.0
    assert obs.channel_name is None
    assert obs.path_prefixes == []          # 0 hops


def test_group_observation_fields_and_dedupe():
    src = _source([])
    data = _frame(0x06, 0, _scope_group_payload())
    obs = src.handle_packet(RxPacket(data=data))
    assert obs is not None
    assert obs.channel_name == "#scope"
    assert obs.prefix == 0                  # no sender identity on wire
    assert obs.origin_ts == 1234567890.0
    assert src.handle_packet(RxPacket(data=data)) is None   # flood repeat
    assert src.stats.duplicates == 1


def test_malformed_counted_not_crashed():
    src = _source([])
    assert src.handle_packet(RxPacket(data=b"\x14\x00")) is None   # truncated
    assert src.handle_packet(RxPacket(data=b"")) is None
    assert src.stats.malformed == 2
    assert src.stats.received == 2


# --------------------------------------------------- signature gate ----

def test_corrupt_advert_rejected_counted_never_stored():
    """THE duplicate-dots fix (Brett, 2026-09-23): a bit-flipped advert
    (mojibake name, nonsense position) is rejected at the gate -
    counted in stats.corrupt, NO observation, NO repeater promotion.
    The scripted ADVERT_BODY above has a fabricated signature (0x22*64
    filler), so it doubles as the corrupt specimen here."""
    src = _source([])
    assert src.handle_packet(RxPacket(data=_frame(0x04, 0, ADVERT_BODY))) \
        is None
    assert src.stats.corrupt == 1
    assert src.stats.decoded == 0
    assert "corrupt 1" in src.stats_line()


def test_signed_advert_still_flows_through_the_gate():
    """The gate must never bite honest adverts: a properly signed one
    (reference-signed, same recipe) decodes into an observation as
    before."""
    from nacl.signing import SigningKey
    key = SigningKey.generate()
    pubkey = bytes(key.verify_key)
    appdata = bytes([0x90]) + (41700000).to_bytes(4, "little", signed=True) \
        + (-111800000).to_bytes(4, "little", signed=True) + b"RealNode"
    ts = (1234567890).to_bytes(4, "little")
    sig = key.sign(pubkey + ts + appdata).signature
    payload = pubkey + ts + sig + appdata
    src = _source([])
    obs = src.handle_packet(RxPacket(data=_frame(0x04, 0, payload)))
    assert obs is not None
    assert obs.prefix == pubkey[0]
    assert obs.node_name == "RealNode"
    assert src.stats.corrupt == 0


def test_foreign_channel_undecodable_honest():
    src = _source([])
    data = _frame(0x06, 0, bytes([0xAA]) + GOLDEN_MAC + GOLDEN_CT)
    obs = src.handle_packet(RxPacket(data=data))
    # PROJECT.md rule 3: payload honestly unreadable, but the header
    # is recorded - an observation WITH the (empty, path_len 0) header
    # facts comes back, not None.
    assert obs is not None
    assert obs.prefix == 0            # sender honestly unknown
    assert obs.lat is None and obs.lon is None
    assert obs.channel_name is None
    assert src.stats.undecodable == 1
    assert src.stats.decoded == 0


def test_ignored_types_counted():
    src = _source([])
    obs = src.handle_packet(RxPacket(data=_frame(0x02, 2, b"\x01\x02")))
    # PROJECT.md rule 3: DMs/room traffic keep their payload unread
    # (not a chat bot) but the header becomes an observation.
    assert obs is not None
    assert obs.prefix == 0
    assert obs.lat is None and obs.lon is None
    assert src.stats.ignored_type == 1


def test_ignored_type_header_path_recorded():
    # A DM routed through two repeaters: path_len byte encodes hash
    # size 1 (top bits 00) and count 2 -> path = 2 x 1-byte hashes.
    # _frame() writes path_len 0; build this one by hand: header,
    # path_len(2), the two hashes, then the payload.
    src = _source([])
    header = bytes([(0x02 << 2) | 0x02])     # version 0, type 0x02, route 2
    data = header + bytes([0x02, 0xAB, 0xCD]) + b"\x01\x02"
    obs = src.handle_packet(RxPacket(data=data))
    assert obs is not None
    assert obs.path_prefixes == [0xAB, 0xCD]   # the route trail survives
    assert obs.prefix == 0


def test_foreign_channel_header_path_recorded():
    # Same for foreign-channel group traffic: undecodable payload,
    # recorded route trail.
    src = _source([])
    payload = bytes([0xAA]) + GOLDEN_MAC + GOLDEN_CT
    header = bytes([(0x06 << 2) | 0x02])
    data = header + bytes([0x02, 0x11, 0x22]) + payload
    obs = src.handle_packet(RxPacket(data=data))
    assert obs is not None
    assert src.stats.undecodable == 1
    assert obs.path_prefixes == [0x11, 0x22]


# -------------------------------------------------------------- async ----

def test_run_feeds_queue_in_order_and_stops():
    async def scenario():
        frames = [RxPacket(data=_frame(0x04, 0, ADVERT_BODY_SIGNED)),
                  RxPacket(data=_frame(0x04, 0, ADVERT_BODY_SIGNED)),   # dup
                  RxPacket(data=_frame(0x06, 0, _scope_group_payload()))]
        src = _source(frames)
        queue: asyncio.Queue = asyncio.Queue()
        stop = asyncio.Event()
        await src.run(queue, stop)
        out = []
        while not queue.empty():
            out.append(queue.get_nowait())
        return out, src.stats

    out, stats = asyncio.run(scenario())
    assert len(out) == 2                       # advert + group, dup dropped
    assert out[0].node_name == "LoganPeak"
    assert out[1].channel_name == "#scope"
    assert stats.decoded == 2 and stats.duplicates == 1

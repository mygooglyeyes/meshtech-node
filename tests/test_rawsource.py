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
    frames = [_frame(0x04, 0, ADVERT_BODY),
              _frame(0x04, 0, ADVERT_BODY),
              _frame(0x04, 0, ADVERT_BODY),
              _frame(0x04, 1, b"\x77" + b"\x11" * 31
                     + (999).to_bytes(4, "little") + b"\x22" * 64),
              _frame(0x04, 1, b"\x77" + b"\x11" * 31
                     + (999).to_bytes(4, "little") + b"\x22" * 64),
              _frame(0x04, 1, b"\x88" + b"\x11" * 31
                     + (999).to_bytes(4, "little") + b"\x22" * 64)]
    src = _source([RxPacket(data=f) for f in frames])
    obs = [src.handle_packet(RxPacket(data=f)) for f in frames]
    assert [o is not None for o in obs] == [True, False, False,
                                            True, False, True]
    s = src.stats
    assert (s.received, s.decoded, s.duplicates, s.malformed) == (6, 3, 3, 0)


def test_advert_observation_fields():
    src = _source([])
    obs = src.handle_packet(RxPacket(data=_frame(0x04, 0, ADVERT_BODY),
                                     rssi=-77, snr=9.5))
    assert obs is not None
    assert obs.prefix == 0x42
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


def test_foreign_channel_undecodable_honest():
    src = _source([])
    data = _frame(0x06, 0, bytes([0xAA]) + GOLDEN_MAC + GOLDEN_CT)
    assert src.handle_packet(RxPacket(data=data)) is None
    assert src.stats.undecodable == 1
    assert src.stats.decoded == 0


def test_ignored_types_counted():
    src = _source([])
    assert src.handle_packet(RxPacket(data=_frame(0x02, 2, b"\x01\x02"))) is None
    assert src.stats.ignored_type == 1


# -------------------------------------------------------------- async ----

def test_run_feeds_queue_in_order_and_stops():
    async def scenario():
        frames = [RxPacket(data=_frame(0x04, 0, ADVERT_BODY)),
                  RxPacket(data=_frame(0x04, 0, ADVERT_BODY)),   # dup
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

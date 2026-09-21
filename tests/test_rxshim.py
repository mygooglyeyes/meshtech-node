"""RX shim tests - heard frames -> decrypt -> codec -> on_packet.

The end-to-end proof: an ENCRYPTED scope packet arriving as a heard
GRP_DATA frame reaches the brain and is answered - the exact path
that never worked under the openhop repeater (the 2026-09-18
refresh-request deaths). Crypto needs the reference library
(conftest.py path).
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

pytest.importorskip("pymc_core", reason="reference library needed for "
                                        "crypto-backed cases")

from pymc_core.protocol.crypto import CryptoUtils  # noqa: E402

from meshtech_node import codec  # noqa: E402
from meshtech_node.packets import ChannelKeys  # noqa: E402
from meshtech_node.rawsource import RawPacketSource, ReplayTransport, RxPacket  # noqa: E402
from meshtech_node.rxshim import wire_scope_rx  # noqa: E402

from test_service import FakeRadio, make_service  # noqa: E402

SCOPE_HASH = 0x39
_AES = bytes.fromhex("2373636f706500000000000000000000")
_HMAC_KEY = _AES + b"\x00" * 16


def _frame(payload_type: int, route: int, payload: bytes,
           hops: int = 0, hash_size: int = 1, path: bytes = b"") -> bytes:
    header = (0 << 6) | (payload_type << 2) | route
    out = bytes([header])
    if route in (0, 3):
        out += (0x1122).to_bytes(2, "little") + (0x3344).to_bytes(2, "little")
    out += bytes([((hash_size - 1) << 6) | (hops & 0x3F)]) + path
    return out + payload


def _encrypt_scope(plaintext: bytes) -> bytes:
    """The on-air shape: ch_hash(1) | mac(2) | AES-ECB(plaintext)."""
    ct = CryptoUtils._aes_encrypt(_AES, plaintext)
    mac = CryptoUtils._hmac_sha256(_HMAC_KEY, ct)[:2]
    return bytes([SCOPE_HASH]) + mac + ct


class SpyService:
    def __init__(self):
        self.got = []

    async def on_packet(self, obj, sender_prefix):
        self.got.append((obj, sender_prefix))


def _source(spy=None, items=None):
    src = RawPacketSource(ReplayTransport(items or []),
                          channels=[ChannelKeys.from_secret("#scope", "#scope")])
    if spy is not None:
        wire_scope_rx(src, spy)
    return src


# ------------------------------------------------------- end to end ------

def test_encrypted_refresh_req_reaches_brain_and_is_answered():
    """THE proof: encrypted REFRESH_REQ heard on air -> decoded ->
    shim -> brain -> answer burst on the radio. Under openhop this
    died in the repeater's plumbing; here the frame never leaves the
    process."""
    async def scenario():
        svc = make_service()
        radio = FakeRadio()
        svc.client = radio
        # seed traffic in the centre section through a known route
        # (same fixture shape as the plugin's refresh test, test_service.py)
        now = time.time()
        for i in range(5):
            svc.store.add(type("O", (), {
                "recv_ts": now, "origin_ts": now - 1.5, "prefix": 0x21,
                "lat": 37.0, "lon": -122.0,
                "path_prefixes": [0x11, 0x12],
                "channel_name": None,
                "delay_s": 1.5})())
        src = _source(spy=svc)
        req = codec.RefreshReq(seq=1, kind=codec.REFRESH_KIND_SECTION,
                               target=5, nonce=99)   # centre square (v1.2)
        data = _frame(0x06, 0, _encrypt_scope(codec.encode_refresh_req(req)))
        obs = src.handle_packet(RxPacket(data=data))
        assert obs is not None            # the frame ALSO feeds the store
        pending = [t for t in asyncio.all_tasks() if t is not
                   asyncio.current_task()]
        await asyncio.gather(*pending)
        types = [t for t, _ in radio.sent]
        assert codec.TYPE_SECT_SUM in types
        assert codec.TYPE_ROUTE in types
        # flood repeat of the same request: source dedupe, nothing new
        before = len(radio.sent)
        assert src.handle_packet(RxPacket(data=data)) is None
        pending = [t for t in asyncio.all_tasks() if t is not
                   asyncio.current_task()]
        await asyncio.gather(*pending)
        assert len(radio.sent) == before
        assert src.stats.duplicates == 1

    asyncio.run(scenario())


# ------------------------------------------------------------- shim ------

def test_on_packet_receives_decoded_scope_object():
    async def scenario():
        spy = SpyService()
        src = _source(spy=spy)
        pulse = codec.Pulse(seq=7, uptime_min=12, rx_per_hour=100,
                            feed_airtime_s_per_h=3, active_total=58,
                            origin=0x1234)
        data = _frame(0x06, 0, _encrypt_scope(codec.encode_pulse(pulse)))
        assert src.handle_packet(RxPacket(data=data)) is not None
        pending = [t for t in asyncio.all_tasks() if t is not
                   asyncio.current_task()]
        await asyncio.gather(*pending)
        assert len(spy.got) == 1
        obj, prefix = spy.got[0]
        assert isinstance(obj, codec.Pulse)
        assert obj.seq == 7 and obj.origin == 0x1234
        assert prefix == "unknown"        # anonymous uplink honesty

    asyncio.run(scenario())


def test_non_scope_plaintext_dropped_but_counted():
    async def scenario():
        spy = SpyService()
        src = _source(spy=spy)
        # valid HMAC for #scope, but another app's data type (0x1234)
        plaintext = (0x1234).to_bytes(2, "little") + bytes([2]) + b"hi"
        data = _frame(0x06, 0, _encrypt_scope(plaintext))
        assert src.handle_packet(RxPacket(data=data)) is not None
        pending = [t for t in asyncio.all_tasks() if t is not
                   asyncio.current_task()]
        await asyncio.gather(*pending)
        assert spy.got == []
        assert src.stats.non_scope == 1

    asyncio.run(scenario())


def test_bad_scope_body_not_delivered():
    async def scenario():
        spy = SpyService()
        src = _source(spy=spy)
        bad = codec.TYPE_PULSE.to_bytes(2, "little") + bytes([99]) + b"\x00" * 9
        data = _frame(0x06, 0, _encrypt_scope(bad))
        assert src.handle_packet(RxPacket(data=data)) is not None
        pending = [t for t in asyncio.all_tasks() if t is not
                   asyncio.current_task()]
        await asyncio.gather(*pending)
        assert spy.got == []              # dropped honestly, no crash

    asyncio.run(scenario())


def test_failing_brain_never_kills_listener():
    async def scenario():
        class Angry:
            async def on_packet(self, obj, prefix):
                raise RuntimeError("boom")

        src = _source(spy=Angry())
        pulse = codec.Pulse(seq=1, uptime_min=1, rx_per_hour=1,
                            feed_airtime_s_per_h=1, active_total=1)
        data = _frame(0x06, 0, _encrypt_scope(codec.encode_pulse(pulse)))
        obs = src.handle_packet(RxPacket(data=data))
        assert obs is not None            # listener unaffected
        pending = [t for t in asyncio.all_tasks() if t is not
                   asyncio.current_task()]
        await asyncio.gather(*pending)    # task death logged, not raised

    asyncio.run(scenario())


def test_grp_txt_never_reaches_shim():
    async def scenario():
        spy = SpyService()
        src = _source(spy=spy)
        chat = (1234567890).to_bytes(4, "little") + b"Brett: hello mesh"
        data = _frame(0x05, 0, _encrypt_scope(chat))     # GRP_TXT
        assert src.handle_packet(RxPacket(data=data)) is not None
        assert spy.got == []              # chat text is not scope traffic

    asyncio.run(scenario())


# ----------------------------------------------------- full header -------

def test_last_scope_frame_keeps_full_header():
    """BENCH-CHECKLIST full-header proof, software half: transport
    codes and path survive - nothing is stripped in our stack."""
    spy = SpyService()
    src = _source(spy=spy)
    path = bytes([0xAA, 0xBB, 0xCC, 0xDD])
    pulse = codec.Pulse(seq=2, uptime_min=2, rx_per_hour=2,
                        feed_airtime_s_per_h=2, active_total=2)
    data = _frame(0x06, 0, _encrypt_scope(codec.encode_pulse(pulse)),
                  hops=2, hash_size=2, path=path)
    assert src.handle_packet(RxPacket(data=data)) is not None
    frame = src.last_scope_frame
    assert frame is not None
    assert frame.transport_codes == (0x1122, 0x3344)
    assert frame.hops == 2 and frame.hash_size == 2
    assert frame.path == path

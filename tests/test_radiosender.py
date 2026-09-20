"""Adapter B tests - the sender's frames proven against the reference.

Headline proof: build_grp_data_frame's output parses with the
REFERENCE library's own Packet.read_from (openhop_core) and decrypts
back to the exact plaintext - plus the closed TX->RX loop through the
node's own listener. Crypto needs the reference library (conftest.py
path).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

pytest.importorskip("pymc_core", reason="reference library needed")

from pymc_core.protocol.crypto import CryptoUtils  # noqa: E402
from pymc_core.protocol.packet import Packet  # noqa: E402
from pymc_core.protocol.constants import (  # noqa: E402
    PAYLOAD_TYPE_GRP_DATA, ROUTE_TYPE_FLOOD)

from meshtech_node import codec, packets  # noqa: E402
from meshtech_node.packets import ChannelKeys, FloodDedupe  # noqa: E402
from meshtech_node.radiosender import RadioSender, build_grp_data_frame  # noqa: E402
from meshtech_node.rawsource import RawPacketSource, ReplayTransport, RxPacket  # noqa: E402

AES = bytes.fromhex("2373636f706500000000000000000000")
HMAC_KEY = AES + b"\x00" * 16
PLAINTEXT = codec.data_type_bytes(codec.TYPE_PULSE,
                                  bytes(range(10)))   # 13B scope plaintext


class FakeModem:
    def __init__(self):
        self.connected = True
        self.sent = []

    async def send(self, data: bytes) -> bool:
        self.sent.append(bytes(data))
        return True


def _sender(**kw):
    modem = FakeModem()
    sender = RadioSender(modem, ChannelKeys.from_secret("#scope", "#scope"),
                         **kw)
    return sender, modem


# ------------------------------------------------- reference parser ------

def test_frame_parses_with_reference_packet_class():
    """THE wire proof: our frame -> pymc_core Packet.read_from ->
    right type/route, HMAC verifies, decrypts to the exact plaintext."""
    frame = build_grp_data_frame(ChannelKeys.from_secret("#scope", "#scope"),
                                 PLAINTEXT)
    assert frame is not None
    pkt = Packet()
    assert pkt.read_from(frame) is True
    assert pkt.get_payload_type() == PAYLOAD_TYPE_GRP_DATA
    assert pkt.get_route_type() == ROUTE_TYPE_FLOOD
    payload = bytes(pkt.get_payload())
    assert payload[0] == 0x39                       # #scope channel hash
    mac, ciphertext = payload[1:3], payload[3:]
    assert CryptoUtils._hmac_sha256(HMAC_KEY, ciphertext)[:2] == mac
    assert CryptoUtils._aes_decrypt(AES, ciphertext)[:len(PLAINTEXT)] \
        == PLAINTEXT


def test_frame_roundtrips_through_node_rx_path():
    """Closed loop: what we send, our own listener decodes - same
    bytes, same scheme."""
    channel = ChannelKeys.from_secret("#scope", "#scope")
    frame = build_grp_data_frame(channel, PLAINTEXT)
    frameparts = packets.split_frame(frame)
    assert frameparts is not None
    assert frameparts.payload_type == 0x06
    decoded = packets.decode_group_payload(frameparts.payload, [channel])
    assert decoded is not None
    _ch, plaintext, _ts = decoded
    assert plaintext[:len(PLAINTEXT)] == PLAINTEXT


# ---------------------------------------------------- guards ---------

def test_tx_off_guard_refuses_loud():
    sender, modem = _sender(tx_enabled=False)
    ok = asyncio.run(sender.send_channel_data(codec.TYPE_PULSE, PLAINTEXT))
    assert ok is False
    assert modem.sent == []                          # nothing handed to TX


def test_framing_mismatch_refused():
    sender, modem = _sender(tx_enabled=True)
    bad = codec.TYPE_PULSE.to_bytes(2, "little") + bytes([99]) + b"\x00" * 5
    assert asyncio.run(sender.send_channel_data(codec.TYPE_PULSE, bad)) is False
    assert modem.sent == []


def test_embedded_type_mismatch_refused():
    sender, modem = _sender(tx_enabled=True)
    bad = codec.data_type_bytes(codec.TYPE_SECT_SUM, b"\x01" * 8)
    assert asyncio.run(sender.send_channel_data(codec.TYPE_PULSE, bad)) is False
    assert modem.sent == []


def test_oversize_frame_refused():
    sender, modem = _sender(tx_enabled=True)
    # framing-valid but AES pads 253B plaintext to 256B -> 262B frame
    body = bytes(250)
    payload = codec.data_type_bytes(codec.TYPE_PULSE, body)
    assert asyncio.run(sender.send_channel_data(codec.TYPE_PULSE,
                                                payload)) is False
    assert modem.sent == []


def test_modem_refusal_propagates():
    class Down(FakeModem):
        async def send(self, data):
            return False
    modem = Down()
    sender = RadioSender(modem, ChannelKeys.from_secret("#scope", "#scope"),
                         tx_enabled=True)
    assert asyncio.run(sender.send_channel_data(codec.TYPE_PULSE,
                                                PLAINTEXT)) is False


# ------------------------------------------------- own-TX loopback ------

def test_loopback_suppressed_via_shared_dedupe():
    """cleanmodem loops our TX back as RX: with a shared dedupe the
    echo is suppressed (120s own-TX window) - no phantom observation."""
    dedupe = FloodDedupe()
    sender, modem = _sender(tx_enabled=True, dedupe=dedupe)
    channel = sender.channel
    ok = asyncio.run(sender.send_channel_data(codec.TYPE_PULSE, PLAINTEXT))
    assert ok is True
    loopback = modem.sent[0]                         # the frame as RX sees it
    src = RawPacketSource(ReplayTransport([]), channels=[channel],
                          dedupe=dedupe)
    obs = src.handle_packet(RxPacket(data=loopback))
    assert obs is None
    assert src.stats.duplicates == 1
    assert src.stats.received == 1


def test_link_state_probes():
    sender, modem = _sender(tx_enabled=False)
    assert sender.is_connected is True
    assert sender.has_slot is True
    sender2 = RadioSender(None, ChannelKeys.from_secret("#scope", "#scope"))
    assert sender2.is_connected is False

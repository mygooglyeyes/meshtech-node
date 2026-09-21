"""Unidentified-repeater tracker tests (Brett 2026-09-21).

The accountability invariant: a node that repeats traffic is COUNTED
from its first relay, even with zero readable payloads, and is
promoted to a named node the moment its advert is heard - exact
pubkey-prefix matching, no fuzzy guesses.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.packets import ChannelKeys  # noqa: E402
from meshtech_node.rawsource import (  # noqa: E402
    RawPacketSource, ReplayTransport, RxPacket,
)
from meshtech_node.repeaters import RepeaterTable  # noqa: E402

SCOPE_HASH = 0x39
GOLDEN_CT = bytes.fromhex(
    "d28e60ccfdf9984883ba5953e9c550aa409864a430c85667419ffa975edbf943")
GOLDEN_MAC = bytes.fromhex("ca92")

# A repeater's full pubkey (32B) - its 1/2/3-byte prefixes are the tags.
PUBKEY = bytes(range(0xA0, 0xC0))          # A0 A1 A2 ... BF
TAG1 = PUBKEY[:1]                          # A0
TAG2 = PUBKEY[:2]                          # A0 A1
TAG3 = PUBKEY[:3]                          # A0 A1 A2

# A DIFFERENT node whose 1-byte tag collides with TAG1 (alias case).
OTHER_PUBKEY = bytes([0xA0]) + bytes(range(0x30, 0x4F))

ADVERT_BODY = (PUBKEY
               + (1234567890).to_bytes(4, "little")
               + b"\x33" * 64
               + bytes([0x90])                       # class | has-name
               + (41700000).to_bytes(4, "little", signed=True)
               + (-111800000).to_bytes(4, "little", signed=True)
               + b"EmergencyRptr")


def _frame(payload_type: int, route: int, payload: bytes,
           path: bytes = b"", hash_size: int = 1) -> bytes:
    """Hand-built frame: header | [transport codes] | path_len | path |
    payload. path_len encodes hash_size (bits 6-7) and hop count."""
    header = bytes([(0 << 6) | (payload_type << 2) | route])
    out = header
    if route in (0, 3):
        out += (0x1122).to_bytes(2, "little") + (0x3344).to_bytes(2, "little")
    path_len = ((hash_size - 1) << 6) | (len(path) // hash_size)
    out += bytes([path_len]) + path + payload
    return out


def _foreign_group(path: bytes, hash_size: int = 1) -> bytes:
    """Foreign-channel group packet (undecodable payload, real header)."""
    payload = bytes([0xAA]) + GOLDEN_MAC + GOLDEN_CT
    return _frame(0x06, 0x02, payload, path=path, hash_size=hash_size)


def _advert() -> bytes:
    return _frame(0x04, 0x02, ADVERT_BODY)


def _source():
    channels = [ChannelKeys.from_secret("#scope", "#scope")]
    return RawPacketSource(ReplayTransport([]), channels=channels)


# ------------------------------------------------------------- table ----

def test_unknown_tag_counted_from_first_relay():
    t = RepeaterTable()
    t.observe_tag(TAG2, now=1000.0)
    t.observe_tag(TAG2, now=1001.0)
    t.observe_tag(TAG2, now=1002.0)
    unk = t.unknown_repeaters()
    assert len(unk) == 1
    assert unk[0].relay_count == 3
    assert unk[0].first_heard == 1000.0
    assert unk[0].last_heard == 1002.0
    assert unk[0].identified is False


def test_advert_promotes_matching_tag_exactly():
    t = RepeaterTable()
    t.observe_tag(TAG2, now=1000.0)
    t.observe_tag(TAG2, now=1001.0)
    promoted = t.observe_advert(PUBKEY, "EmergencyRptr", 2, now=1002.0)
    assert len(promoted) == 1
    assert promoted[0].tag == TAG2
    assert promoted[0].name == "EmergencyRptr"
    assert promoted[0].relay_count == 2      # history is kept, not reset
    assert promoted[0].identified is True
    assert t.unknown_repeaters() == []
    assert len(t.known_repeaters()) == 1


def test_promotion_matches_every_tag_width():
    # The same pubkey heard as 1-byte, 2-byte, AND 3-byte tags (nodes
    # emit different widths) - one advert promotes all three.
    t = RepeaterTable()
    t.observe_tag(TAG1, now=1.0)
    t.observe_tag(TAG2, now=2.0)
    t.observe_tag(TAG3, now=3.0)
    promoted = t.observe_advert(PUBKEY, "R", 2, now=4.0)
    assert sorted(e.tag for e in promoted) == sorted([TAG1, TAG2, TAG3])


def test_non_matching_pubkey_promotes_nothing():
    t = RepeaterTable()
    t.observe_tag(TAG2, now=1.0)
    stranger = bytes([0xBB]) + PUBKEY[1:]
    promoted = t.observe_advert(stranger, "Stranger", 2, now=2.0)
    assert promoted == []
    assert t.unknown_repeaters()[0].identified is False


def test_prune_drops_silent_entries():
    t = RepeaterTable(expire_after_seconds=100.0)
    t.observe_tag(TAG1, now=0.0)
    t.observe_tag(TAG2, now=500.0)
    assert t.prune(now=550.0) == 1           # the tag-0 entry
    assert TAG1 not in t.entries
    assert TAG2 in t.entries


# --------------------------------------------------- rawsource wiring ----

def test_source_counts_repeater_tags_from_foreign_traffic():
    src = _source()
    # Foreign-channel packets relayed through our two-byte-tag repeater.
    src.handle_packet(RxPacket(data=_foreign_group(TAG2, hash_size=2)))
    src.handle_packet(RxPacket(data=_foreign_group(TAG2, hash_size=2)))
    assert len(src.repeaters) == 1
    assert src.repeaters.unknown_repeaters()[0].relay_count == 2


def test_source_identifies_repeater_from_later_advert():
    src = _source()
    src.handle_packet(RxPacket(data=_foreign_group(TAG2, hash_size=2)))
    src.handle_packet(RxPacket(data=_foreign_group(TAG2, hash_size=2)))
    assert src.repeaters.unknown_repeaters()[0].relay_count == 2
    src.handle_packet(RxPacket(data=_advert()))                 # advert!
    unk = src.repeaters.unknown_repeaters()
    assert unk == []                       # promoted - never invisible
    known = src.repeaters.known_repeaters()
    assert len(known) == 1
    assert known[0].name == "EmergencyRptr"
    assert known[0].relay_count == 2


def test_multi_hop_path_counts_every_repeater():
    src = _source()
    two_hops = TAG1 + bytes([0xBB])         # our repeater + a stranger
    src.handle_packet(RxPacket(data=_foreign_group(two_hops, hash_size=1)))
    assert len(src.repeaters) == 2
    counts = {e.tag: e.relay_count for e in src.repeaters.entries.values()}
    assert counts[TAG1] == 1 and counts[bytes([0xBB])] == 1


def test_advert_relay_also_counts_then_promotes():
    # The advert ITSELF travelled through the repeater: the tag is
    # counted AND the advert (same packet stream) promotes it.
    src = _source()
    advert_via_repeater = _frame(0x04, 0x02, ADVERT_BODY,
                                 path=TAG2, hash_size=2)
    src.handle_packet(RxPacket(data=advert_via_repeater))
    known = src.repeaters.known_repeaters()
    assert len(known) == 1
    assert known[0].relay_count == 1        # carried the advert itself

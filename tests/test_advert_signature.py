"""Advert signature gate tests (2026-09-23, Brett's duplicate-dots fix).

THE BUG: a bit-corrupted advert passed every check (only lengths were
examined) and created a FRESH node row - mojibake name, nonsense
position, "new" radio ID. hilltop's table had collected dozens
(`ES\\7 Gilroy Repeater (O` beside the real `ESP6 Gilroy Repeater`,
`ARE[(&(CERT` twins at identical coordinates, a mojibake Novato twin
~200 km off), and the connect roster delivered every one as an extra
dot. THE FIX: MeshCore adverts are Ed25519-signed; the node now
verifies each advert at the RX gate (recipe proven against the
openhop_core reference, pymc_core AdvertHandler) and rejects anything
that fails - corrupted names, positions, IDs, or signature bytes.

Recipe proven against: pymc_core/node/handlers/advert.py
    signed_region = pubkey + timestamp(4 LE) + appdata
    verified with VerifyKey(pubkey); signature = payload[36:100]
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from meshtech_node.packets import (  # noqa: E402
    advert_recipe_reports, parse_advert, verify_advert_signature,
)

pymc = pytest.importorskip("pymc_core", reason="reference library signs "
                                              "the test adverts")
from nacl.signing import SigningKey  # noqa: E402


def _signed_advert(signing_key: SigningKey, *, name: bytes = b"LoganPeak",
                   flags: int = 0x90, lat: int = 41700000,
                   lon: int = -111800000, ts: int = 1234567890) -> bytes:
    """A REAL advert, signed exactly as the reference packet_builder
    does: payload = pubkey + ts(4 LE) + signature(64) + appdata, with
    signature over pubkey + ts + appdata."""
    pubkey = bytes(signing_key.verify_key)
    appdata = bytes([flags]) + lat.to_bytes(4, "little", signed=True) \
        + lon.to_bytes(4, "little", signed=True) + name
    ts_bytes = ts.to_bytes(4, "little")
    sig = signing_key.sign(pubkey + ts_bytes + appdata).signature
    return pubkey + ts_bytes + sig + appdata


def test_intact_advert_verifies():
    key = SigningKey.generate()
    payload = _signed_advert(key)
    assert verify_advert_signature(payload) is True
    info = parse_advert(payload)
    assert info.name == "LoganPeak"       # parsing still works after


def test_flipped_name_bit_fails_verification():
    """THE EXACT BUG CLASS Brett's table showed: one flipped bit turns
    a name into mojibake and the advert into a phantom node. The
    signature now catches it."""
    key = SigningKey.generate()
    payload = bytearray(_signed_advert(key, name=b"LoganPeak"))
    # flip one bit deep in the name
    payload[-1] ^= 0x01
    assert verify_advert_signature(bytes(payload)) is False


def test_flipped_position_bit_fails_verification():
    """Corrupt twins carried corrupt positions too (the mojibake
    Novato twin sat ~200 km off). A flipped coordinate bit breaks the
    signature just the same."""
    key = SigningKey.generate()
    payload = bytearray(_signed_advert(key))
    payload[101] ^= 0x01                  # inside the lat bytes
    assert verify_advert_signature(bytes(payload)) is False


def test_flipped_pubkey_bit_fails_verification():
    """Flipped ID bits made the 'new node' (a phantom radio identity).
    The pubkey is part of the signed region - and the verify key
    itself."""
    key = SigningKey.generate()
    payload = bytearray(_signed_advert(key))
    payload[5] ^= 0x01
    assert verify_advert_signature(bytes(payload)) is False


def test_forged_signature_fails():
    """A fabricated signature (the old ADVERT_BODY test vector's
    0x22*64 filler is exactly that) must not pass."""
    key = SigningKey.generate()
    payload = bytearray(_signed_advert(key))
    payload[40] ^= 0xFF                   # inside the signature bytes
    assert verify_advert_signature(bytes(payload)) is False


def test_untouched_signature_bytes_not_in_signed_region():
    """The signature itself is NOT signed (by definition); verify the
    recipe matches the reference split: bytes 36..100. Flipping a byte
    OUTSIDE the payload cannot be caught - but the timestamp boundary
    must behave exactly as the reference parses it."""
    key = SigningKey.generate()
    payload = bytearray(_signed_advert(key))
    payload[35] ^= 0x01                   # high byte of the timestamp
    assert verify_advert_signature(bytes(payload)) is False


def test_truncated_payload_rejected():
    assert verify_advert_signature(b"\\x00" * 50) is False
    assert verify_advert_signature(b"") is False


# ------------------------------------------------- recipe battery -------


def test_recipe_battery_reference_recipe_verifies_forgery_fails():
    """The DIAGNOSTIC battery (the reject-capture companion, Brett's
    "work a tool that tries different decoding techniques too"): on a
    clean advert the production recipe verifies; on a forged one no
    recipe does. The battery NEVER feeds the gate - it names recipes
    offline (tools/advert_probe.py)."""
    key = SigningKey.generate()
    payload = _signed_advert(key)
    reports = advert_recipe_reports(payload)
    reference_ok = [ok for name, ok in reports if "uncapped" in name]
    assert reference_ok == [True]

    forged = bytearray(payload)
    forged[40] ^= 0xFF                   # inside the signature bytes
    assert not any(ok for _n, ok in advert_recipe_reports(bytes(forged)))


def test_recipe_battery_spots_truncated_appdata_variant():
    """The battery's real job: DISTINGUISH recipes. With appdata longer
    than the reference's 96-byte cap, the capped variant verifies a
    DIFFERENT payload than the uncapped one - so a sender that signs
    the truncated form shows up as 'capped verifies, uncapped not'.
    (Below the cap the two are byte-identical - no signal there.)"""
    key = SigningKey.generate()
    long_name = b"R" * 88                # appdata: 1+8+88 = 97 bytes
    payload = _signed_advert(key, name=long_name)
    assert len(payload) - 100 == 97      # one byte over the cap
    reports = dict(advert_recipe_reports(payload))
    assert reports["reference: pub+ts+appdata, sig@36..100, appdata uncapped"]
    assert not reports["appdata capped at 96 (reference handler truncates)"]

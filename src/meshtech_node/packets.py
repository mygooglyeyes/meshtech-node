"""The packet core: frame split, channel crypto, dedupe, advert parse.

SEED PROVENANCE (SEED-MAP.md "ADAPT core/mcp.py"): the pure packet
functions are adapted from the bot's proven `core/mcp.py` (live-traffic
proven, off-air stack) - parse_envelope, path helpers, channel key
derivation and group decode. Wire constants are VERIFIED against the
read-only reference `openhop_core/src/pymc_core`:
- payload types: TXT_MSG=0x02 ADVERT=0x04 GRP_TXT=0x05 GRP_DATA=0x06
  (constants.py:26-30)
- frame layout: header(1) | [transport codes(4) when route 0/3] |
  path_len(1) | path | payload (packet.py read_from, lines 435-461)
- channel crypto: AES-ECB zero-padded, HMAC-SHA256 truncated to 2B
  (protocol/crypto.py) - golden vectors generated from the reference
  itself live in tests/test_packets.py.

Honesty rules honored here:
- a failed crypto import logs ONCE, LOUDLY, and decode answers None -
  never a plausible constant, never a silent skip.
- advert signatures are NOT verified in v1 (the bot verified them for
  contact-book trust; the node learns names/positions only). Flagged
  for bench review - see rawsource.py.
- group packets carry NO sender identity on the wire (openhop_repeater
  engine.py:608 returns src_hash=None for them); the caller gets
  prefix=0 and the scope brain owns body-level attribution.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from Crypto.Cipher import AES

log = logging.getLogger("meshtech-node.packets")

# Wire constants - verified against openhop_core/src/pymc_core
# protocol/constants.py:26-30 (2026-09-20). Never re-derived by test.
PAYLOAD_TYPE_TXT_MSG = 0x02
PAYLOAD_TYPE_ADVERT = 0x04
PAYLOAD_TYPE_GRP_TXT = 0x05
PAYLOAD_TYPE_GRP_DATA = 0x06

ROUTE_TRANSPORT_FLOOD = 0
ROUTE_FLOOD = 1
ROUTE_DIRECT = 2
ROUTE_TRANSPORT_DIRECT = 3
# VERIFIED against openhop_core/src/pymc_core protocol/constants.py:16-19
# (2026-09-20). NOTE: the bot's mcp.py name dict had 0/1 swapped - its
# transport-codes bit rule was right, its labels were not. Trust the
# reference, not the inherited dict.
ROUTE_TYPE_NAMES = {0: "transport-flood", 1: "flood", 2: "direct",
                    3: "transport-direct"}

FLOOD_DEDUPE_WINDOW_S = 45.0     # mcp.py _is_duplicate
OWN_TX_WINDOW_S = 120.0          # our own TX heard back


# ---------------------------------------------------------------- frame ----

@dataclass
class FrameParts:
    """One heard packet, FULL header kept - the reason this node exists.

    The openhop repeater stripped transport codes/paths from GRP_DATA
    (the 2026-09-18 refresh-request deaths); nothing here does."""
    route: int
    payload_type: int
    version: int
    transport_codes: Optional[Tuple[int, int]]   # None when absent
    path_len_byte: int
    hash_size: int                               # 1-3 bytes per hop
    hops: int                                    # 0-63 from path_len byte
    path: bytes
    payload: bytes


def split_frame(data: bytes) -> Optional[FrameParts]:
    """Pure-bytes frame split, mirroring pymc_core Packet.read_from()
    (openhop_core/src/pymc_core/protocol/packet.py:435-461) and the
    bot's parse_envelope (mcp.py:210). Returns None on anything
    malformed - the caller counts it and moves on."""
    if not data or len(data) < 2:
        return None
    header = data[0]
    route = header & 0x03
    payload_type = (header >> 2) & 0x0F
    version = (header >> 6) & 0x03
    idx = 1
    transport_codes: Optional[Tuple[int, int]] = None
    # Transport codes: the two "transport" routes carry them -
    # TRANSPORT_FLOOD (0) and TRANSPORT_DIRECT (3), per reference
    # has_transport_codes() (packet.py:183).
    if route in (ROUTE_TRANSPORT_FLOOD, ROUTE_TRANSPORT_DIRECT):
        if len(data) < idx + 4:
            return None
        transport_codes = (
            int.from_bytes(data[idx:idx + 2], "little"),
            int.from_bytes(data[idx + 2:idx + 4], "little"),
        )
        idx += 4
    if idx >= len(data):
        return None
    path_len_byte = data[idx]
    idx += 1
    hash_size = ((path_len_byte >> 6) & 0x03) + 1
    if hash_size > 3:                    # reference raises on this encoding
        return None
    hops = path_len_byte & 0x3F
    path_end = idx + hash_size * hops
    if path_end > len(data):
        return None
    path = data[idx:path_end]
    payload = data[path_end:]
    return FrameParts(route, payload_type, version, transport_codes,
                      path_len_byte, hash_size, hops, path, payload)


# --------------------------------------------------------------- crypto ----

_CRYPTO = None            # memoized CryptoUtils
_CRYPTO_ERR: Optional[str] = None


class _NodeCrypto:
    """The FOUR crypto primitives the node needs, vendored line-for-
    line from pymc_core/protocol/crypto.py (openhop_core reference,
    read-only). 2026-09-20: hilltop has NO pymc_core anywhere (Brett's
    find), so the dependency is met by this vendor - same bytes,
    proved by the same golden vectors generated FROM the reference
    library (test_packets.py). Only stdlib (hashlib, hmac) and
    pycryptodome (AES) - both already in the node venv."""

    CIPHER_MAC_SIZE = 2      # matches firmware
    CIPHER_BLOCK_SIZE = 16

    @staticmethod
    def sha256(data: bytes) -> bytes:
        return hashlib.sha256(data).digest()

    @staticmethod
    def _hmac_sha256(key: bytes, data: bytes) -> bytes:
        return hmac.new(key, data, hashlib.sha256).digest()

    @staticmethod
    def _aes_encrypt(key: bytes, data: bytes) -> bytes:
        cipher = AES.new(key, AES.MODE_ECB)
        pad_len = (CryptoSize - (len(data) % CryptoSize)) % CryptoSize
        if pad_len > 0:
            data += b"\x00" * pad_len
        return cipher.encrypt(data)

    @staticmethod
    def _aes_decrypt(key: bytes, ciphertext: bytes) -> bytes:
        cipher = AES.new(key, AES.MODE_ECB)
        orig_len = len(ciphertext)
        if orig_len % CryptoSize != 0:
            pad_len = CryptoSize - (orig_len % CryptoSize)
            ciphertext = ciphertext + (b"\x00" * pad_len)
        out = b"".join(
            cipher.decrypt(ciphertext[i:i + CryptoSize])
            for i in range(0, len(ciphertext), CryptoSize))
        return out[:orig_len]


CryptoSize = _NodeCrypto.CIPHER_BLOCK_SIZE


def _crypto():
    """The crypto provider, imported once. Preference order: the
    reference library if present (dev machines with openhop_core),
    else the vendored _NodeCrypto (proven identical by golden vector).
    Never fails silently: if AES itself is missing, log ONCE, LOUDLY,
    and stay failed - group packets count as undecodable, the gap is
    honest (v0.0.183 rule: never answer a failed read with a plausible
    constant)."""
    global _CRYPTO, _CRYPTO_ERR
    if _CRYPTO is None and _CRYPTO_ERR is None:
        try:
            from pymc_core.protocol.crypto import CryptoUtils as _C
            _CRYPTO = _C
            log.info("crypto: reference pymc_core CryptoUtils in use")
        except Exception:
            try:
                _CRYPTO = _NodeCrypto()
                log.info("crypto: vendored _NodeCrypto in use (golden-"
                         "vector-proven identical to the reference)")
            except Exception as exc:              # pragma: no cover
                _CRYPTO_ERR = f"{type(exc).__name__}: {exc}"
                log.error("CHANNEL DECRYPT UNAVAILABLE - AES setup "
                          "failed (%s). Group traffic counts but does "
                          "NOT decode. The gap is honest.", _CRYPTO_ERR)
    return _CRYPTO


def crypto_ok() -> bool:
    """True when channel decrypt is available. Call once at startup so
    a broken import surfaces LOUDLY at boot, not on the first packet."""
    return _crypto() is not None


def secret_bytes(secret: str) -> bytes:
    """Channel secret text -> key bytes, matching openhop's
    GroupTextHandler: hex when it parses as hex, utf-8 otherwise;
    truncated to 32 (mcp.py:456)."""
    try:
        raw = bytes.fromhex(secret)
    except ValueError:
        raw = secret.encode("utf-8")
    return raw[:32]


def derive_channel_keys(secret: str) -> Tuple[int, bytes, bytes]:
    """(channel_hash, aes_key, hmac_key) - the firmware group scheme,
    proven against live traffic (mcp.py:468). AES key = zero-padded
    secret's first 16 bytes; HMAC key = full zero-padded 32 bytes;
    hash = sha256(secret)[0] with the firmware's zero-tail 16-byte
    rule for secrets whose second half is all zeros."""
    raw = secret_bytes(secret)
    basis = raw[:16] if (len(raw) >= 32 and raw[16:32] == b"\x00" * 16) else raw
    if len(raw) < 32:
        raw = raw + b"\x00" * (32 - len(raw))
    return hashlib.sha256(basis).digest()[0], raw[:16], raw


@dataclass
class ChannelKeys:
    """One joined channel, ready to decrypt."""
    name: str
    channel_hash: int
    aes_key: bytes
    hmac_key: bytes

    @classmethod
    def from_secret(cls, name: str, secret: str) -> "ChannelKeys":
        ch, aes, hmac = derive_channel_keys(secret)
        return cls(name, ch, aes, hmac)


def decode_group_payload(payload: bytes,
                         channels: List[ChannelKeys],
                         ) -> Optional[Tuple[ChannelKeys, bytes, float]]:
    """Decode one GRP_TXT/GRP_DATA payload: ch_hash(1) | mac(2) |
    ciphertext. Returns (channel, plaintext, origin_ts) for the first
    HMAC-valid candidate, or None (foreign channel / bad MAC / crypto
    unavailable). Layout + loop per mcp.py:1256-1290."""
    if len(payload) < 4:
        return None
    channel_hash = payload[0]
    mac = payload[1:3]
    ciphertext = payload[3:]
    crypto = _crypto()
    if crypto is None:
        return None
    for channel in channels:
        if channel.channel_hash != channel_hash:
            continue
        expected = crypto._hmac_sha256(channel.hmac_key, ciphertext)[:2]
        if mac != expected:
            continue                                 # foreign or corrupt
        plaintext = crypto._aes_decrypt(channel.aes_key, ciphertext)
        origin_ts = float(int.from_bytes(plaintext[:4], "little")) \
            if len(plaintext) >= 4 else 0.0
        return channel, plaintext, origin_ts
    return None


# --------------------------------------------------------------- dedupe ----

class FloodDedupe:
    """MeshCore-style flood dedupe (mcp.py:1084-1117): sha256 over
    payload_type byte + payload, truncated to 16 hex chars. Two
    memories: heard packets (45 s) and our own TX (120 s). Pure state
    machine; clock is injectable for tests."""

    def __init__(self, *, now: Optional[float] = None) -> None:
        self._now = time.time if now is None else (lambda: now)
        self._seen: Dict[str, float] = {}
        self._own_tx: Dict[str, float] = {}

    @staticmethod
    def payload_hash(payload_type: int, payload: bytes) -> str:
        digest = hashlib.sha256()
        digest.update(bytes([payload_type & 0xFF]))
        digest.update(payload)
        return digest.hexdigest()[:16]

    def mark_own_tx(self, payload_type: int, payload: bytes) -> None:
        self._own_tx[self.payload_hash(payload_type, payload)] = self._now()

    def is_duplicate(self, payload_type: int, payload: bytes) -> bool:
        """True when this exact packet was already heard (or is our own
        TX echoing back). First hearing is marked and returns False."""
        key = self.payload_hash(payload_type, payload)
        now = self._now()
        for store, window in ((self._seen, FLOOD_DEDUPE_WINDOW_S),
                              (self._own_tx, OWN_TX_WINDOW_S)):
            stale = [k for k, ts in store.items() if now - ts > window]
            for k in stale:
                store.pop(k, None)
        if key in self._own_tx or key in self._seen:
            return True
        self._seen[key] = now
        return False


# --------------------------------------------------------------- adverts ---

@dataclass
class AdvertInfo:
    """What an ADVERT payload says, parsed. Layout per the live-verified
    docstring in meshtech_scope packetsource.py (row 105713 truth):
    pubkey(32) | sender_timestamp(4 LE) | signature(64) | appdata;
    appdata = flags(bit0-3 node class 1 chat/2 repeater/3 room/4 sensor,
    0x10 has-location, 0x80 has-name) | [i32 LE lat, i32 LE lon /1e6] |
    [UTF-8 name]."""
    prefix: int
    origin_ts: float
    flags: int
    lat: Optional[float] = None
    lon: Optional[float] = None
    name: Optional[str] = None
    pubkey: Optional[bytes] = None   # full 32B key - path-tag matching

    @property
    def node_class(self) -> int:
        return self.flags & 0x0F


def parse_advert(payload: bytes) -> Optional[AdvertInfo]:
    """Parse one ADVERT payload. SIGNATURE NOT VERIFIED in v1 (see
    module docstring) - flagged loudly here so no later reader assumes
    trust that does not exist yet."""
    if len(payload) < 32 + 4 + 64:
        return None
    prefix = payload[0]
    pubkey = payload[0:32]              # full key: matches path tags
    origin_ts = float(int.from_bytes(payload[32:36], "little"))
    appdata = payload[100:]
    if not appdata:
        return AdvertInfo(prefix, origin_ts, 0, pubkey=pubkey)
    flags = appdata[0]
    idx = 1
    lat = lon = None
    if flags & 0x10:                                   # has-location
        if len(appdata) < idx + 8:
            return AdvertInfo(prefix, origin_ts, flags)
        lat = int.from_bytes(appdata[idx:idx + 4], "little", signed=True) / 1e6
        idx += 4
        lon = int.from_bytes(appdata[idx:idx + 4], "little", signed=True) / 1e6
        idx += 4
    name = None
    if flags & 0x80:                                   # has-name
        name = appdata[idx:].decode("utf-8", "replace").rstrip("\x00") or None
    return AdvertInfo(prefix, origin_ts, flags, lat, lon, name, pubkey)

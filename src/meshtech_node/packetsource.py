"""Packet sources - where observations come from.

Two implementations:

- DemoSource: deterministic synthetic mesh (no radio, no API) for the
  bench and tests. Reproducible from a seed; the same fixture drives
  the golden vectors and the PWA's demo mode.
- RepeaterApiSource: polls the repeater's REST API - the SAME data the
  outpost UI plugin uses. SOURCE TRUTH (2026-09-18, mined from the
  openhop_repeater-dev source AND CONFIRMED LIVE on hilltop the same
  day; BENCH-CHECKLIST.md section G):
    GET /api/recent_packets?limit=  -> rows with src_hash (2-char
      UPPERCASE hex prefix; on ADVERT rows this equals the first byte
      of the advert pubkey - live row 105713), timestamp (repeater
      receipt, epoch SECONDS), type (0x04 ADVERT, 0x06 GRP_DATA),
      route (2/3 = DIRECT, no path), original_path (JSON array of
      4-char hop hashes in TRAVEL ORDER - last element == the row's
      upstream_hash, i.e. the hop that handed the packet to this box;
      live row 105713), upstream_hash (last-hop relay; populated even
      when src_hash/dst_hash are NULL on flood rows), rssi/snr/score,
      id (autoincrement poll cursor), is_duplicate (live rows carry
      1 + drop_reason "Duplicate" - skipped in ingest).
      ADVERT rows (type 4) carry the sender's appdata in the payload:
      pubkey(32B) + sender_timestamp(4B LE) + signature(64B) +
      appdata (flags byte: bits 0-3 node type 1 chat/2 repeater/3
      room/4 sensor, 0x10 has-location, 0x80 has-name; then i32 LE
      lat/lon /1e6, then UTF-8 name). The sender timestamp makes the
      propagation delay computable for adverts (negative deltas =
      clock skew -> unknown, never fabricated).
    The hilltop adverts TABLE is empty (live finding C1: the endpoints
    require contact_type ints and nothing is persisted there), so the
    node table is built from type-0x04 payloads, not /adverts_by_contact_type.
  Auth: X-API-Key header (machine-to-machine; admin-equivalent -
  token stays in a file). Envelope: {"success": true, "data": ...}.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import struct
import time
from pathlib import Path
from typing import AsyncIterator, Callable, List, Optional

from .config import Settings
from .observations import BackboneNeighbor, Observation

log = logging.getLogger("meshtech-scope.source")


# --------------------------------------------------------------------------
# Demo source (deterministic; seed controls everything)
# --------------------------------------------------------------------------

# Place names for the synthetic demo mesh, around the 94945 / Novato CA
# default centre. Cycle with a numeric suffix if node_count exceeds the
# list; the demo is honest that it is synthetic - these are labels, not
# claimed real node positions.
DEMO_PLACES = [
    "Hilltop", "Novato Creek", "Hamilton", "Ignacio", "Bel Marin",
    "Stafford Lake", "Pacheco", "Lynwood", "Olive", "Rush Creek",
    "San Marin", "Deer Island", "Binford", "Simmons", "Warner",
]


class DemoSource:
    """A synthetic but plausible mesh: N nodes scattered over the area,
    clustered around a few backbone repeaters, each sending traffic.

    `tick()` yields one batch of observations per call; drives at
    `packets_per_tick` per second of simulated time.
    """

    def __init__(self, seed: int = 42, node_count: int = 40,
                 center_lat: float = 0.0, center_lon: float = 0.0,
                 span_deg: float = 0.36):
        self._rng = random.Random(seed)
        self._nodes: List[dict] = []
        self._center_lat = center_lat
        self._center_lon = center_lon
        self._span_deg = span_deg
        backbone = 5
        for i in range(node_count):
            if i < backbone:
                # backbone repeaters: spread wide
                lat = center_lat + (self._rng.random() - 0.5) * span_deg
                lon = center_lon + (self._rng.random() - 0.5) * span_deg
            else:
                # leaf nodes: cluster around a random backbone node
                b = self._nodes[self._rng.randrange(min(i, backbone))]
                lat = b["lat"] + (self._rng.random() - 0.5) * span_deg * 0.12
                lon = b["lon"] + (self._rng.random() - 0.5) * span_deg * 0.12
            place = DEMO_PLACES[i % len(DEMO_PLACES)]
            suffix = i // len(DEMO_PLACES)
            self._nodes.append({
                "prefix": (0x10 + i) & 0xFF,
                "lat": lat,
                "lon": lon,
                "name": place if suffix == 0 else f"{place} {suffix + 1}",
                "is_backbone": i < backbone,
                # class mirrors is_backbone: the demo knows its own mesh
                "node_class": 1 if i < backbone else 2,
            })

    @property
    def nodes(self) -> List[dict]:
        return list(self._nodes)

    def positions(self) -> List[tuple]:
        return [(n["prefix"], n["lat"], n["lon"], n["name"],
                 n["node_class"])
                for n in self._nodes]

    def tick(self, now: float, packets: int = 8) -> List[Observation]:
        """Generate `packets` observations at time `now`."""
        out: List[Observation] = []
        for _ in range(packets):
            sender = self._rng.choice(self._nodes)
            path = []
            if not sender["is_backbone"] and self._rng.random() < 0.7:
                backbone = [n for n in self._nodes if n["is_backbone"]]
                hop = self._rng.choice(backbone)
                path.append(hop["prefix"])
                if self._rng.random() < 0.3:
                    other = self._rng.choice(backbone)
                    if other["prefix"] != hop["prefix"]:
                        path.append(other["prefix"])
            origin = now - self._rng.uniform(0.2, 3.0)
            out.append(Observation(
                recv_ts=now,
                origin_ts=origin,
                prefix=sender["prefix"],
                lat=sender["lat"],
                lon=sender["lon"],
                path_prefixes=path,
            ))
        return out


# --------------------------------------------------------------------------
# Repeater API source
# --------------------------------------------------------------------------

def row_prefix_of(row: dict) -> Optional[int]:
    """Node prefix byte from a repeater API row, or None if unusable.

    SOURCE TRUTH (2026-09-18, live-confirmed): packet rows carry
    `src_hash` = the 2-char UPPERCASE hex of the sender's prefix byte
    (engine.py _packet_record_src_dst; on adverts = the pubkey's first
    byte, live row 105713). Flood rows where src_hash is NULL carry
    `upstream_hash` = the LAST-HOP relay (4-char hash on hilltop where
    path_hash_size=2 -> the prefix byte is the LAST 2 hex chars, not
    the first - live row 105713). Advert rows keep `pubkey` (full hex,
    first 2 chars = prefix). Precedence: src_hash (sender identity)
    then upstream_hash (the relay we can honestly attribute), then
    pubkey. One shared helper so any future field change has exactly
    one place to update.
    """
    for key in ("src_hash", "upstream_hash", "pubkey"):
        raw = row.get(key)
        if raw is None:
            continue
        key_hex = str(raw).strip().lower().replace("0x", "")
        if len(key_hex) < 2:
            continue
        try:
            if key == "pubkey":
                # Full 64-char pubkey hex: the prefix byte is FIRST
                # (row_prefix_of's original advert-table rule).
                return int(key_hex[:2], 16)
            # src_hash is the 2-char prefix itself; upstream_hash and
            # original_path hops are 4-char hashes whose prefix byte
            # is the LAST 2 hex chars ("3e3c" -> 0x3C, live row
            # 105713). 2-char values are unaffected by last-2.
            return int(key_hex[-2:], 16)
        except ValueError:
            continue
    return None


# Contact-type strings the repeater's adverts table stores (openhop_core
# get_contact_type_name). Map to the feed's class bits: 1 = backbone
# repeater, 2 = companion, 0 = unknown/honest gap. Room servers and
# sensors are their own thing in MeshCore; they map to 0 here (they are
# NOT repeaters) - tightening that is a future wire change, not a guess.
_CONTACT_TYPE_CLASS = {
    "repeater": 1,
    "chat node": 2,
    "companion": 2,
    "room server": 0,
    "sensor": 0,
    "unknown": 0,
}


def node_class_of(row: dict) -> int:
    """Node class from a repeater API row, honestly.

    SOURCE TRUTH (2026-09-18): the adverts table stores
    `contact_type` as the STRING name ("Repeater", "Chat Node", ...)
    plus an `is_repeater` BOOLEAN. Packet rows carry no class at all.
    Anything unrecognised stays 0 (unknown) - the feed publishes class
    only when the source data claims it. Used for both packet rows and
    advert rows.
    """
    value = row.get("contact_type")
    if value is not None:
        # The source spoke. Recognised names map (0 = honestly neither
        # repeater nor companion: Room Server, Sensor, Unknown); an
        # UNRECOGNISED name also stays 0 - it must NOT borrow a class
        # from is_repeater (a future "Sensor 2" name would otherwise
        # publish as companion).
        return _CONTACT_TYPE_CLASS.get(str(value).strip().lower(), 0)
    # `is_repeater` only consulted when contact_type said nothing.
    if "is_repeater" in row and row.get("is_repeater") is not None:
        return 1 if row.get("is_repeater") else 2
    # Legacy/open spellings kept for older builds (honest fallback).
    for field in ("node_class", "class", "type", "node_type",
                  "role", "kind"):
        value = row.get(field)
        if value is None:
            continue
        text = str(value).strip().lower()
        if text in ("repeater", "rptr", "router", "1"):
            return 1
        if text in ("companion", "comp", "client", "room", "2"):
            return 2
        if text in ("unknown", "", "0"):
            return 0
    return 0


# --------------------------------------------------------------------------
# Advert payload decode (type 0x04) - layout from openhop_core
# parse_advert_payload + decode_appdata, live-confirmed 2026-09-18
# (hilltop rows 105713/105778; BENCH-CHECKLIST.md section G).
# --------------------------------------------------------------------------

# Advert flags (openhop_core constants): bits 0-3 node type, 4-7 features.
_ADVERT_FLAG_HAS_LOCATION = 0x10
_ADVERT_FLAG_HAS_FEATURE1 = 0x20
_ADVERT_FLAG_HAS_FEATURE2 = 0x40
_ADVERT_FLAG_HAS_NAME = 0x80
_ADVERT_PUBKEY_SIZE = 32
_ADVERT_TIMESTAMP_SIZE = 4
_ADVERT_SIGNATURE_SIZE = 64

# flags nibble -> feed class bit: 1 Chat Node -> 2 companion,
# 2 Repeater -> 1 backbone. Room (3) / sensor (4) / unknown (0) have
# NO honest two-bit class in the INTRO wire (bits only carry
# repeater/companion) -> 0 (unknown, shown honestly).
_ADVERT_NIBBLE_CLASS = {1: 2, 2: 1}


def decode_advert_payload(payload_hex: str) -> Optional[dict]:
    """Decode a type-0x04 advert payload hex string, or None if not one.

    Wire (openhop_core parse_advert_payload + decode_appdata):
      pubkey(32B) + timestamp(4B LE, sender-set) + signature(64B) +
      appdata[flags(1B)] then, in order, only if the flag is set:
      0x10 lat/lon (two i32 LE, /1e6), 0x20 feature1 u16 LE,
      0x40 feature2 u16 LE, 0x80 name (UTF-8 tail).
    Malformed/truncated payloads return None (honest skip), never a
    partial guess.
    """
    if not payload_hex or not isinstance(payload_hex, str):
        return None
    try:
        raw = bytes.fromhex(payload_hex.strip())
    except ValueError:
        return None
    head = _ADVERT_PUBKEY_SIZE + _ADVERT_TIMESTAMP_SIZE \
        + _ADVERT_SIGNATURE_SIZE
    if len(raw) < head + 1:
        return None  # too short to hold pubkey+ts+sig+flags
    pubkey = raw[:_ADVERT_PUBKEY_SIZE]
    sender_ts = int.from_bytes(
        raw[_ADVERT_PUBKEY_SIZE:head - _ADVERT_SIGNATURE_SIZE], "little")
    app = raw[head:]
    flags = app[0]
    out: dict = {
        "prefix": pubkey[0],
        "sender_ts": float(sender_ts) if sender_ts else None,
        "flags": flags,
        "node_class": _ADVERT_NIBBLE_CLASS.get(flags & 0x0F, 0),
    }
    offset = 1
    if flags & _ADVERT_FLAG_HAS_LOCATION:
        if len(app) < offset + 8:
            return None  # truncated location: not honest to guess
        lat, lon = struct.unpack("<ii", app[offset:offset + 8])
        out["latitude"] = lat / 1e6
        out["longitude"] = lon / 1e6
        offset += 8
    if flags & _ADVERT_FLAG_HAS_FEATURE1:
        offset += 2
    if flags & _ADVERT_FLAG_HAS_FEATURE2:
        offset += 2
    if flags & _ADVERT_FLAG_HAS_NAME:
        try:
            name = app[offset:].decode("utf-8").rstrip("\x00").strip()
        except UnicodeDecodeError:
            name = ""
        if name:
            out["node_name"] = name
    return out


def hop_prefix(hop: str) -> Optional[int]:
    """Prefix byte of one original_path hop hash, honestly.

    LIVE TRUTH (2026-09-18, row 105713): hilltop runs path_hash_size=2
    so hop hashes are 4-char and the PREFIX BYTE IS THE LAST 2 HEX
    CHARS ("3E3C" -> 0x3C). 2-char hashes keep working (their last 2
    chars are the whole thing). Order is travel order (confirmed same
    row); this helper does not reorder anything.
    """
    text = str(hop or "").strip().lower().replace("0x", "")
    if len(text) < 2:
        return None
    try:
        return int(text[-2:], 16)
    except ValueError:
        return None


def neighbor_of(row: dict) -> Optional[BackboneNeighbor]:
    """Map one /api/neighbor_links row to a BackboneNeighbor, honestly.

    LIVE TRUTH (2026-09-18, probe D): rows carry peer_hash (2-char hex
    = the byte itself on hilltop, but the last-2 rule handles any
    width), sample_count, duplicate_sample_count, first/last_seen,
    age_seconds, active, last_rssi/snr/score, ewma_rssi/snr/score,
    best/worst_score. Missing numeric fields stay None - never
    guessed. Rows without a usable peer_hash are skipped (None).
    """
    prefix_byte = row_prefix_of({"src_hash": row.get("peer_hash")})
    if prefix_byte is None:
        return None
    try:
        samples = int(row["sample_count"]) if row.get("sample_count") else 0
    except (TypeError, ValueError):
        samples = 0
    return BackboneNeighbor(
        prefix=prefix_byte,
        sample_count=samples,
        active=bool(row.get("active")),
        age_seconds=row.get("age_seconds"),
        last_rssi=row.get("last_rssi"),
        last_snr=row.get("last_snr"),
        ewma_rssi=row.get("ewma_rssi"),
        ewma_snr=row.get("ewma_snr"),
        best_score=row.get("best_score"),
        worst_score=row.get("worst_score"),
    )


def contact_row(row: dict) -> Optional[dict]:
    """Normalize one /api/companion/contacts row for node ingest.

    LIVE TRUTH (2026-09-18, probe C2): rows carry public_key (full
    hex, FIRST 2 chars = prefix), name, adv_type int (same ints as
    openhop_core: 2 repeater / 1 chat / 3 room / 4 sensor), flags,
    out_path_len, last_advert_timestamp + lastmod (epoch seconds;
    STALE rows exist - the 2024-advert case), gps_lat/gps_lon
    (0.0/0.0 = no position). Returns {prefix, name, node_class, lat,
    lon, last_advert} or None when the row has no usable identity.
    """
    prefix = row_prefix_of({"pubkey": row.get("public_key")})
    if prefix is None:
        return None
    name = str(row.get("name") or "").strip() or None
    adv_type = row.get("adv_type")
    try:
        node_class = _ADVERT_NIBBLE_CLASS.get(int(adv_type), 0) \
            if adv_type is not None else 0
    except (TypeError, ValueError):
        node_class = 0
    lat, lon = row.get("gps_lat"), row.get("gps_lon")
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        lat_f = lon_f = None
    if lat_f is not None and (lat_f == 0.0 and lon_f == 0.0):
        lat_f = lon_f = None  # 0.0/0.0 = no GPS, never publish it
    last_advert = row.get("last_advert_timestamp")
    try:
        last_advert = float(last_advert) if last_advert else None
    except (TypeError, ValueError):
        last_advert = None
    return {"prefix": prefix, "name": name, "node_class": node_class,
            "lat": lat_f, "lon": lon_f, "last_advert": last_advert}


# NODE ADAPTATION (2026-09-20, SEED-MAP): the seeded file ends here.
# RepeaterApiSource and its token/auth helpers stay in the plugin repo
# (the node IS the listener - it does not poll the repeater). The
# DemoSource and row helpers above are the part the brain consumes.



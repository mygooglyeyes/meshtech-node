"""Wire-frame codec for the modem protocol.

Frame format (identical on both directions of the socket):

    SYNC (0xAA) | CMD (1B) | LEN (2B LE) | PAYLOAD (0..255 B) | CRC-16 (2B LE)

- LEN covers PAYLOAD only.
- CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, no xor-out)
  computed over CMD + LEN + PAYLOAD; the SYNC byte is excluded.
- Radio payloads are capped at MAX_LORA_PAYLOAD (255 B, the LoRa radio
  MTU). The LEN field itself is u16, so framing handles anything up to
  65535 B; anything past the radio cap is refused at command level.

CRC-16/CCITT-FALSE check vector: crc16(b"123456789") == 0x29B1.

The parser is total: it never raises IndexError on hostile input. It
returns None while a frame is incomplete and raises FrameError (a
ValueError) only on a CRC mismatch, so the caller can answer
ERR_CRC_MISMATCH and resynchronise.
"""
from __future__ import annotations

import struct
from typing import Optional, Tuple

# ─── Framing ─────────────────────────────────────────────────────────
PROTO_SYNC = 0xAA
MAX_LORA_PAYLOAD = 255
MAX_WIRE_PAYLOAD = 0xFFFF          # LEN field limit (framing only)

# ─── Host → modem ────────────────────────────────────────────────────
CMD_TX_REQUEST = 0x01
CMD_SET_CONFIG = 0x10
CMD_GET_CONFIG = 0x11
CMD_STATUS_REQ = 0x20
CMD_NOISE_REQ = 0x22
CMD_CAD_REQUEST = 0x30
CMD_RX_START = 0x31
CMD_SET_CAD_PARAMS = 0x34
CMD_AUTH = 0x50
CMD_GET_VERSION = 0x70
CMD_PING = 0xFF

# ─── Modem → host ────────────────────────────────────────────────────
CMD_TX_DONE = 0x02
CMD_TX_FAIL = 0x03
CMD_RX_PACKET = 0x04
CMD_CONFIG_RESP = 0x12
CMD_STATUS_RESP = 0x21
CMD_NOISE_RESP = 0x23
CMD_CAD_RESP = 0x32
CMD_RX_STARTED = 0x33
CMD_CAD_PARAMS_RESP = 0x35
CMD_AUTH_OK = 0x51
CMD_VERSION_RESP = 0x71
# v0.0.173: modem -> controller only. One byte: count of connected
# observers. Sent on connect (initial state), and whenever an observer
# joins or leaves. The controller dashboard shows the openHop TCP
# push state from this - the old modem-feed chip was inert in modem
# mode because nothing ever told the bot who was listening.
CMD_OBSERVER_STATE = 0x72
CMD_ERROR = 0xFE
CMD_PONG = 0xFF

# ─── Error codes (CMD_ERROR payload[0]) ──────────────────────────────
ERR_CRC_MISMATCH = 0x01
ERR_INVALID_CMD = 0x02
ERR_RADIO_BUSY = 0x03
ERR_TX_TIMEOUT = 0x04
ERR_PAYLOAD_TOO_BIG = 0x05
ERR_INVALID_CONFIG = 0x06
ERR_CAD_FAILED = 0x07
ERR_RADIO_INIT = 0x08
ERR_UNAUTHORIZED = 0x09

# ─── Packed struct layouts ───────────────────────────────────────────
# RadioConfig (14 B): freq_hz(u32) | bandwidth_hz(u32) | sf(u8) | cr(u8)
#                     | power_dbm(i8) | syncword(u16) | preamble(u8)
RADIO_CONFIG_FMT = "<IIBBbHB"
RADIO_CONFIG_SIZE = struct.calcsize(RADIO_CONFIG_FMT)

# StatusResp (24 B): uptime | rx_count | tx_count | crc_errors
#                    | last_rssi | snr×10 | noise×10 | temp_c | radio_state
STATUS_RESP_FMT = "<IIIIhhhbBIII"
STATUS_RESP_SIZE = struct.calcsize(STATUS_RESP_FMT)

# RX_PACKET metadata (6 B) before the raw radio bytes:
# rssi (i16 LE, dBm) | snr×10 (i16 LE) | signal-rssi (i16 LE, dBm)
RX_META_FMT = "<hhh"
RX_META_SIZE = struct.calcsize(RX_META_FMT)

# TX_DONE payload: airtime in microseconds (u32 LE)
TX_DONE_FMT = "<I"

_HEADER = struct.Struct("<BH")          # CMD, LEN (after the SYNC byte)


class FrameError(ValueError):
    """CRC mismatch or structurally bad frame (caller should resync)."""


def _build_crc_table() -> Tuple[int, ...]:
    """Precomputed 256-entry table for CRC-16/CCITT-FALSE."""
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
        table.append(crc)
    return tuple(table)


_CRC_TABLE = _build_crc_table()


def crc16(data: bytes) -> int:
    """CRC-16/CCITT-FALSE, table-driven (one lookup per byte).

    Same result as the bitwise reference: crc16(b"123456789") == 0x29B1.
    """
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _CRC_TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc


def build_frame(cmd: int, payload: bytes = b"") -> bytes:
    """One wire frame: SYNC | CMD | LEN | PAYLOAD | CRC."""
    length = len(payload)
    if length > MAX_WIRE_PAYLOAD:
        raise ValueError(
            f"payload length {length} exceeds the 16-bit LEN field")
    hdr = _HEADER.pack(cmd, length)
    return bytes([PROTO_SYNC]) + hdr + payload + struct.pack(
        "<H", crc16(hdr + payload))


def parse_frame(buf: bytes) -> Optional[Tuple[int, bytes, int]]:
    """Parse the first complete frame in buf.

    Returns (cmd, payload, frame_size) or None if buf holds no complete
    frame yet. Raises FrameError on a CRC mismatch - the caller should
    answer ERR_CRC_MISMATCH and resynchronise past the bad SYNC.
    """
    sync_idx = buf.find(bytes([PROTO_SYNC]))
    if sync_idx < 0:
        return None
    if len(buf) < sync_idx + 4:        # SYNC + CMD + LEN not all present
        return None
    off = sync_idx
    cmd = buf[off + 1]
    (length,) = struct.unpack_from("<H", buf, off + 2)
    total = 4 + length + 2
    if len(buf) < off + total:
        return None
    payload = bytes(buf[off + 4:off + 4 + length])
    (crc,) = struct.unpack_from("<H", buf, off + 4 + length)
    hdr = _HEADER.pack(cmd, length)
    if crc16(hdr + payload) != crc:
        raise FrameError("CRC mismatch")
    return cmd, payload, off + total


def find_sync(buf: bytes, start: int = 0) -> int:
    """Index of the next SYNC byte at or after start (-1 if absent)."""
    return buf.find(bytes([PROTO_SYNC]), start)


def build_rx_packet(rssi: int, snr: float, signal_rssi: int,
                    data: bytes) -> bytes:
    """One RX_PACKET frame wrapping raw radio bytes with their metadata."""
    if len(data) > MAX_LORA_PAYLOAD:
        raise ValueError("radio payload too big")
    meta = struct.pack(RX_META_FMT, int(rssi), int(round(snr * 10)),
                       int(signal_rssi))
    return build_frame(CMD_RX_PACKET, meta + data)


def parse_rx_payload(payload: bytes) -> Tuple[int, float, int, bytes]:
    """Split an RX_PACKET payload into (rssi, snr, signal_rssi, data)."""
    if len(payload) < RX_META_SIZE:
        raise FrameError("RX_PACKET payload shorter than its metadata")
    rssi, snr_x10, signal_rssi = struct.unpack_from(RX_META_FMT, payload)
    return rssi, snr_x10 / 10.0, signal_rssi, payload[RX_META_SIZE:]


def parse_noise_payload(payload: bytes) -> float:
    """Decode a NOISE_RESP payload (noise*10, i16 LE) into dBm."""
    if len(payload) < 2:
        raise FrameError("NOISE_RESP payload shorter than 2 bytes")
    return struct.unpack("<h", payload[:2])[0] / 10.0


# NOISE_RESP sentinel (v0.0.183): the modem answers THIS when the radio
# read failed, and parse_noise_payload_or_none maps it to None so the
# dashboard shows a gap instead of a plausible-looking fake -105.0
# (meshtech-modem hard-coded exactly that value; cleanmodem's error
# path echoed it until now).
NOISE_NO_VALUE = struct.pack("<h", -32768)


def parse_noise_payload_or_none(payload: bytes) -> Optional[float]:
    """NOISE_RESP payload -> dBm, or None for the NO-VALUE sentinel."""
    if len(payload) >= 2 and payload[:2] == NOISE_NO_VALUE:
        return None
    return parse_noise_payload(payload)


def _sanitize(text: object, limit: int = 80) -> str:
    """Make peer-controlled text safe for log lines (no newlines,
    control characters, or ANSI escapes) and bounded in length."""
    cleaned = "".join(ch if ch.isprintable() else "?"
                      for ch in str(text or ""))
    return cleaned[:limit]

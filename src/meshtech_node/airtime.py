"""Estimated LoRa airtime for a packet (the standard Semtech formula).

Airtime is deterministic: it depends only on the payload size and the
radio settings (spreading factor, bandwidth, coding rate, preamble
length).  The scope feed's budget limiter and the PULSE "feed cost"
figure are computed from these numbers and labelled **est.** everywhere
- real collisions, retries and LBT backoff are invisible to us.

Defaults match the hilltop design point for the scope feed:
**SF7 / BW250** (coding rate 4/5, 32-symbol preamble).  The live values
come from the ``radio:`` section of config.json so a node on different
settings gets correct numbers by editing config, not code.

Reference: Semtech AN1200.13 "LoRa Modulation Basics" (payload symbol
count) with the explicit-header, payload-CRC-on uplink shape MeshCore
uses.  Same formula the answerbot ships (meshtech_answerbot/core/
airtime.py); kept in-tree so this plugin stays standalone.
"""
from __future__ import annotations

import math
from typing import Dict, Optional


def lora_airtime_ms(payload_bytes: int, *,
                    spreading_factor: int = 7,
                    bandwidth_khz: float = 250.0,
                    coding_rate_index: int = 1,     # 1 == 4/5
                    preamble_symbols: int = 32,
                    explicit_header: bool = True,
                    payload_crc: bool = True) -> float:
    """Estimated time on air for one LoRa packet, in milliseconds.

    ``payload_bytes`` is the LoRa payload length (PL).  Returns 0.0 for
    non-positive sizes rather than guessing.
    """
    size = int(payload_bytes or 0)
    if size <= 0:
        return 0.0
    sf = int(spreading_factor)
    bw_hz = float(bandwidth_khz) * 1000.0
    if sf < 5 or sf > 12 or bw_hz <= 0:
        return 0.0

    de = 1 if sf >= 11 else 0                 # low-data-rate optimization
    ih = 1 if explicit_header else 0
    crc = 1 if payload_crc else 0
    cr = max(0, min(3, int(coding_rate_index)))

    t_sym = (2.0 ** sf) / bw_hz              # seconds per symbol
    # SN = 8 + ceil( (8*PL - 4*SF + 28 + 16*CRC - 20*IH) / (4*(SF-2*DE)) ) * (CR+4)
    numerator = 8.0 * size - 4.0 * sf + 28.0 + 16.0 * crc - 20.0 * ih
    denominator = 4.0 * (sf - 2 * de)
    symbol_count = 8 + max(math.ceil(numerator / denominator) * (cr + 4), 0)
    preamble_count = preamble_symbols + 4.25
    return round((preamble_count + symbol_count) * t_sym * 1000.0, 2)


def airtime_from_settings(payload_bytes: int, radio: Optional[object]) -> float:
    """Estimate airtime using a config ``radio`` section (RadioCfg).

    Tolerant of a missing section (RadioCfg defaults apply) so callers
    never need to check.
    """
    return lora_airtime_ms(
        payload_bytes,
        spreading_factor=getattr(radio, "spreading_factor", 7),
        bandwidth_khz=getattr(radio, "bandwidth_khz", 250.0),
        coding_rate_index=getattr(radio, "coding_rate_index", 1),
        preamble_symbols=getattr(radio, "preamble_symbols", 32),
    )


def radio_summary(radio: Optional[object]) -> Dict[str, object]:
    """The radio settings that drive the estimate, for tooltips/labels."""
    return {
        "sf": getattr(radio, "spreading_factor", 7),
        "bw_khz": getattr(radio, "bandwidth_khz", 250.0),
        "cr_index": getattr(radio, "coding_rate_index", 1),
        "preamble": getattr(radio, "preamble_symbols", 32),
    }

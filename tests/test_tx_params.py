"""TX parameters + AGC reset - Brett's transmitter call (2026-09-30).

Three changes pinned here: send power 21 dBm default, 80 us ramp, and
an AGC reset bounce every 4 seconds. The ramp code below is the
reference driver's, verbatim from openhop_core's LoRaRF/SX126x.py
(PA_RAMP_80U = 0x03); the AGC interval is the MeshCore community
standard for `agc.reset.interval`.
"""
from cleanmodem import sx126x
from cleanmodem.config import ModemConfig

# Verbatim from the reference (LoRaRF/SX126x.py PA_RAMP_80U).
REF_PA_RAMP_80U = 0x03


def test_ramp_code_matches_the_reference() -> None:
    assert sx126x.PA_RAMP_80U == REF_PA_RAMP_80U


def test_send_power_default_is_21_dbm() -> None:
    assert ModemConfig().tx_power_dbm == 21
    radio = sx126x.SX126xRadio({}, force_null_hw=True)
    assert radio._radio["tx_power_dbm"] == 21


def test_agc_reset_interval_is_4_seconds() -> None:
    assert sx126x.SX126xRadio.AGC_RESET_S == 4.0

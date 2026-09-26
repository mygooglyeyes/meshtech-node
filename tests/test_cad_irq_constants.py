"""CAD IRQ constants - pinned to the reference driver.

AGENTS rule: verify wire constants against the reference source
(openhop_core) BEFORE shipping. The bug this pins (hilltop 2026-09-25
and 26, "radio work failed: CAD timed out" at every send): cleanmodem
waited for IRQ_CAD_DONE on 0x0100 - the DETECTED bit - so on a quiet
channel the "probe finished" poll matched nothing, the 150 ms
deadline always won, and every transmit died in the CAD pre-check.
The numbers below are the reference driver's, verbatim from
openhop_core/src/pymc_core/hardware/lora/LoRaRF/SX126x.py.
"""
import pytest

from cleanmodem import sx126x

# Verbatim from the reference (LoRaRF/SX126x.py lines 149-151).
REF_IRQ_CAD_DONE = 0x0080
REF_IRQ_CAD_DETECTED = 0x0100
REF_IRQ_TIMEOUT = 0x0200


def test_cad_irq_bits_match_the_reference() -> None:
    assert sx126x.IRQ_CAD_DONE == REF_IRQ_CAD_DONE
    assert sx126x.IRQ_CAD_DETECTED == REF_IRQ_CAD_DETECTED
    assert sx126x.IRQ_TIMEOUT == REF_IRQ_TIMEOUT


def test_irq_flag_bits_are_all_distinct() -> None:
    # THE COLLISION CLASS OF BUG: the old table had IRQ_CAD_DETECTED
    # and IRQ_TIMEOUT both on 0x0200 - two meanings on one bit.
    named = {
        "TX_DONE": sx126x.IRQ_TX_DONE,
        "RX_DONE": sx126x.IRQ_RX_DONE,
        "PREAMBLE_DETECTED": sx126x.IRQ_PREAMBLE_DETECTED,
        "SYNCWORD_VALID": sx126x.IRQ_SYNCWORD_VALID,
        "HEADER_VALID": sx126x.IRQ_HEADER_VALID,
        "HEADER_ERR": sx126x.IRQ_HEADER_ERR,
        "CRC_ERR": sx126x.IRQ_CRC_ERR,
        "CAD_DONE": sx126x.IRQ_CAD_DONE,
        "CAD_DETECTED": sx126x.IRQ_CAD_DETECTED,
        "TIMEOUT": sx126x.IRQ_TIMEOUT,
    }
    assert len(set(named.values())) == len(named), (
        f"duplicate IRQ bits: {named}")


def test_reference_driver_agrees_when_importable() -> None:
    """Compare against the REFERENCE ITSELF when its hardware deps
    are importable here; the literals above stand when they are not
    (honest skip, never a silent pass)."""
    try:
        from pymc_core.hardware.lora.LoRaRF.SX126x import SX126x  # noqa: PLC0415
    except Exception as exc:  # pragma: no cover - machine dependent
        pytest.skip(f"reference driver not importable here: {exc}")
    assert SX126x.IRQ_CAD_DONE == sx126x.IRQ_CAD_DONE
    assert SX126x.IRQ_CAD_DETECTED == sx126x.IRQ_CAD_DETECTED
    assert SX126x.IRQ_TIMEOUT == sx126x.IRQ_TIMEOUT
    # The CAD symbol-count byte cleanmodem sends (0x04 = 16 symbols).
    assert SX126x.CAD_ON_16_SYMB == 0x04

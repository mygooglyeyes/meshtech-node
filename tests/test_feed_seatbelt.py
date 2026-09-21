"""Feed seatbelt tests (2026-09-21, the 16:13 silent feed-killer).

Two invariants pinned after the live freeze on hilltop:
1. The background-summary rotation NEVER asks for section 0 (reserved
   whole-area since the v1.2 renumbering) - it walks 1..9 and wraps to 1.
2. One failing tick can no longer kill broadcast_loop: the error is
   logged, the cadence keeps running, and later ticks still fire.
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.config import Settings          # noqa: E402
from meshtech_node.service import ScopeService     # noqa: E402

TICK_SECONDS = 5.0   # broadcast_loop's between-ticks sleep (hardcoded)


class _FakeClient:
    is_connected = True
    has_slot = True

    async def send_channel_data(self, dt, payload):
        return False


class _Tap:
    """Records every packet the feed builds, TX or no-TX."""

    current_req_id = None

    def __init__(self):
        self.kinds = []

    def on_built_packet(self, dt, payload, *, would_tx, tx_ok=False,
                        in_reply_to=None):
        self.kinds.append((hex(dt), would_tx, tx_ok))


def _make_service():
    s = Settings()
    s.feed.burst_gap_seconds = 0.0
    svc = ScopeService(s, use_demo=True)
    svc.client = _FakeClient()
    svc.tx_enabled = False
    tap = _Tap()
    svc.feed_tap = tap
    return svc, tap


async def _wait_until(cond, timeout=15.0, step=0.1):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        await asyncio.sleep(step)
    return False


@pytest.mark.asyncio
async def test_background_rotation_never_asks_for_section_zero():
    """The counter that killed the feed: seed 1, rotate 1..9, wrap to 1."""
    svc, _tap = _make_service()
    b = svc.builder
    seen = [b._background_section]
    for _ in range(svc.geometry.section_count * 3):
        # exactly what broadcast_loop does on every cadence pulse:
        # building section 0 would raise CodecError (0 is reserved)
        b.build_sect_sum(b._background_section)
        b._background_section = (b._background_section
                                 % svc.geometry.section_count) + 1
        seen.append(b._background_section)
    assert 0 not in seen, "rotation must never target reserved section 0"
    assert seen[-1] == 1, "counter wraps back to 1, not 0"
    assert min(seen) == 1 and max(seen) == svc.geometry.section_count


@pytest.mark.asyncio
async def test_bad_tick_no_longer_kills_broadcast_loop(monkeypatch):
    """One failing tick: logged, survived; the NEXT cadence pulse still fires.

    Timing: the loop sleeps TICK_SECONDS between ticks, so each stage
    waits for the next real tick (poll, never a fixed sleep guess).
    """
    svc, tap = _make_service()
    task = asyncio.create_task(svc.broadcast_loop())
    ok = await _wait_until(
        lambda: any(k == "0x5305" for k, *_ in tap.kinds))
    assert ok, "seed layout never went out"

    # make build_sect_sum blow up ONCE, exactly like the 0-bug did
    real = svc.builder.build_sect_sum
    calls = {"n": 0}

    def boom_then_recover(section_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated mid-tick failure")
        return real(section_id)

    monkeypatch.setattr(svc.builder, "build_sect_sum", boom_then_recover)
    svc.builder._last_pulse = time.time() - 999   # pulse due immediately
    assert await _wait_until(lambda: calls["n"] >= 1), \
        "the pulse tick with the failing builder never ran"
    assert task.done() is False, "broadcast_loop must survive a bad tick"

    svc.builder._last_pulse = time.time() - 999   # and the cadence continues
    assert await _wait_until(lambda: calls["n"] >= 2), \
        "the next tick never ran after the failure"
    assert any(k == "0x5301" for k, *_ in tap.kinds), \
        "a pulse still gets built after the failure"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

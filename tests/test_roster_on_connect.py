"""Roster-on-connect tests (Brett, 2026-09-21).

The connect burst must hand a freshly opened app the WHOLE node
roster - not just the map frame - so the mapped-nodes count fills on
connect without a refresh press. Pinned here:

- the connect burst is LAYOUT, PULSE, then INTRO batches;
- every known node (positioned AND name-only) appears in the INTROs
  (the old independent-group rotation starved plain nodes forever -
  caught live while building this feature, fixed in feedbuilder);
- the roster send stops (no infinite stream of duplicate batches).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest  # noqa: E402

from meshtech_node.config import Settings       # noqa: E402
from meshtech_node.service import ScopeService  # noqa: E402
from meshtech_node import codec                 # noqa: E402


class _FakeClient:
    is_connected = True
    has_slot = True

    async def send_channel_data(self, dt, payload):
        return False


class _Tap:
    current_req_id = None

    def __init__(self):
        self.sent = []          # (data_type, payload)

    def on_built_packet(self, dt, payload, *, would_tx, tx_ok=False,
                        in_reply_to=None):
        self.sent.append((dt, bytes(payload)))


def _make_service(n_positioned=12, n_plain=8):
    svc = ScopeService(Settings(), use_demo=True)
    svc.settings.feed.burst_gap_seconds = 0.0
    for i in range(n_positioned):
        svc.store.add_position(0x10 + i, 37.70 + 0.01 * i,
                               -122.40 + 0.01 * i, f"pos{i}")
    for i in range(n_plain):
        svc.store.add_name(0x80 + i, f"plain{i}")
    svc.tx_enabled = False
    tap = _Tap()
    svc.feed_tap = tap
    svc.client = _FakeClient()
    return svc, tap


@pytest.mark.asyncio
async def test_connect_burst_carries_full_roster():
    """LAYOUT + PULSE + INTRO batches; every known node covered."""
    svc, tap = _make_service()
    await svc.pulse_now(reason="test-connect", with_layout=True)
    kinds = [dt for dt, _p in tap.sent]
    assert kinds[0] == codec.TYPE_LAYOUT
    assert codec.TYPE_PULSE in kinds, "health must fill on connect"
    # section summaries ride between LAYOUT and PULSE (2026-09-24)
    assert kinds.count(codec.TYPE_SECT_SUM) == 9
    assert codec.TYPE_INTRO in kinds, "roster must ride the connect burst"

    intros = [codec.decode_any(p)
              for dt, p in tap.sent if dt == codec.TYPE_INTRO]
    prefixes = [e.prefix for d in intros for e in d.entries]
    known = set(svc.store.known_nodes())
    missing = known - set(prefixes)
    assert not missing, f"roster incomplete on connect: {missing}"
    # positioned nodes lead the very first batch (cursor 0 semantics)
    assert prefixes[0] == 0x10


@pytest.mark.asyncio
async def test_roster_send_stops():
    """The roster send terminates: no runaway stream of dup batches."""
    svc, tap = _make_service()
    await svc.pulse_now(reason="test-connect", with_layout=True)
    intro_count = sum(1 for dt, _p in tap.sent if dt == codec.TYPE_INTRO)
    total_known = len(svc.store.known_nodes())
    # each batch fits ~9 entries; a full roster needs a handful, not 32
    assert intro_count <= 6, (
        f"roster send ran away: {intro_count} batches for "
        f"{total_known} nodes")


@pytest.mark.asyncio
async def test_plain_nodes_reach_the_air():
    """The rotation fix: name-only nodes are no longer starved.

    Regression pin for the feedbuilder bug: independent per-group
    rotation re-queued positioned nodes in every batch, so plain nodes
    NEVER fit any batch (invisible on air). Whole-list rotation fixed
    it; every third batch or so must contain plain entries.
    """
    svc, tap = _make_service()
    await svc.pulse_now(reason="test-connect", with_layout=True)
    intros = [codec.decode_any(p)
              for dt, p in tap.sent if dt == codec.TYPE_INTRO]
    prefixes = [e.prefix for d in intros for e in d.entries]
    plain_seen = [p for p in prefixes if p >= 0x80]
    assert len(plain_seen) == 8, (
        f"plain nodes starved: only {plain_seen} reached the air")

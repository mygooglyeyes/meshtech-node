"""Budget limiter + dedupe + rate limiter tests.

The feed must never overload the mesh - these pin the guards.
"""
from meshtech_node.airtime import lora_airtime_ms
from meshtech_node.budget import (BudgetLimiter, RefreshDedupe,
                                        RefreshRateLimiter)
from meshtech_node.config import RadioCfg


def test_airtime_sf7_bw250_small_packet():
    # ~60-byte plaintext scope packet at the design point: tens of ms.
    ms = lora_airtime_ms(60, spreading_factor=7, bandwidth_khz=250.0)
    assert 0 < ms < 150
    # SF7/BW250 carries ~6x the bits per second of SF7/BW62.5 -> shorter.
    ms_slow = lora_airtime_ms(60, spreading_factor=7, bandwidth_khz=62.5)
    assert ms < ms_slow


def test_budget_cap_packets_per_hour():
    radio = RadioCfg()
    lim = BudgetLimiter(radio, max_packets_per_hour=3, max_duty_percent=100.0,
                        now=0.0)
    t = 0.0
    assert lim.allow(60, now=t) and lim.record(60, now=t) > 0
    t += 60
    assert lim.allow(60, now=t) and lim.record(60, now=t)
    t += 60
    assert lim.allow(60, now=t) and lim.record(60, now=t)
    t += 60
    assert not lim.allow(60, now=t)      # 4th packet inside the hour: NO
    t += 3700                            # window slides past the first sends
    assert lim.allow(60, now=t)


def test_budget_cap_duty_percent():
    radio = RadioCfg()
    # 1% duty = 36 s TX/hour. Max packets huge; the duty cap binds.
    lim = BudgetLimiter(radio, max_packets_per_hour=1000, max_duty_percent=1.0,
                        now=0.0)
    t = 0.0
    sent = 0
    while lim.allow(120, now=t) and sent < 500:
        lim.record(120, now=t)
        sent += 1
        t += 1.0
    assert sent < 500                    # duty cap stopped the flood
    spent = lim.tx_seconds_last_hour(now=t)
    assert spent <= 37                   # ~36 s cap (rounding headroom)


def test_dedupe_window():
    d = RefreshDedupe(ttl_seconds=600.0)
    assert not d.seen_before(1, 2, 3, now=0.0)
    assert d.seen_before(1, 2, 3, now=100.0)      # same nonce inside TTL
    assert not d.seen_before(1, 2, 4, now=100.0)  # different nonce: fresh
    assert not d.seen_before(1, 2, 3, now=700.0)  # TTL expired: fresh again


def test_rate_limiter_cooldown_and_cap():
    lim = RefreshRateLimiter(cooldown_seconds=30.0, hourly_cap=3)
    t = 0.0
    assert lim.allowed("aabbcc", now=t)
    lim.record("aabbcc", now=t)
    assert not lim.allowed("aabbcc", now=t + 5)   # cooldown
    t += 31
    assert lim.allowed("aabbcc", now=t)
    lim.record("aabbcc", now=t)
    t += 31
    assert lim.allowed("aabbcc", now=t)
    lim.record("aabbcc", now=t)
    t += 31
    assert not lim.allowed("aabbcc", now=t)       # hourly cap hit
    assert lim.allowed("ddeeff", now=t)           # other client unaffected


def test_rate_limiter_acl():
    lim = RefreshRateLimiter(30.0, 10, allowed_prefixes=["aabb"])
    assert lim.allowed("aabbcc112233")
    assert not lim.allowed("ffffaabbccdd")


# --------------------------------------- v1.3 per-size refresh caps (Brett)

def test_span_caps_load_balanced():
    """Brett's limits: 60 km = 1/h, 40 km = 2/h, 20 km = 3/h.
    Packet math (MAP-SIZE-DESIGN section 5): 12 x 1, 7 x 2, 4 x 3 ->
    every level ~12-14 packets/hour."""
    lim = RefreshRateLimiter(cooldown_seconds=0.0, hourly_cap=10)
    assert lim.span_cap(60.0) == 1
    assert lim.span_cap(40.0) == 2
    assert lim.span_cap(20.0) == 3
    assert lim.span_cap(0) == 10        # legacy (no size) keeps config cap
    assert lim.span_cap(45.0) == 2      # snapped to 40


def test_size_buckets_are_independent():
    """A 60 km ask never consumes a 20 km slot: buckets key on the
    snapped size, so one client can hold one slot at each level."""
    lim = RefreshRateLimiter(cooldown_seconds=0.0, hourly_cap=10)
    t = 100.0
    assert lim.allowed("aabbcc", now=t, span_km=60.0)
    lim.record("aabbcc", now=t, span_km=60.0)
    assert not lim.allowed("aabbcc", now=t + 1, span_km=60.0)  # 60 pool spent
    assert lim.allowed("aabbcc", now=t + 2, span_km=40.0)      # 40 pool free
    lim.record("aabbcc", now=t + 2, span_km=40.0)
    assert lim.allowed("aabbcc", now=t + 3, span_km=40.0)      # 2nd of 2
    lim.record("aabbcc", now=t + 3, span_km=40.0)
    assert not lim.allowed("aabbcc", now=t + 4, span_km=40.0)
    assert lim.allowed("aabbcc", now=t + 5, span_km=20.0)      # 20 pool free


def test_span_cap_per_client_buckets():
    """The per-client limiter buckets by size: one client's 60 km ask
    does not consume another client's 60 km slot HERE - the GLOBAL
    per-size pool (all clients pooled, webserve's _GlobalRefreshBudget)
    is the layer that refuses the second client (test_webserve)."""
    lim = RefreshRateLimiter(cooldown_seconds=0.0, hourly_cap=10)
    t = 100.0
    assert lim.allowed("aabbcc", now=t, span_km=60.0)
    lim.record("aabbcc", now=t, span_km=60.0)
    assert not lim.allowed("aabbcc", now=t + 1, span_km=60.0)  # own pool spent
    assert lim.allowed("ddeeff", now=t + 1, span_km=60.0)      # own 60 km slot
    # ...and either client can still ask other sizes
    assert lim.allowed("aabbcc", now=t + 2, span_km=20.0)


def test_legacy_unsized_refresh_unchanged():
    """No span_km on the ask (old clients): the configured hourly cap
    applies exactly as before v1.3."""
    lim = RefreshRateLimiter(cooldown_seconds=0.0, hourly_cap=3)
    t = 100.0
    for i in range(3):
        assert lim.allowed("aabbcc", now=t + i)
        lim.record("aabbcc", now=t + i)
    assert not lim.allowed("aabbcc", now=t + 10)

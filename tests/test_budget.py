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

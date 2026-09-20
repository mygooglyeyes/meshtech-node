# meshtech-node - FEEDBUILDER CHANGES (drafted 2026-09-20, for Brett's review)

The exact deltas to apply when the scope brain seeds into meshtech-node
(SEED-MAP verdict: feedbuilder/budget/service logic REUSE). These are
NOT changes to the live hilltop plugin - the plugin keeps its current
schedule until cutover. Every fact below is verified against the real
code (service.py, feedbuilder.py, budget.py, config.py), file/line
cited.

## Change 1 - LAYOUT every 10 minutes

- config.py: `max_packets_per_hour` neighbor
  `feed.layout_interval_seconds` default 3600 -> 600; same in the
  loader and the node's default config.yaml. The broadcast loop
  already reads this value (service.py:387) - no loop change needed.
- Bonus (1 packet): send the first LAYOUT immediately at link-ready
  instead of seeding `_last_layout = now` (service.py:380), which
  currently defers it a full interval. Radio-only clients get a grid
  in seconds after host start; direct-mode clients get it from
  WebServe anyway.
- multi_host beacon stays off (config), unchanged.

## Change 2 - INTRO joins the background rotation

- service.py broadcast_loop, pulse cycle (line ~409): the burst
  `[pulse, sect]` becomes `[pulse, sect, intro]` where
  `intro = self.builder.build_intro_batch()` - the cursor mechanics
  (`_intro_cursor`, feedbuilder.py:81) already exist; reuse as-is.
- Guard: when the store has no rows, build_intro_batch returns empty
  and the loop sends the old two-packet burst. Never an empty batch.
- Effect: 3 packets per 5-min cycle. Batches of ~8-10 nodes -> 63
  nodes sweep in ~7-8 cycles = ~35-40 min. Optional knob
  `intro_every_cycles` (default 1; set 2 for a ~70-80 min sweep at
  half the cost) - Brett's tuning, not a v1 requirement.

## Change 3 - budget 26 -> 60 pkt/h

- config.py:65 `max_packets_per_hour: int = 26` -> 60 (loader line
  252 too) and the node's default config.yaml. Duty cap stays 1%.
- Honest math (the piece my PHONE-CONNECT worst case glossed over):
  base load becomes 42/h (36 background + 6 LAYOUT), leaving ~18/h =
  ONE whole-area refresh answer guaranteed + part of a second. Five
  phones refreshing flat out (55 packets) + background = ~97/h > 60.
  With background-first dropping (Change 4) the answers win and the
  ROTATION shows honest gaps - correct priority, but if real load
  shows rotation starvation, the fix is raising to ~100-120/h (duty
  would still be ~0.2% vs the 1% allowance). Brett's call when real
  load says so; recorded, not silently changed.

## Change 4 - background-first dropping

Two findings in the current code shape this:

- `_send_burst` (service.py:186-190) drops the WHOLE TAIL of a burst
  at the first over-budget packet (`return`, not `continue`).
- Background cycles and refresh answers share one gate with no
  priority: whichever burst runs when the budget is full loses.

Changes:
1. PRE-FLIGHT for background cycles: before building the pulse cycle,
   check the limiter's remaining packet count; if a full cycle (3
   packets + margin) would not fit, SKIP the cycle entirely (log the
   honest skip). Background never spends tokens an answer arriving
   seconds later would need.
2. `_send_burst` drops PER PACKET and continues (not `return`), so one
   refusal no longer kills an answer burst's tail - a dropped middle
   packet is an honest hole the client re-requests (PHONE-CONNECT gap
   rule).
3. The 5/h per-phone limit maps onto the EXISTING RefreshRateLimiter
   (budget.py: per-client cooldown + hourly cap) - configured, not
   written new. Node-side over-limit stays honest silence; app-side
   lockout per PHONE-CONNECT.

## Explicitly NOT in this change set

- SNAP: `build_snapshot` exists and is never called. Wiring it as a
  one-request full-state primitive stays a future option - Brett's
  call, not bundled here.
- Wire format: zero codec changes. INTRO/SECT_SUM/LAYOUT bodies are
  untouched - only WHEN they are sent changes. Existing app decoders
  keep working.

## Tests to add (node copy: test_service.py, test_budget.py)

- Rotation sends 3 packets/cycle; empty store sends 2.
- Pre-flight skip when remaining budget < cycle size.
- Per-packet drop-and-continue: middle packet refusal does not kill
  the burst tail.
- Config defaults: 600s layout, 60/h budget, intro_every_cycles=1.
- Rate limiter: 6th request from the same prefix in an hour refused.

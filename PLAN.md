# meshtech-node - PLAN (updated 2026-09-20 ~00:30 PDT - Brett's simplification applied: NO repeating)

## What this is

One standalone program for hilltop that owns the PiMesh 1W v2 radio
(the board the openhop repeater uses today) and does two jobs in one
process:

1. LISTENER - hears the mesh and preserves FULL packet headers
   (transport codes, paths, RSSI) - the thing the openhop repeater
   strips from GRP_DATA.
2. SCOPE FEED - builds the mesh health/structure picture and broadcasts
   it on #scope (pulse / background / layout / intro packets, and
   answers to client refresh requests).

NO REPEATING - Brett's explicit call (2026-09-20): "we do not need the
node to repeat. just listen and broadcast the data we need." When the
openhop repeater stands down, hilltop stops repeating mesh traffic;
that tradeoff is accepted and recorded here so no future session
re-adds repeating without Brett's go.

Replaces the openhop repeater's role for OUR data. openhop_core is a
read-only reference/library - never modified.

## Why (Brett's words)

- "we can't use openhop as it strips the header information of
  GRP_DATA. we need a repeater software that doesn't do that."
- "we do not need the node to repeat. just listen and broadcast the
  data we need."

Owning the radio in our own process fixes the header problem
structurally: packets never cross a boundary where headers can be
lost, and the scope brain finally sees the real packets.

## Where the code comes from

Module-by-module verdicts with reasons: SEED-MAP.md in this folder
(summary below; the map is the detail).

REUSE AS-IS (from C:\projects\lora-bot\.freebuff - proven on this box):
- cleanmodem/ - the whole direct-radio layer: clean-room SX126x driver,
  HAL (native SPI: spidev + gpiod - matches the modem, which is
  SPI/serial only, ONE radio, no ch341), LBT + CAD + politeness gap on
  TX, watchdog re-init, two-role TCP server (observer + controller).

REUSE / ADAPT:
- core/mcp.py - MeshCore packet type/route/transport-code parsing,
  group channel decrypt, flood-dedupe hashing (the listener pipeline).
- meshtech_scope.core (from C:\projects\meshtech-scope) - the feed
  brain: feedbuilder, observations, grid, peers, budget, codec.

WRITE NEW (the glue):
- In-process packet source replacing the plugin's web-API poller.
- Direct-radio sender replacing the plugin's companion link, including
  TX-side channel encryption (mirrors the decrypt code in mcp.py).
- WebServe [C] - small HTTP + WebSocket server exposing the live feed
  and listener state to the web app (bot dashboard server patterns).
- scope-app: a direct-data mode (WebSocket client) alongside radio
  mode, rendering the same packets from either source.
- Service shell - config file, logging, systemd unit - on the
  answerbot's standalone patterns (loud task-death logging, honest
  send semantics, startup order rules).

## Architecture (sketch 2026-09-20, grounded in the real seams)

    ┌─ cleanmodem process (REUSED AS-IS) ──────────────┐
    │ SX1262 over native SPI · watchdog · LBT/CAD gate │
    │   RX feed (every packet) ↓        ↑ TX requests │
    └──────────────┼────────────────────┼─────────────┘
             loopback TCP (one port, two roles:
             observer = RX feed, controller = TX)
    ┌──────────────▼────────────────────┴─────────────┐
    │ meshtech-node (the ONE new program)             │
    │                                                 │
    │   [A] RawPacketSource          [B] RadioSender  │
    │    mcp.py parse:                build + encrypt │
    │    type/route/transport/path,   #scope packet,  │
    │    group decrypt                CMD_TX_REQUEST  │
    │      │ Observations → queue          ↑          │
    │      └──────→ ScopeService ──────────┘          │
    │               (scope brain, reused:             │
    │                RollingStore · FeedBuilder ·     │
    │                codec · BudgetLimiter)           │
    │                       │                         │
    │           [C] FeedTap + WebServe (NEW)          │
    │    every packet the feed builds is ALSO served  │
    │    to the web app over WebSocket - works with   │
    │    TX off (listen-only) and on (dual path)      │
    └─────────────────────────────────────────────────┘

- RX: cleanmodem's observer role fans every heard packet to the node -
  headers/transport codes/paths/RSSI intact (nothing leaves our stack).
  [A] parses (mcp.py) and pushes Observations into the SAME asyncio
  queue the old web-API poller fed - verified seam: source.run(queue,
  stop); the brain cannot tell the difference. Flood-dedupe hashing
  lives in [A] so mesh re-broadcasts do not double-count.
- TX: FeedBuilder -> codec body -> BudgetLimiter -> [B] wraps the body
  in a channel packet (TX-side encrypt, adapted from mcp.py's
  modem-mode senders) -> CMD_TX_REQUEST -> cleanmodem's LBT/CAD gate ->
  air. Refresh answers ride the same loop (on_packet -> [B]).
- [C] FeedTap + WebServe: every packet the FeedBuilder produces is
  ALSO handed to a small WebSocket server (bot dashboard patterns) so
  the web app can consume the feed DIRECTLY - no radio hop. In
  listen-only this is the ONLY delivery path; after TX-on it becomes a
  second path the app can cross-check against radio-heard packets.
  Refresh answers work here too (request heard on RX, answer served
  over the wire) - everything proven except the final radio hop.
- cleanmodem stays its OWN process (recommended): proven shape, radio
  crash cannot take the brain down (node reconnects with backoff).
  Embedding the HAL in-process = new integration work, no Gate 0 gain.

## Phone-era feed schedule (added 2026-09-20 ~01:00 - Brett approved
## the faster schedule + a strict phone request limit)

Why (timing math, sanity check 2026-09-20):
- What the feed broadcasts today: PULSE every 5 min; one SECT_SUM per
  5-min slot rotating through 9 sections; LAYOUT hourly; INTRO (node
  names/dots) NEVER on a schedule - it only rides refresh answers.
- Passive phone (no refresh): grid ~30 min avg (worst 60+), all 9
  sections ~45 min, names NEVER. Loss is expensive, not lossy repeats
  (dedupe eats those): miss the hourly LAYOUT = wait another hour;
  modeled 25% loss -> sections ~60 min, 2 h+ tails. Observed reality
  2026-09-18: map drew ~2 h after connect.
- With one whole-area refresh (kind=1 target=0): 11 packets, ~20-35 s
  to a full map; ~1-2 min in messy air. Therefore REFRESH-ON-CONNECT
  IS THE PHONE APP'S DEFAULT BEHAVIOR, not a fallback.

Changes approved:
1. LAYOUT interval 3600s -> 600s (config). Passive grid wait drops to
   avg ~5 min. Cost: 5 packets/h.
2. INTRO joins the background rotation (code change): one INTRO batch
   per 5-min cycle, batches of ~8-10 nodes rotating. 63 nodes -> full
   name sweep ~35-40 min passive. Background becomes 3 packets/cycle
   = 36/h.
3. Budget raised 26 -> 60 pkt/h (config). Duty: 60 x ~55ms = ~3.3s/h
   = ~0.09% vs the 1% allowance - packet count, not airtime, is the
   real limit. Background stays first-dropped so client answers win.
4. PHONE REQUEST LIMIT (Brett, strict, to start): 5 refreshes/hour
   per phone. Enforced BOTH sides, no new wire types: the node counts
   per requester and stays silent over-limit (honest silence); the
   app self-counts, disables the button, shows a countdown. Tunable
   after real-world observation.

Honest flag: 4 phones x 5/h x 11 packets = 220/h > 60/h budget ->
later phones see honest gaps. If that bites, raise the budget (duty
has ~10x headroom) or add a node-wide refresh pool - Brett's call
when real load says so.

## Gates (Brett's explicit go at each)

- Gate 0 - BENCH: the whole program proven on a bench radio FIRST.
  Full pass/fail checklist: BENCH-CHECKLIST.md in this folder.
  cleanmodem's TX/RX round-trip gets re-proved (the bot stack went
  off-air mid-incident with TX provability unresolved - nothing builds
  on it until the bench proves it again). The listener sees packets
  with full headers; the feed's test packets reach the web app through
  [C]; a second radio hears nothing (TX still off).
- Gate 1 - LISTEN-ONLY LIVE (Brett's staging, 2026-09-20: prove the
  data feed BEFORE anything goes on air). Brett stands down the
  openhop repeater; meshtech-node takes the radio with TX DISABLED
  (config-gated: default-off flag + loud "TX DISABLED - listen-only"
  startup line + the sender refuses without it). The node listens and
  serves the web app the COMPLETE data feed - health card, map,
  sections, routes, refresh answers over the wire. Zero airtime.
  (The scope plugin stops with the repeater, but the web app keeps
  being fed - visibility never goes dark. Bonus: the app gets the
  LAYOUT packet at start, so the map draws immediately instead of
  waiting for the hourly broadcast.)
- Gate 2 - TX ON (Brett's explicit go): config flip; the feed goes on
  air; the web app can cross-check direct-served vs radio-heard.
- Gate 3 - STEADY STATE: watch a day - listener health, feed cadence,
  refresh answers working end to end on the air.

## Known risks / honest flags

- cleanmodem TX is UNPROVEN since the incident - Gate 0 exists for
  exactly this and nothing skips it.
- The feed is the ONLY transmitter on this radio: the scope budget
  limiter stays as the duty guard.
- TX is config-gated OFF through Gate 1 (default-off flag + loud
  startup line + sender refuses without it) - no accidental airtime
  while proving the feed.
- Hilltop stops repeating mesh traffic when the openhop repeater stands
  down - accepted by Brett 2026-09-20 (recorded above).

## Open questions

- RESOLVED 2026-09-20: the Gate 0 RADIO bench runs on HILLTOP with the
  openhop repeater stopped (Brett's call) - the radio half of Gate 0
  gets a hilltop window, software bench already runs locally.
- Provisional name "meshtech-node" - rename free.
- git init in this folder: on Brett's word only (rule zero).

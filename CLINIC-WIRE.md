# CLINIC-WIRE.md — the mesh clinic's facts, in bytes (Phase 1)

Written BEFORE any bytes were wired (the plan's rule). This page
governs the implementation: anything not described here does not
exist on the wire. One page, plain words, byte tables.

## What this is for

Several boxes listen to the mesh (and to each other's data bursts).
Each box answers, from what IT heard: how is each node doing, how is
each route doing, what looks wrong, and what did the other boxes say.
Those answers ride the air as ONE new packet type so a phone can
later draw the health layer. This page defines exactly those bytes.

## One new packet type: CLINIC (0x5314)

Everything rides in ONE type. Inside, a stream of small RECORDS,
each with its own kind. This is the only new wire type Phase 1 adds.

```
CLINIC packet:
  data_type (2 LE) = 0x5314
  data_len  (1)    = length of body
  body:
    header (5)     = proto_version(1)=0x05 + seq(2 LE) + origin(2 LE)
                     origin = the box SENDING this packet
    count   (1)    = number of records (0..7)
    records (each): kind(1) + len(1) + payload(len bytes)
```

Whole packet stays <= 163 bytes (the channel data cap), so at most
7 records ride at once. The sender walks its facts with a rotating
cursor: every batch carries the NEXT slice, so all facts cycle.

**Provenance rule (the whole point):** every record carries
`source` (2 LE) = the box that MEASURED or REPORTED the fact.
- source == packet origin: first-hand (this box's own radio).
- source != packet origin: second-hand (a peer box said it).
Second-hand facts are never merged into first-hand ones and never
re-worded as first-hand. Disagreement between boxes is preserved.

## Record kind 1 — NODE FACT (one node's chart)

```
  source        u16 LE   which box measured this
  prefix        u8       node key (first pubkey byte)
  last_age_min  u16 LE   minutes since last heard (65535 = older)
  age_days      u16 LE   days since first heard
  strip         3 bytes  availability, last 24 hours:
                         bit 0 = oldest hour ... bit 23 = current hour
                         1 = heard at least once that hour
  hops_typ      u8       typical radio hops to reach it
                         (repeater trail length + 1; 0 = unknown)
  share_pct     u8       percent of IDENTIFIED traffic in 24 h
                         (255 = unknown / no traffic)
  snr_ewma      i8       smoothed SNR, quarter-dB units (like discover)
  snr_best      i8       best SNR heard, quarter-dB
  snr_worst     i8       worst SNR heard, quarter-dB
  snr_sd        u8       SNR standard deviation, quarter-dB
  rssi_ewma     i8       smoothed RSSI, dBm
  rssi_best     i8       best RSSI heard, dBm
  rssi_worst    i8       worst RSSI heard, dBm
  rssi_sd       u8       RSSI standard deviation, dB
```

Payload 20 bytes. Signal stats are "as THIS box heard it" (its own
radio's numbers) — never another box's numbers wearing ours.

## Record kind 2 — ROUTE FACT (one route's chart)

```
  source        u16 LE   which box measured this
  path_len      u8       1..8 (the repeater trail)
  path          path_len bytes (trail, travel order)
  uses          u16 LE   times traffic used this route
  direct        u8       1 = heard straight from sender, 0 = via trail
  delay_min_s   u16 LE   smallest measured end-to-end delay (0 = unknown)
  delay_med_s   u16 LE   median (0 = unknown)
  delay_max_s   u16 LE   largest (0 = unknown)
  last_age_min  u16 LE   minutes since last use
  age_days      u16 LE   days since first use
```

Delay facts come only from honest sender timestamps (same rule the
route table already keeps). The 7/14-day death laws are unchanged —
a dead route is deleted everywhere and stops appearing.

## Record kind 3 — TROUBLE FLAG (a fact that deserves a look)

Flags are FACTS with evidence, never verdicts. A flag never says
who is "bad" — it says what was measured.

```
  source        u16 LE   which box measured this
  flag          u8       1 = advert sig-fail, 2 = timestamps backwards,
                         3 = rate storm, 4 = corrupt share
  subject       u8       key prefix concerned; 0 = mesh-wide
  events        u16 LE   how many times measured
  first_age_min u16 LE   minutes since first event
  last_age_min  u16 LE   minutes since most recent event
  detail        u16 LE   kind-specific (below)
```

Flag meanings and thresholds (constants, named in code):

| flag | subject | meaning (exact words shown to people) | detail | trigger |
|------|---------|----------------------------------------|--------|---------|
| 1 sig-fail | claimed key prefix | "bytes claiming this key failed signature checks (broken node OR impersonation — the flag does not pick)" | 0 | every failed check |
| 2 ts-backwards | key prefix (verified ads only) | "advert timestamps went backwards (replay, reset, or drift)" | worst jump, seconds | new stamp < previous best - 300 s |
| 3 rate storm | key prefix | "packets from this key arriving far faster than advert cadence" | peak, packets/min | >= 20 identity-bearing packets in 60 s |
| 4 corrupt share | 0 (ALWAYS mesh-wide) | "corrupt packets are X% of heard traffic (band noise or a broken transmitter — never blamed on a sender)" | share, per-mille | >= 10% corrupt over 10 min, >= 20 packets heard in window |

Corrupt packets are shown as mesh-level noise and are NEVER blamed
on a sender — flag 4's subject is always 0.

## Record kind 4 — PEER REPORT (what another box said)

A box folds the data bursts it hears from other boxes (PULSE,
SECT_SUM, ROUTE, INTRO) into its own picture, tagged with which box
said it. Those tagged facts ride on as second-hand records:

```
  source        u16 LE   the peer box that said it (never us)
  report        u8       1 = pulse, 2 = sect_sum, 3 = route, 4 = intro
  subject       u16 LE   0 (pulse) / section id / route id / node prefix
  heard_age_min u16 LE   minutes since the peer said it
  body          rest, report-specific:
    pulse:    uptime_min u16, rx_per_hour u16, active_total u16,
              airtime_s_per_h u16
    sect_sum: active u16, packets u16, delay_p50 u16, delay_p90 u16
    route:    uses u16, delay_med_s u16, last_age_min u16,
              path_len u8 + path bytes
    intro:    class u8, lat_e7 i32 LE, lon_e7 i32 LE,
              name_len u8 + name bytes (<= 24, UTF-8)
```

## What is folded and what is not

- Folded (tagged with the peer's origin): PULSE, SECT_SUM, ROUTE,
  INTRO bursts heard over the air from an origin that is neither 0
  nor my own. Origin 0 (anonymous) cannot be attributed to any box —
  counted out loud and dropped, never invented.
- NOT folded: my own echoes (origin = mine), and CLINIC packets
  heard from other boxes (Phase 1 scope: the four burst types only).
- Anti-stomping rules are untouched: section ownership election,
  lowest-origin rule, rate limits, airtime gates all behave exactly
  as before. Peer LAYOUT tracking (PeerTable) is untouched.

## Isolation baseline

With ZERO peers heard, a box produces a complete clinic from its own
direct facts alone. Peer reports only ADD; nothing depends on them.

## Emission rules

- One CLINIC batch per pulse beat (the audience-gated cadence —
  hard rule 6 unchanged: no listener, no airtime).
- Capped: one packet per beat, <= 163 bytes, cursor-rotated so every
  fact cycles through. The normal budget limiter governs like always.
- Refresh answers and request/answer behavior are UNCHANGED. Clinic
  packets never appear in answers and never answer asks.
- Every built packet goes through the door tap like all others.

## Persistence (restarts lose nothing)

Four stores, all SQLite write-through like the node table already is:
node charts (strip, signal stats, hop histogram, 24 h counts),
trouble flags, peer reports, and route delay min/max columns.
Boot refills all of them. No raw packets are ever stored.

## Honesty rules carried forward

- Missing numbers stay missing (sentinels like 0/255 mean "unknown",
  never a plausible constant).
- Signal is not distance.
- Second-hand facts stay visibly second-hand.
- A flag is evidence, not a verdict.

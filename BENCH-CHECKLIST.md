# meshtech-node - GATE 0 BENCH CHECKLIST (drafted 2026-09-20; updated
# 2026-09-20 after the software-bench build - see status block)

Gate 0 rule: EVERY item below passes on the bench before anything is
installed on hilltop. A failure is something to fix and re-run - never
skipped. Evidence = the actual log lines / app screenshots, recorded in
a bench report (BENCH-REPORT.md in this folder, started empty).

Honest framing: this checklist exists because the bot stack went
off-air mid-incident with "TX never provably transmitted" unresolved.
"cleanmodem said TX_DONE" is NOT proof. Proof = an INDEPENDENT second
radio hears the packet on the air.

## STATUS 2026-09-20 (software bench built; radio bench pending)

Brett's call: the RADIO bench runs on HILLTOP with the openhop
repeater stopped. The SOFTWARE half of several items is already proven
in the node's test suite (124 pass + 6 skip, 2026-09-20):

- Item 2 (wire truth): channel crypto PROVEN against the reference -
  golden vectors generated FROM openhop_core's CryptoUtils
  (test_packets.py); frame layout verified byte-order against
  Packet.read_from (split_frame tests); MeshCore route constants
  verified (and a bot-inherited 0/1 swap corrected). TX-side encrypt
  PROVEN 2026-09-20: Adapter B's frames parse with the reference
  Packet.read_from, HMAC verifies, decrypt to the exact plaintext
  (test_radiosender.py). REMAINING: the radio parameter line-by-line
  (hardware step).
- Item 5 software half: full-header preservation proven -
  last_scope_frame keeps transport codes + path intact
  (test_last_scope_frame_keeps_full_header). REMAINING: the on-air
  byte-for-byte round trip against a second radio.
- Item 6 software half: dedupe scripted set exact (test_packets.py +
  test_rawsource.py: N=3/M-dup counters match the script; window
  expiry honest; own-TX 120 s window). REMAINING: the same script
  over the air.
- Item 7 dependencies BUILT 2026-09-20: WebServe [C] (webserve.py +
  node.py shell + config.bench.json) + scope-app direct mode
  (directclient.ts + App.ts source switch + ScopeState.resetAll).
  Section 7's software half LIVE-PROVED on the running bench node:
  app connect -> instant map from the served LAYOUT; pulse/sect_sum
  cadence taps through; wire refresh -> tagged answer burst (ack +
  10 packets); every packet line [direct]-tagged; TX refusals loud.
  The section-7 boxes below stay unchecked: their full proofs need
  the on-air window (second-radio silence, radio-mode refresh heard
  on air, byte-for-byte sender-boundary comparison).
- Adapter B (RadioSender) BUILT 2026-09-20 with its acceptance tests
  green (reference-parser proof, TX-off guard, framing refusals,
  loopback suppression via the shared dedupe). Section 4's remaining
  proof is the ON-AIR half only.

Sections below keep their boxes unchecked: a box is checked at bench
time with evidence, never by a software test alone.

---

## 1. Setup (before any test)

- [ ] Bench rig RESOLVED 2026-09-20 (Brett): hilltop, openhop
      repeater STOPPED for the window. Schedule the window with Brett;
      his hand stops the repeater, his hand restarts it after.
- [ ] TWO radios on the bench: the bench radio (cleanmodem drives it)
      + a second radio (the desk radio) as the independent receiver.
- [ ] Bench config written: radio parameters copied from hilltop's
      CURRENT /etc/openhop_repeater/config.yaml radio block at prep
      time (do NOT assume cleanmodem's defaults are current - verify
      frequency/SF/BW/sync word/preamble/power line by line).
- [ ] #scope key present in the bench config from the box config;
      NEVER committed to git (rule zero).
- [ ] TX-off default VERIFIED IN CODE before power-up: config flag
      defaults to off, the sender refuses to transmit without it, and
      startup prints a loud "TX DISABLED - listen-only" line.

## 2. Wire-truth desk check (BEFORE any bench TX)

Rule (AGENTS.md, v0.0.098 lesson): never discover wire constants via a
bench test. Verify against the reference first.
SOFTWARE HALF DONE 2026-09-20 - see STATUS block; TX-side encrypt +
radio parameters remain.

- [ ] MeshCore packet build (header, transport codes, path, channel
      hash, AES-CTR payload encryption) checked against openhop_core's
      packet layer - read-only reference, line by line.
- [ ] Scope body format checked against meshtech_scope codec tests
      (the v0.2.5 wire-truth tests) - the body is what goes INSIDE the
      encrypted channel payload.
- [ ] GRP_TXT decrypt in mcp.py confirmed against the same reference
      (decrypt correctness = encrypt correctness mirror).

## 3. cleanmodem RX proof

- [ ] Bench radio in RX; send a plain text message from the second
      radio (MeshCore app). cleanmodem's observer feed delivers it to
      the node client: payload, RSSI, SNR all present.
- [ ] Send an ADVERT from the second radio (name + location). Listener
      parses: name, coords, node type - exactly what was sent.
- [ ] Send a GRP_TXT on a test channel the bench config knows.
      Node decrypts it, HMAC passes, text matches.
- [ ] Corrupt/garbled packet (second radio sends truncated bytes):
      node logs the failure honestly and keeps running - no crash, no
      made-up data (the -105.0 honesty rule).

## 4. cleanmodem TX re-proof (THE incident item)

- [ ] Node sends a short TEST packet (bench cadence, #scope channel,
      tiny payload). PROOF = the second radio hears it on the air -
      the scope-app in radio mode (desk radio over BLE) decodes it and
      shows the payload. NOT "TX_DONE" alone - TX_DONE is necessary,
      never sufficient.
- [ ] Byte-level: what cleanmoden was asked to send (captured at the
      sender boundary) matches what the app decoded - same body bytes.
- [ ] Three sends in a row, all heard. Then one send per minute for
      ten minutes, all heard - TX works repeatedly, not once.
- [ ] LBT check: hold the second radio's PTT / transmit continuously
      from the app while the node tries to send. cleanmodem backs off
      (its own LBT + CAD gate), logs the attempts, and NO packet is
      lost silently - the node logs what happened to each attempt.
- [ ] Politeness: two back-to-back node sends are spaced by
      cleanmodem's gap - visible in the airtime log.

## 5. Full-header listener checks (the reason this project exists)

SOFTWARE HALF DONE 2026-09-20 (last_scope_frame + test); the on-air
items below remain.

- [ ] GRP_DATA header preservation: second radio sends a GRP_DATA
      packet with a known payload. Node receives it with payload bytes
      INTACT and the full header (type, route code, transport codes,
      path, src hash, RSSI/SNR). Byte-for-byte payload comparison
      against what was sent. This is the check openhop repeater
      failed.
- [ ] Every parsed field logged for one test burst and eyeballed by
      Brett against the sender's app (name/route shown in MeshCore
      app matches what the node logged).
- [ ] Two identical packets sent back-to-back: BOTH arrive at the
      radio; the dedupe layer (section 6) is what merges them - the
      listener itself never drops data before dedupe.

## 6. Dedupe behavior

SOFTWARE HALF DONE 2026-09-20 (scripted set exact in tests); the
on-air script below remains.

- [ ] Same packet heard twice (second radio sends it, or another node
      re-broadcasts): dedupe window catches the copy - health counts
      ONCE. Log shows the duplicate was suppressed AND stored (the
      router's "duplicates are recorded, never vanished" rule).
- [ ] Different packets are never falsely merged: two distinct
      payloads in a burst both counted.
- [ ] After the dedupe window expires, a genuine re-observation is
      counted again (honest).
- [ ] Scripted sequence test: send a known scripted set of packets
      (N nodes, M duplicates, K distinct). The node's health numbers
      (active nodes, RX/hour) match the script EXACTLY - no
      double-counting, no silent drops.

## 7. Feed delivery to the web app with TX OFF

- [ ] Bench cadence shortened for the bench (e.g. pulse every 60 s).
      Node builds pulse / background / layout per the brain's cadence.
- [ ] TX-off enforced: the built packets are handed to WebServe [C]
      and NOT transmitted. Second radio confirms silence on the air
      during this whole section.
- [ ] scope-app in DIRECT mode (new WebSocket client) connects to the
      node: every built packet arrives, decodes, renders - health card
      numbers, map, sections all appear with no radio involved.
- [ ] LAYOUT at start: app draws the map immediately from the served
      LAYOUT packet (no hour-long wait).
- [ ] Byte-for-byte honesty of the tap: the bytes the node WOULD have
      transmitted (logged at the sender boundary) == the bytes the app
      received over the WebSocket.
- [ ] Refresh loop over the wire: from the app (radio mode), send a
      refresh request via the desk radio - the node hears it on air,
      the brain answers, the answer arrives over the WebSocket and the
      app updates. Everything proven except the final radio hop.
- [ ] Budget limiter: scripted burst beyond the packet budget ->
      refused sends logged honestly, gap shown in the app feed (the
      "honest gap" rule).
- [ ] Quiet-log check: between packets the log prints NOTHING (the
      log-spam lesson). Quiet link = silent log.

## 8. Stability soak

- [ ] 60-minute unattended soak: listener + feed building + app fed.
      No memory creep visible in logs, no connection drops, zero
      unhandled task deaths (the loud task-death watcher stays silent
      or explains everything).
- [ ] Restart cleanmodem mid-soak: node reconnects with backoff and
      resumes without human help.
- [ ] Restart the node mid-soak: clean start, feed cadence re-arms,
      app reconnects automatically.

## 9. Exit criteria (Gate 0 -> Gate 1)

- [ ] Every box above checked, dated, with evidence lines in
      BENCH-REPORT.md.
- [ ] Brett has seen the TX proof himself (second radio hearing the
      node) and the app proof himself (direct-mode map + health card).
- [ ] Zero open failures. Any "will fix later" item = Gate 1 does not
      start until it is fixed and re-run.

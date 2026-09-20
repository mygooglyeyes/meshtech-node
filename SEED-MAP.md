# meshtech-node - SEED MAP (drafted 2026-09-20; SEED EXECUTED
# 2026-09-20 - every verdict below re-confirmed at seed time per the
# map's own rule: the map records intent, the import records fact.
# Build status summary at the bottom; test totals: 124 pass + 6 skip.)

Where every module comes from, and what happens to it.
Sources: `C:\projects\lora-bot\.freebuff` (the bot stack, proven on
this box) and `C:\projects\meshtech-scope` (the scope brain, 100/100
tests). Verdicts: AS-IS (copy unchanged), ADAPT (copy + targeted
changes, listed), NEW (written fresh).

---

## Radio layer - from the bot stack

| Module | Verdict | Notes |
|---|---|---|
| `cleanmodem/` (whole package: `hal.py`, `sx126x.py`, `frames.py`, `server.py`, `client.py`, `config.py`, `PROTOCOL.md`) | **AS-IS - COPIED 2026-09-20** into `meshtech-node/cleanmodem/` | The proven direct-radio layer: clean-room SX126x driver, native SPI (spidev + gpiod - matches the modem), LBT/CAD/politeness gate, watchdog, two-role TCP server. Verified live on this box before the incident. No changes until the bench says otherwise. |

## Listener pipeline - from the bot stack

| Module | Verdict | Notes |
|---|---|---|
| `core/mcp.py` | **ADAPT - DELIVERED 2026-09-20 as `src/meshtech_node/packets.py`** | The packet core adapted as pure functions (frame split with FULL header, channel key derivation + group decode, flood dedupe 45s/120s, advert parse); the bot-coupled `Mcp` class orchestration was NOT copied - Adapter A (`rawsource.py`) replaces it. FINDING at seed time: mcp.py's route-name dict had flood/transport-flood SWAPPED - the reference (`openhop_core` constants.py:16-19) says 0=transport-flood, 1=flood; the node uses the reference and documents the correction. Golden crypto vectors generated FROM the reference CryptoUtils live in tests/test_packets.py. The ENCRYPT mirror for TX (Adapter B) is still to write. |
| `core/router.py` | **ADAPT** | normalize -> dedupe -> persist -> publish pipeline with the stored-duplicates rule. ADAPT = remove the bot's message-routing guards (channel allowlist, sender-name handling); keep dedupe + persistence + publish. |
| `core/store.py` | **ADAPT** | Node/message persistence, per-node traffic stats, airtime estimation. ADAPT = drop chat-message tables; keep node table + packet log + stats. |
| `core/meshhealth.py` | **AS-IS** (likely) | Flood scoring / offender ranking - pure computation over packet stats. No bot coupling expected; confirm at seed time. |
| `core/noisefloor.py` | **AS-IS** (likely) | Noise-floor tracking incl. modem mode (reads the chip via cleanmodem). Same check. |
| `core/airtime.py` | **AS-IS** | The Semtech airtime formula - pure function. (The scope brain already carries its own copy of this; consolidate to one.) |
| `core/capture.py` | **ADAPT** (optional) | Raw packet capture (decoded + raw-hex layers). Valuable for bench debugging (section 5 of the checklist); ADAPT = point it at the node's event stream instead of the meshcore library's. |
| `core/persist.py`, `core/selfupdate.py`, `core/updatecheck.py`, `core/version.py`, `core/modules.py`, `core/format.py`, `core/feed.py` | **SKIP** | Bot-dashboard/config-editor machinery the node doesn't need (the node's own shell is simpler). `feed.py`'s FeedHub pattern is REUSED conceptually by WebServe [C] (NEW) but not copied - the node's protocol is scope-packet-shaped, not chat-event-shaped. |

## Scope brain - from meshtech-scope

| Module | Verdict | Notes |
|---|---|---|
| `meshtech_scope/core/service.py` | **ADAPT - DONE 2026-09-20** | The orchestrator. Connection points swapped: the client is INJECTED (none wired = `_UnwiredClient` - TX off, loud warning; the Gate 1 listen-only default is the constructor's default state) and the non-demo ingest source is INJECTED (`external_source`, Adapter A). Multi-host coexistence, election, refresh-answering, budget-first rules all untouched. |
| `meshtech_scope/core/{feedbuilder,observations,grid,peers,budget,codec}.py` + `airtime.py`, `config.py` | **AS-IS - COPIED 2026-09-20** | The brain's substance: feed packets, rolling store, 3x3 grid, peer/owner tables, duty limiter, wire codec (the v0.2.5-corrected one). The copied codec/config/airtime suite passes unchanged in the node. |
| `meshtech_scope/core/client.py` | **AMENDED 2026-09-20: seeded AS-IS** (was SKIP) | Seed-time finding: its TX frame-building and RX decode tests ARE Adapter B's wire proofs (test_send_channel_frame_* etc. run green in the node). It stays as the wire reference; Adapter B swaps its TRANSPORT, not its logic. |
| `meshtech_scope/core/packetsource.py` | **PARTIAL - DONE 2026-09-20** | `DemoSource` + all row helpers seeded; `RepeaterApiSource` (lines 407+, HTTP/token/auth) NOT seeded - the node is the listener, it does not poll. The plugin-life tests that need it are marked skip with named reasons in the node's suite. |

## Written new (the glue - the only genuinely new code)

| Module | Notes |
|---|---|
| `node/rawsource.py` - **Adapter A: BUILT 2026-09-20** | Real `Observation` objects out (a dict contract was tried and cut - `_ingest_advert_rows` reads attributes; caught at seed time). Scope packets additionally decoded with the 0x5300-0x53FF guard and offered to the RX shim (`on_scope`); `last_scope_frame` keeps the FULL header of the last scope packet (the bench proof). `ReplayTransport` runs the whole pipeline with no radio. |
| `node/rxshim.py` - **BUILT 2026-09-20** (split out of Adapter A) | `wire_scope_rx(source, service)`: heard scope packets -> `service.on_packet(obj, "unknown")` - the same RX entry the plugin client used. HEADLINE TEST GREEN: encrypted REFRESH_REQ heard on air -> decrypt -> shim -> brain -> answer burst (SECT_SUM+ROUTE) on a FakeRadio; flood-repeat answered by dedupe. The path that died under openhop, proven in-process. |
| `node/radiosender.py` - **Adapter B: BUILT 2026-09-20** | FeedBuilder plaintext -> firmware-compatible GRP_DATA frame (header/flood + ch_hash + MAC(2) + AES-ECB; scheme verified as the firmware group scheme, NOT the reference's python-side sha256 variant - see module docstring) -> cleanmodem `CMD_TX_REQUEST` (raw radio bytes). TX-off guard + v0.2.2 framing checks + MTU cap, all refusals loud. Own-TX loopback marked in the SHARED FloodDedupe so cleanmodem's RX echo is suppressed, not counted. WIRE PROOF: frames parse with the reference Packet.read_from and decrypt to the exact plaintext (test_radiosender.py). |
| `node/webserve.py` - **[C] FeedTap + WebServe: BUILT 2026-09-20** | Small HTTP + WebSocket server: every built packet served to the web app (works TX-off and TX-on). Protocol spec: WEBSERVE-PROTOCOL.md in this folder. The brain's `_send_burst` taps EVERY built packet (before any TX attempt, budget-drops included) tagged `would_tx`/`tx_ok`/`in_reply_to`; wire refreshes dispatch through the brain's REAL dedupe + rate limiter via a req-id stack (answers auto-tagged - no bypass); ring buffer (200) + resume; token auth (constant-time + throttle) for non-loopback binds; serves the app dist same-origin (CSP-safe) with no-store headers (gray-fog lesson). LIVE-PROVED end-to-end on the bench node (below). |
| `node/node.py` + `node/__main__.py` - **shell: BUILT 2026-09-20** | Service shell: one process, all tasks supervised, graceful stop, loud bench banner (`TX DISABLED - listen-only`), fail-closed non-loopback bind without a token. `--bench-no-radio` mode: sender reports link-ready, feed builds, TX impossible. Run: `python -m meshtech_node --config config.bench.json --bench-no-radio` (see config.bench.json: 10-min layout, 60 pkt/h, 5 refreshes/h - the approved schedule). |
| scope-app direct mode - **BUILT 2026-09-20** | Web-side: `src/lib/directclient.ts` (WebSocket transport, shared decoder, resume-on-reconnect, node-restart reset via `ScopeState.resetAll`) + App.ts source switch (radio \| direct, one at a time, every packet line tagged `[radio]`/`[direct]`). Refresh buttons work in both modes. LIVE-PROVED in the preview: map drew instantly at connect, refresh answered over the wire. |

## Coupling rules (why the verdicts hold)

1. **Nothing from openhop_repeater/openhop_core is copied except
   reference reading** - openhop_core stays read-only; the retransmit
   question is moot (no repeating per Brett).
2. **The scope brain keeps its own tests green**: adapters must satisfy
   the same interfaces the plugin's client/source satisfied - the
   100/100 suite runs against the node adapters at bench time.
3. **One airtime module**, not two (consolidate bot + scope copies at
   seed time).
4. Every AS-IS/likely-AS-IS verdict gets RE-CONFIRMED at seed time by
   actually importing it - the map records intent, the import records
   fact.

## Build status (2026-09-20, after the WebServe + direct mode increment)

BUILT: cleanmodem copied; packet core (packets.py) + Adapter A
(rawsource.py) + RX shim (rxshim.py) + Adapter B (radiosender.py);
WebServe [C] (webserve.py) + service shell (node.py) + scope-app
direct mode; the full scope brain seeded behind injected client/
source. Node suite: 139 tests pass + 6 skip (plugin-life API-source
tests, named reasons); app codec/trailfx suites still green.
BENCH NODE RUNNING (2026-09-20): pid on 127.0.0.1:8710,
`--bench-no-radio`, serving the app at / and the feed at /feed.
LIVE-PROVED: map draws instantly at app connect (served LAYOUT);
pulse/sect_sum cadence taps through; wire refresh -> tagged answer
burst (ack + 10 packets); every packet line tagged [direct]; TX
refusals loud (`TX DISABLED - listen-only`) - nothing on the air.
NOT BUILT: nothing in software. MODEMLINK BUILT 2026-09-20
(modemlink.py): cleanmodem's proven ModemClient bridged into the
source as an async-iterator transport - RX pushes (rssi, snr, sig,
data) become RxPackets; the RX callback is a coroutine (cleanmodem's
_pump awaits it); failed links raise LOUDLY and tear down (v0.0.160
lesson); empty/garbled packets never kill the link; real-mode boot
verified on Windows (link retries honestly where no modem server
exists). The node's real mode runs with cleanmodem ON THE BOX
(SPI/serial - PiMesh 1W v2, no ch341); the node reaches it over
loopback TCP. 143 pass + 6 skip.
ENVIRONMENT: node venv = Python 3.12 + pycryptodome + pynacl +
meshcore 2.3.11 + aiohttp + pytest-asyncio + pytest; reference
library used read-only from openhop_core/src via conftest.py path
injection. NOTE: the package is not pip-installed in the venv - run
with PYTHONPATH=src or install it when deploying to hilltop.

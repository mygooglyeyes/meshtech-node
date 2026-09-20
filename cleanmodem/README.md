# cleanmodem - standalone LoRa modem process

A clean-room SX1262 modem: one process owns the radio over SPI and
serves two kinds of TCP clients on one port -

- **observer** - receives a copy of every packet the radio hears
  (the visualization/packet-log feed),
- **controller** - the bot's exclusive TX path: sends packets to the
  air and receives the same RX feed.

Written from the SX126x datasheet's command set plus the project's own
requirements checklist; no code from any other modem project.

## Architecture (one process, three layers)

1. **Radio HAL + SX126x driver** (`hal.py`, `sx126x.py`) - ALL SPI and
   GPIO live on a dedicated thread that exclusively owns the radio.
   asyncio talks to it through queues, so a slow SPI transaction never
   stalls a TCP client. DIO1 is a real edge event (not polling), and a
   radio watchdog re-inits the chip after repeated failures.
2. **Wire protocol** (`frames.py`) - `SYNC | CMD | LEN | PAYLOAD | CRC16`
   frames (see PROTOCOL.md). Table-driven CRC-16/CCITT-FALSE; the parser
   never raises on hostile input.
3. **Server** (`server.py`) - role-by-token auth (constant-time compare,
   per-connection brute-force throttling), one RX frame built once and
   fanned out to every client without per-client draining, TX serialized
   through the radio's own LBT + CAD pre-check + politeness gap.

## Running

    python -m cleanmodem --config modem.conf

Config keys, pin presets and per-pin overrides are documented in
`deploy/cleanmodem.conf.example`. Radio defaults match the live mesh
(910.525 MHz, SF7, 62.5 kHz, 20 dBm, sync word 0x12, preamble 32).

## Security model

- Passwords live in mode-600 token files (first line = password), never
  in the config; a loose file is refused (fail closed).
- No token file = that role serves nothing. Loopback bind by default.
- Unknown commands, oversized frames, and unauthenticated chatter are
  answered once and rate-limited, never allowed to wedge the server.
- Log lines sanitize peer-controlled text; payload hex stays at DEBUG.

## Status / verification

- Wire constants (frame layouts, status/config structs) were verified
  against the reference modem implementation the clients expect before
  shipping.
- Signal-metric conversions follow the SX126x datasheet (RSSI = raw/-2,
  SNR = signed raw/4) and match the values the mesh already logs.
- Test suite: frames, pin config, driver register sequences (fake SPI),
  server behavior (fake radio), the client pump, and a security suite
  (parser fuzzing, auth matrix, DoS caps, log-injection). Bench
  verification (RX parity + latency + soak) is the on-hardware gate
  before this replaces the current modem on the box.

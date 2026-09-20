# meshtech-node - WEBSERVE PROTOCOL SPEC (drafted 2026-09-20, for Brett's review)

The wire contract between meshtech-node's WebServe [C] and the
scope-app's DIRECT mode. Design principle: the WebSocket carries the
SAME wire bytes the BLE path extracts today, so the app's decoder is
shared and unmodified - direct mode is a new TRANSPORT, not a new
decoder.

---

## Transport

- One endpoint: `ws://127.0.0.1:8710/feed` (port configurable; loopback
  bind by default - same posture as cleanmodem).
- Auth: no token needed on loopback. A non-loopback bind REQUIRES a
  token file (mode-600, first line = password, constant-time compare,
  brute-force throttle - cleanmodem's proven rules copied). Non-loopback
  bind without a token file = the server refuses to start (fail closed).
- JSON text frames only in v1. Protocol version carried in `hello`;
  a client that sees an unknown `proto` shows an honest error and stops.

## Message shapes

Server -> client:

    hello    {type:"hello", proto:1, node:"meshtech-node",
              tx_enabled:false, last_seq:1234,
              feed:{channel:"#scope", origin:"a1b2",
                    pulse_s:300, layout_s:3600},
              now:<unix seconds>}

    packet   {type:"packet", seq:1235, ts_ms:1700000000000,
              kind:"pulse"|"sect_sum"|"layout"|"intro"|"answer",
              wire:"01531a015317...", would_tx:false,
              snr:null, in_reply_to:null}

      - `wire` = the scope wire packet (envelope + body), plaintext -
        byte-for-byte what the app ends up holding after extracting it
        from a BLE 0x1b frame and decrypting on the radio path. The
        app's existing decode consumes it directly.
      - `would_tx` = what the sender WOULD have done (false all through
        Gate 1 listen-only). Honest by construction.
      - `snr` = null in direct mode (no radio hop); the app's UI must
        show "direct" rather than a fake number (the -105.0 rule).
      - `in_reply_to` = req_id when this packet answers a client's
        `refresh`.

    state    {type:"state", ts_ms, listener:{pkts_last_hour,
              nodes_active, last_heard_s},
              feed:{last_pulse_ts, next_pulse_ts,
                    budget_used_h, budget_cap_h},
              uptime_s}
      - Listener-side truth the BLE path can never see. Optional for
        the app in v1; the health card still renders from packets.

    pong     {type:"pong", now}

Client -> server:

    ping     {type:"ping"}                      -> pong
    resume   {type:"resume", after_seq:1200}    -> replay from ring
                 buffer (bounded, last 200 packets), then live
    refresh  {type:"refresh", req_id:"r7",
              kind:"layout"|"section", section?:0-8}
                 - the brain answers exactly as if a REFRESH_REQ arrived
                   on air (same rate limiter, same dedupe - no bypass);
                   answers come back as `packet` with `in_reply_to`.
                 - gives the app a no-radio refresh path at the bench;
                   the over-the-air refresh path keeps working too.

## Backpressure and drops

- The server never queues unboundedly: a slow client is disconnected
  (cleanmodem's slow-client guard). The app reconnects and resumes via
  the ring buffer. Nothing is silently swallowed - drops are visible as
  a disconnect + resume in the app's event log.

## Reconnect behavior

- App side: reconnect with backoff 1s -> 2s -> 5s -> 10s -> 30s cap,
  jittered (the answerbot's proven pattern). Status shown honestly:
  "direct: reconnecting (12s)".
- On connect: server sends `hello`; client compares `last_seq`:
  - if the client has seen seq <= hello.last_seq, send `resume
    after_seq:<client's last seq>` and replay the gap;
  - if the server's seq REGRESSED (node restarted, fresh buffer), the
    client discards its stale view and requests a fresh `refresh
    kind:"layout"` - the map redraws immediately (the Gate 1 bonus).
- Heartbeat: client pings every 10 s; server closes a silent connection
  after 30 s. Both sides log a disconnect reason once - no spam.

## Mode switching in the app

- The app has exactly ONE active source at a time: `radio` (BLE) or
  `direct` (WS). Explicit toggle in the app header - no silent
  auto-switching (honest display beats cleverness).
- The status line always shows: active source, last packet age,
  reconnect state when relevant.
- Every packet in the event log is tagged with its source
  ([radio]/[direct]) - this is what makes the Gate 2 cross-check
  (direct-served vs radio-heard) a two-second eyeball job.
- A later "dual" mode (both sources live, duplicates suppressed by seq)
  is deliberately OUT of v1; one honest source at a time first.

## What v1 deliberately does NOT do

- No node configuration over the WebSocket (config is files on the box).
- No raw listener firehose (a read-only HTTP dump of recent heard
  packets can be added for bench debugging if needed - deferred).
- No retransmit anything (there is no repeating, per Brett's call).

## Security summary

- Loopback by default; token-gated otherwise; fail closed on missing
  token for a wide bind; no secrets or key material ever served in the
  protocol; log lines sanitize peer-controlled text.

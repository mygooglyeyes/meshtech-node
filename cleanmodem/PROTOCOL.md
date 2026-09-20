# cleanmodem wire protocol

All integers little-endian. One framing for both directions:

    SYNC (0xAA) | CMD (1B) | LEN (2B LE) | PAYLOAD (0..255 B) | CRC-16 (2B LE)

- LEN covers PAYLOAD only. Radio payloads cap at 255 bytes (the LoRa MTU).
- CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflect, no xor-out)
  over CMD + LEN + PAYLOAD (SYNC excluded). Check vector:
  crc16("123456789") = 0x29B1.
- A CRC mismatch is answered with CMD_ERROR/ERR_CRC_MISMATCH and the
  receiver resynchronizes at the next SYNC byte.

## Authentication

- **Controller (bot):** the first bytes on the socket are the raw
  token; the modem answers a single byte `0x01` (ok) or `0x00`
  (refused). Constant-time compare, throttled retries.
- **Observer (repeater feed):** full frames, starting with CMD_AUTH
  (payload = token). Answer: CMD_AUTH_OK or CMD_ERROR/ERR_UNAUTHORIZED.

Roles gate commands: observers get RX + status queries only; TX and
config commands are controller-only (answered ERR_UNAUTHORIZED).

## Host -> modem

| CMD | Name | Payload |
|-----|------|---------|
| 0x01 | TX_REQUEST | raw radio bytes (<= 255 B) |
| 0x10 | SET_CONFIG | RADIO_CONFIG (14 B, below) |
| 0x11 | GET_CONFIG | empty |
| 0x20 | STATUS_REQ | empty |
| 0x22 | NOISE_REQ | empty |
| 0x30 | CAD_REQUEST | empty |
| 0x31 | RX_START | empty (arm RX; observers auto-arm at auth) |
| 0x34 | SET_CAD_PARAMS | peak u8, min u8 |
| 0x50 | AUTH | token bytes (observer path) |
| 0x70 | GET_VERSION | empty |
| 0xFF | PING | empty |

## Modem -> host

| CMD | Name | Payload |
|-----|------|---------|
| 0x02 | TX_DONE | airtime in microseconds (u32 LE) |
| 0x03 | TX_FAIL | reason byte |
| 0x04 | RX_PACKET | RSSI i16 \| SNR*10 i16 \| signal-RSSI i16 \| raw bytes |
| 0x12 | CONFIG_RESP | RADIO_CONFIG |
| 0x21 | STATUS_RESP | STATUS (24 B, below) |
| 0x23 | NOISE_RESP | noise*10, i16 LE |
| 0x32 | CAD_RESP | 1 byte: 1 = clear, 0 = busy |
| 0x33 | RX_STARTED | empty |
| 0x35 | CAD_PARAMS_RESP | peak u8, min u8 |
| 0x51 | AUTH_OK | empty |
| 0x71 | VERSION_RESP | major u8, minor u8 |
| 0xFE | CMD_ERROR | error code byte (below) |
| 0xFF | PONG | empty |

## Structs

- **RADIO_CONFIG (14 B):** freq_hz u32 \| bandwidth_hz u32 \| sf u8 \|
  cr u8 \| power_dbm i8 \| syncword u16 \| preamble u8
- **STATUS (24 B):** uptime u32 \| rx_count u32 \| tx_count u32 \|
  crc_errors u32 \| last_rssi i16 \| snr*10 i16 \| noise*10 i16 \|
  temp_c i8 \| radio_state u8

## Error codes (CMD_ERROR payload byte)

0x01 CRC mismatch, 0x02 invalid command, 0x03 radio busy,
0x04 TX timeout, 0x05 payload too big, 0x06 invalid config,
0x07 CAD failed, 0x08 radio init, 0x09 unauthorized.

## TX behavior (controller)

The modem serializes TX: one in-flight TX_REQUEST at a time, CAD
listen-before-talk before each send (configurable peak/min), a
politeness gap between the modem's own packets, and the just-sent frame
looped back as RX to all clients (so the observer's packet log shows
the bot's own traffic).

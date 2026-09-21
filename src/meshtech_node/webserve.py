"""WebServe [C] - the FeedTap + WebSocket server.

Every packet the FeedBuilder produces is served to the scope-app's
DIRECT mode over WebSocket - the SAME wire bytes the BLE path yields,
so the app's decoder is shared and unmodified (WEBSERVE-PROTOCOL.md).
Works TX-off (Gate 1 listen-only: the only delivery path) and TX-on
(second path for the Gate 2 cross-check).

Security (cleanmodem's posture): loopback bind by default; a non-
loopback bind REQUIRES a token file (mode-600, first line = password,
constant-time compare, brute-force throttle) or the server refuses to
start (fail closed).

Messages (protocol v1): hello / packet / state / pong down;
ping / resume / refresh up. The ring buffer (last 200 packets) powers
resume; a regressed server seq tells the client its view is stale.

Refresh over the wire: a client `refresh` becomes a real
codec.RefreshReq handed to service.on_packet through the SAME dedupe
and rate limiter an on-air request takes - no bypass. Answers come
back as packets tagged with in_reply_to (req_id).
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac as hmac_mod
import itertools
import json
import logging
import os
import secrets
import struct
import time
from pathlib import Path
from typing import Callable, List, Optional, Set

from aiohttp import WSMsgType, web

from . import codec

log = logging.getLogger("meshtech-node.webserve")

PROTO_VERSION = 1
RING_CAPACITY = 200
PING_INTERVAL_S = 10.0
SILENCE_TIMEOUT_S = 30.0
TX_TIMEOUT_S = 5.0

KIND_BY_TYPE = {
    codec.TYPE_PULSE: "pulse",
    codec.TYPE_SECT_SUM: "sect_sum",
    codec.TYPE_ROUTE: "route",
    codec.TYPE_INTRO: "intro",
    codec.TYPE_LAYOUT: "layout",
    codec.TYPE_SNAP: "snap",
    codec.TYPE_REFRESH_REQ: "refresh",
}


def read_token(path: str) -> Optional[str]:
    """First line of a mode-600 token file, or None (and it says so)."""
    try:
        p = Path(path)
        if os.name == "posix":
            mode = p.stat().st_mode & 0o777
            if mode & 0o177:
                log.error("token file %s is group/other-accessible "
                          "(mode %o) - refusing", path, mode)
                return None
        return p.read_text(encoding="utf-8").splitlines()[0].strip()
    except Exception as exc:
        log.warning("token file %s unreadable: %s", path, exc)
        return None


class _Auth:
    """Token gate for non-loopback binds (constant-time + throttle)."""

    def __init__(self, token: str):
        self.token = token.encode("utf-8")
        self.fails: dict = {}

    def check(self, candidate: str, peer: str) -> bool:
        now = time.time()
        fails = [t for t in self.fails.get(peer, []) if now - t < 60.0]
        self.fails[peer] = fails
        if len(fails) >= 5:
            return False
        ok = hmac_mod.compare_digest(self.token,
                                     candidate.encode("utf-8"))
        if not ok:
            self.fails.setdefault(peer, []).append(now)
        return ok


class _GlobalRefreshBudget:
    """S2 (2026-09-20 review, Brett's design): wire refreshes draw from
    a GLOBAL airtime budget, not a per-client one - a browser can mint
    a new request id per click, so per-client maps saw a fresh client
    each time and never throttled. Whole-map refreshes are the big
    spender (a LAYOUT burst): only 2 allowed per 30 minutes across ALL
    connections. Per-section refreshes are cheap and stay per-
    connection (rate limiter via sender_prefix) so casual browsing
    doesn't burn the global budget. On-air requests never touch this:
    the brain's own limiter gates them as before."""

    def __init__(self, max_per_window: int = 2, window_s: float = 1800.0):
        self.max = max_per_window
        self.window = window_s
        self._hits: List[float] = []

    def allow(self, *, now: Optional[float] = None) -> bool:
        now = time.time() if now is None else now
        self._hits = [t for t in self._hits if now - t < self.window]
        if len(self._hits) >= self.max:
            return False
        self._hits.append(now)
        return True

    def retry_after_s(self, *, now: Optional[float] = None) -> int:
        now = time.time() if now is None else now
        self._hits = [t for t in self._hits if now - t < self.window]
        if len(self._hits) < self.max or not self._hits:
            return 0
        oldest = min(self._hits)
        return max(1, int(self.window - (now - oldest)))


class WebServe:
    def __init__(self, host: str, port: int, *,
                 token: Optional[str] = None,
                 on_refresh: Optional[Callable[[object, str], None]] = None,
                 on_client_connected: Optional[Callable[[str], None]] = None,
                 feed_info: Optional[dict] = None,
                 state_provider: Optional[Callable[[], dict]] = None):
        self.host = host
        self.port = port
        self.auth = _Auth(token) if token else None
        self.on_refresh = on_refresh
        # Fired (awaited) right after hello+state reach a NEW client:
        # the brain answers with a fresh PULSE so the app's Feed-health
        # card fills immediately instead of waiting up to one full
        # pulse cadence (Brett, 2026-09-21: "not waiting 5 minutes").
        self.on_client_connected = on_client_connected
        self.feed_info = feed_info or {}
        self.map_budget = _GlobalRefreshBudget()
        # state_provider = the brain's honest state snapshot (listener
        # + feed truth the BLE path can never see); None -> nulls.
        self.state_provider = state_provider
        self.clients: Set = set()
        self.seq = 0
        self.ring: List[dict] = []          # last RING_CAPACITY packets
        # req-id stack: while a client refresh is being dispatched, its
        # req_id is on top; the shell's FeedTap reads current_req_id to
        # tag the answer burst's packets with in_reply_to.
        self._req_stack: List[str] = []
        self._conn_counter = itertools.count(1)
        self.started_at = time.time()
        self.app = web.Application()
        self.app.router.add_get("/feed", self._ws_handler)
        self._runner: Optional[web.AppRunner] = None

    @property
    def current_req_id(self) -> Optional[str]:
        return self._req_stack[-1] if self._req_stack else None

    # ------------------------------------------------------------- tap ----
    def on_built_packet(self, data_type: int, payload: bytes, *,
                        would_tx: bool, tx_ok: bool = False,
                        in_reply_to: Optional[str] = None) -> dict:
        """The FeedTap: the brain calls this for EVERY packet it builds
        (via the service adapter in the shell). Returns the message
        dict (also enqueued)."""
        self.seq += 1
        msg = {
            "type": "packet",
            "seq": self.seq,
            "ts_ms": int(time.time() * 1000),
            "kind": KIND_BY_TYPE.get(data_type, f"0x{data_type:04x}"),
            "wire": payload.hex(),
            "would_tx": would_tx,
            "tx_ok": tx_ok,
            "snr": None,                    # direct: no radio hop - honest
            "in_reply_to": in_reply_to,
        }
        self.ring.append(msg)
        if len(self.ring) > RING_CAPACITY:
            self.ring.pop(0)
        for ws in list(self.clients):
            asyncio.ensure_future(self._send(ws, msg))
        return msg

    async def _send(self, ws, msg: dict) -> None:
        try:
            await ws.send_str(json.dumps(msg, separators=(",", ":")))
        except Exception:
            self.clients.discard(ws)

    def on_heard_packet(self, data_type: int, plaintext: bytes, *,
                        snr: Optional[float] = None) -> dict:
        """COMPANION MODE tap: a #scope packet the companion HEARD
        (decrypted by the listener from the radio's observer feed).
        Same message shape as on_built_packet; would_tx/tx_ok are False
        (we did not send it) and snr is the REAL radio hop when the
        modem reported one - None stays None."""
        self.seq += 1
        msg = {
            "type": "packet",
            "seq": self.seq,
            "ts_ms": int(time.time() * 1000),
            "kind": KIND_BY_TYPE.get(data_type, f"0x{data_type:04x}"),
            "wire": plaintext.hex(),
            "would_tx": False,
            "tx_ok": False,
            "snr": snr,
            "in_reply_to": None,
        }
        self.ring.append(msg)
        if len(self.ring) > RING_CAPACITY:
            self.ring.pop(0)
        for ws in list(self.clients):
            asyncio.ensure_future(self._send(ws, msg))
        return msg

    # ----------------------------------------------------------- state ----
    def _state_msg(self) -> dict:
        snap = self.state_provider() if self.state_provider else {}
        return {
            "type": "state",
            "ts_ms": int(time.time() * 1000),
            "listener": snap.get("listener", {"pkts_last_hour": None,
                                              "nodes_active": None,
                                              "last_heard_s": None}),
            "feed": snap.get("feed", {"last_pulse_ts": None,
                                      "next_pulse_ts": None,
                                      "budget_used_h": None,
                                      "budget_cap_h": None}),
            "uptime_s": int(time.time() - self.started_at),
        }

    # ---------------------------------------------------------- static ----
    def add_static(self, dist_dir: str) -> None:
        """Serve the scope-app's built dist/ from this origin.

        The app's CSP is connect-src 'self' - serving the app HERE puts
        the WebSocket on the same origin, no CSP change. Caching is
        disabled everywhere (the 2026-09-18 gray-fog lesson: Chrome
        heuristically cached stale bytes after a rebuild) via a small
        response middleware - aiohttp's add_static takes no headers arg."""
        dist = Path(dist_dir).resolve()

        @web.middleware
        async def no_store(request, handler):
            resp = await handler(request)
            resp.headers.setdefault("Cache-Control", "no-store")
            return resp

        async def index(_request):
            return web.FileResponse(dist / "index.html")

        self.app.middlewares.append(no_store)
        self.app.router.add_get("/", index)
        self.app.router.add_static("/", dist, show_index=False)

    # -------------------------------------------------------------- ws ----
    async def _ws_handler(self, request) -> web.Response:
        peer = request.remote or "?"
        if self.auth is not None:
            # Token via WS subprotocol ("bearer.<token>"): browsers
            # cannot set custom headers, but they DO control the
            # subprotocol list. The node picks the bearer protocol when
            # it matches (constant-time); anything else is refused
            # before the socket is upgraded - fail closed. Non-browser
            # clients may use the X-Node-Token header as before.
            candidate = request.headers.get("X-Node-Token", "")
            if not candidate:
                for proto in request.headers.get("Sec-WebSocket-Protocol",
                                                 "").split(","):
                    proto = proto.strip()
                    if proto.startswith("bearer."):
                        candidate = proto[len("bearer."):]
                        break
            if not candidate or not self.auth.check(candidate, peer):
                log.info("WS auth refused for %s", peer)
                return web.Response(status=401, text="unauthorized")
            # Chrome closes the socket unless the server SELECTS one of
            # the client's offered subprotocols (RFC 6455). aiohttp only
            # echoes when the protocol is listed here. Browsers send
            # exactly one: bearer.<token>. Non-browsers send none.
            offered = [p.strip() for p in
                       request.headers.get("Sec-WebSocket-Protocol",
                                           "").split(",") if p.strip()]
            selected = [p for p in offered if p.startswith("bearer.")]
        else:
            selected = []
        ws = web.WebSocketResponse(heartbeat=SILENCE_TIMEOUT_S,
                                   max_msg_size=64 * 1024,
                                   protocols=selected)
        await ws.prepare(request)
        self.clients.add(ws)
        # S2: stable per-CONNECTION client identity for the brain's rate
        # limiter - the browser's per-click req_id minted a new identity
        # every refresh and sidestepped the cooldown/cap entirely.
        conn_id = f"ws#{next(self._conn_counter)}"
        log.info("WS client connected (%s as %s) - total %d", peer,
                 conn_id, len(self.clients))
        try:
            hello = {
                "type": "hello", "proto": PROTO_VERSION,
                "node": "meshtech-node",
                "tx_enabled": bool(self.feed_info.get("tx_enabled", False)),
                "last_seq": self.seq,
                "feed": self.feed_info.get("feed", {}),
                "now": time.time(),
            }
            await ws.send_str(json.dumps(hello, separators=(",", ":")))
            # bench helper: initial state immediately
            await ws.send_str(json.dumps(self._state_msg(),
                                         separators=(",", ":")))
            # A NEW client gets its PULSE now, not at the next cadence
            # tick: the brain builds one and the tap delivers it on
            # this very socket. Never let a brain failure kill the
            # connection (the pulse cadence will catch up anyway).
            if self.on_client_connected is not None:
                try:
                    await self.on_client_connected()
                except Exception:
                    log.exception("connect-pulse failed - client stays "
                                  "connected (cadence will catch up)")
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        obj = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    await self._client_msg(ws, obj, conn_id)
                elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
        finally:
            self.clients.discard(ws)
            log.info("WS client disconnected (%s) - total %d", peer,
                     len(self.clients))
        return web.Response(status=200)

    async def _client_msg(self, ws, obj: dict, conn_id: str = "ws") -> None:
        mtype = obj.get("type")
        if mtype == "ping":
            await self._send(ws, {"type": "pong",
                                  "now": time.time()})
        elif mtype == "resume":
            after = obj.get("after_seq", 0)
            if not isinstance(after, int):
                return
            for msg in self.ring:
                if msg["seq"] > after:
                    await self._send(ws, msg)
        elif mtype == "refresh":
            req_id = str(obj.get("req_id", ""))[:40]
            kind_s = obj.get("kind")
            is_map = False
            if kind_s == "layout":
                # legacy spelling (pre-v1.2 app) for the map button
                kind = codec.REFRESH_KIND_SECTION
                target = codec.REFRESH_WHOLE_AREA
                is_map = True          # whole-map = the big burst
            elif kind_s == "map":
                # v1.2 spelling: the map button says what it means
                kind = codec.REFRESH_KIND_SECTION
                target = codec.REFRESH_WHOLE_AREA
                is_map = True
            elif kind_s == "section":
                kind = codec.REFRESH_KIND_SECTION
                target = obj.get("section")
                # v1.2 wire: squares are 1..9 (1 = NW); 0 = whole-area.
                if not isinstance(target, int) or \
                        not 0 <= target <= 9:
                    return
                is_map = target == codec.REFRESH_WHOLE_AREA
            else:
                return
            # S2, closed 2026-09-20: ANY target-0 refresh means
            # whole-area on the wire, whatever kind string carried it
            # (the map button has always sent kind=section target=0) -
            # so the GLOBAL budget keys on target==0, not on is_map.
            # Real squares (1..9) never draw the global budget.
            # COMPANION MODE: refused BEFORE the budget - a listen-only
            # device has no host data to answer with; heard packets are
            # the map. The app gets the plain-words ack, never silence.
            if self.feed_info.get("companion_mode", False):
                log.info("refresh refused - companion mode (listen-only)")
                await self._send(ws, {"type": "ack",
                                      "req_id": req_id,
                                      "accepted": False,
                                      "reason": "listen_only"})
                return
            if target == codec.REFRESH_WHOLE_AREA and \
                    not self.map_budget.allow():
                wait = self.map_budget.retry_after_s()
                log.info("whole-map refresh refused - global budget "
                         "spent, %ds until the next slot", wait)
                await self._send(ws, {"type": "ack",
                                      "req_id": req_id,
                                      "accepted": False,
                                      "reason": "map_budget",
                                      "retry_after_s": wait})
                return
            nonce = secrets.randbits(16)
            req = codec.RefreshReq(seq=nonce, kind=kind, target=target,
                                   nonce=nonce,
                                   origin=int(obj.get("origin", 0)) & 0xFFFF)
            if self.on_refresh is not None:
                self._req_stack.append(req_id)
                try:
                    # conn_id (stable per connection) feeds the brain's
                    # per-client cooldown/cap - not the per-click req_id.
                    await self.on_refresh(req, conn_id)
                finally:
                    self._req_stack.pop()
                await self._send(ws, {"type": "ack",
                                      "req_id": req_id,
                                      "accepted": True})
        else:
            log.debug("WS client sent unknown type %r", mtype)


def make_app(webserve: WebServe) -> web.Application:
    return webserve.app

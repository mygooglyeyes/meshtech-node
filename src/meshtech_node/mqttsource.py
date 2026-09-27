"""MQTT collector - listen to the public observers, build coverage.

Lab plan 2026-09-26, Ch5. openhop keeps SENDING to the public brokers
(meshcore.ca, gomesh, waev, meshmapper); this node only COLLECTS: it
subscribes to the meshcore topic family and records which observer
heard which packet - the same packet hash reported by several
observers is the coverage map (repeater-planning gold).

Wire format (verified against openhop_repeater's PacketRecord +
mqtt_handler, 2026-09-26):

    meshcore/{REGION}/{OBSERVER_PUBKEY}/packets   (legacy: /packet)

    JSON payload, all fields strings:
    {origin, origin_id, timestamp (ISO), type: "PACKET", direction,
     time, date, len, packet_type, route, payload_len, raw,
     SNR, RSSI, score, duration, hash}

Other sub-topics (status, neighbors, raw) are observer metadata, not
coverage, and never enter the heard-by table. Mobile-client topics
(meshcore/client/...) are a different path and are skipped.

Honesty: a message without a hash identifies no packet and is dropped
(counted, never fudged); a missing RSSI/SNR stays NULL; a payload we
cannot parse is counted and skipped - the collector never dies on one
bad message.

paho-mqtt is an OPT-IN dependency (mqtt.enabled is false by default):
without it the collector logs one error and idles - the node runs fine
without it.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional, Tuple

log = logging.getLogger("meshtech-node.mqttsource")

# Sub-topics that carry packet observations (the plural is the MC2MQTT
# family; the singular is the legacy "mqtt" format - both are the same
# packet report).
PACKET_SUBTOPICS = ("packets", "packet")


def _num(value: object) -> Optional[float]:
    """PacketRecord ships every field as a STRING. Parse a number;
    anything unparseable is honestly None (never a default)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _ts(value: object) -> Optional[float]:
    """ISO timestamp -> epoch seconds (local-zone naive stamps included
    via fromisoformat), None when absent/unparseable."""
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


class MqttCollector:
    """One MQTT subscription -> heard-by rows in the NodeStore."""

    def __init__(self, cfg, store) -> None:
        self.cfg = cfg                  # config.MqttCfg
        self.store = store              # node_store.NodeStore
        self.stats = {"heard": 0, "no_hash": 0, "bad_payload": 0,
                      "skipped": 0, "region_dropped": 0}
        # Ch6: regions already warned about (one line per region, not
        # per message - the Go ingestor's throttled warn pattern).
        self._region_warned = set()
        self._client = None

    # ------------------------------------------------------- the seam --
    @staticmethod
    def parse_topic(topic: str) -> Optional[Tuple[str, str, str]]:
        """meshcore/{region}/{observer}/{subtopic} -> the triple.

        None for everything else: mobile-client topics
        (meshcore/client/...), the status bus (meshcore/status), the
        event bus, and any shape we do not recognise - those are not
        observer coverage and must never poison the table."""
        parts = str(topic or "").split("/")
        if len(parts) < 4 or parts[0] != "meshcore":
            return None
        region, observer_id, subtopic = parts[1], parts[2], parts[3]
        if region == "client" or not region or not observer_id:
            return None
        return region, observer_id, subtopic

    def handle_message(self, topic: str, payload: bytes) -> bool:
        """One MQTT message -> at most one heard-by row. True = recorded.

        Never raises: a malformed message is counted and dropped, the
        connection keeps collecting."""
        try:
            parsed = self.parse_topic(topic)
            if parsed is None:
                self.stats["skipped"] += 1
                return False
            region, observer_id, subtopic = parsed
            if subtopic not in PACKET_SUBTOPICS:
                self.stats["skipped"] += 1
                return False
            # CH6: the wide view is SUBSCRIBED whole (meshcore/#) but
            # recorded through the region filter - mqtt.regions empty
            # accepts every region; otherwise a region outside the list
            # is dropped (counted, warned once). Same contract as the
            # Go ingestor's IATA whitelist.
            if self.cfg.regions and region.upper() not in self.cfg.regions:
                self.stats["region_dropped"] += 1
                if region not in self._region_warned:
                    self._region_warned.add(region)
                    log.info("[region-filter] dropping region '%s' "
                             "(not in mqtt.regions) - further messages "
                             "from it are counted, not listed",
                             region)
                return False
            msg = json.loads(bytes(payload).decode("utf-8"))
            if not isinstance(msg, dict):
                self.stats["bad_payload"] += 1
                return False
            packet_hash = str(msg.get("hash") or "").strip()
            if not packet_hash:
                # No hash, no packet identity - a coverage row for it
                # would be a fabrication. Counted, never invented.
                self.stats["no_hash"] += 1
                return False
            ts = _ts(msg.get("timestamp")) or time.time()
            self.store.upsert_heard_by(
                packet_hash, observer_id,
                region=region,
                rssi=_num(msg.get("RSSI")),
                snr=_num(msg.get("SNR")),
                ts=ts)
            self.stats["heard"] += 1
            return True
        except Exception:                # noqa: BLE001 - one bad message never kills the collector
            log.exception("MQTT message dropped (unparseable) on %s",
                          str(topic)[:80])
            self.stats["bad_payload"] += 1
            return False

    # ------------------------------------------------------- lifecycle --
    async def run(self, stop: asyncio.Event) -> None:
        """Connect and collect until `stop`. paho's network thread does
        the I/O; this task owns the lifecycle only."""
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            log.error("mqtt.enabled is set but paho-mqtt is NOT "
                      "installed - MQTT collection stays OFF (install "
                      "paho-mqtt or set mqtt.enabled false). The gap "
                      "is honest.")
            await stop.wait()
            return
        # paho 2.x renamed the callback API; VERSION1 keeps the classic
        # signatures on both 1.x and 2.x (the 1.x name is gone there).
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)
        except AttributeError:           # paho-mqtt 1.x
            client = mqtt.Client()
        self._client = client

        def _on_connect(c, userdata, flags, rc, *extra):
            if rc != 0:
                log.warning("MQTT connect refused (code %s) - paho "
                            "retries on its own", rc)
                return
            c.subscribe(self.cfg.topic)
            log.info("MQTT collector subscribed to %s at %s:%d",
                     self.cfg.topic, self.cfg.host, self.cfg.port)

        def _on_message(c, userdata, msg):
            self.handle_message(msg.topic, msg.payload)

        client.on_connect = _on_connect
        client.on_message = _on_message
        try:
            client.connect_async(self.cfg.host, self.cfg.port,
                                 keepalive=60)
            client.loop_start()
            log.info("MQTT collector running (host %s:%d, topic %s)",
                     self.cfg.host, self.cfg.port, self.cfg.topic)
            await stop.wait()
        except Exception:                # noqa: BLE001 - a dead broker must not take the node down
            log.exception("MQTT collector failed - collection idles "
                          "(the gap is honest)")
            await stop.wait()
        finally:
            try:
                client.loop_stop()
                client.disconnect()
            except Exception:            # noqa: BLE001 - best-effort teardown
                pass
            log.info("MQTT collector stopped (%s heard, %s skipped)",
                     self.stats["heard"], self.stats["skipped"])

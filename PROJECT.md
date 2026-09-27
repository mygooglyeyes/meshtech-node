# PROJECT.md - what this project IS (plain words)

The rulebook: Buffy checks every build idea against this file.
Anything it does not answer = a question to Brett, never a guess.
Brett verified the facts below. Change nothing here without his OK.

---

## The one-sentence version

A box 'hilltop' - 192.168.12.145 - listens to ALL radio traffic around it, figures out
the shape and health of the local mesh, and hands that finished
picture to display devices over the network instead of over the air
(for now).

---

## How a packet travels (the picture)

```
   radio signals in the air
          |
          v
 +------------------+
 |  PiMesh radio    |   hears EVERYTHING: every channel,
 |  (on hilltop)    |   every node, no keys needed to HEAR
 +------------------+
          |
          v
 +------------------+
 |  meshtech-node   |   the brain. From every packet it
 |  (the program)   |   takes: who sent it, which repeaters
 |                  |   passed it along, how strong, when.
 |                  |   Adverts give node names + positions.
 |                  |   This builds: nodes, routes, health.
 +------------------+
          |
          v
 +------------------+
 |  finished map    |   deciphered, organized, ready to show.
 |  data (TCP)      |   FOR NOW: sent over the network, NOT
 |                  |   broadcast on the radio.
 +------------------+
          |
          v
 +------------------+      +-------------------+
 |  Brett's PC      |      |  future: a phone  |
 |  web app         |      |  with its own     |
 |  (simulation of  |      |  companion radio  |
 |  the phone)      |      |  hearing the air  |
 +------------------+      +-------------------+
```

---

## The parts, in plain words

- **PiMesh radio** - the antenna hardware on hilltop. It listens.
  Brett said: the node LISTENS and does not need to repeat.
- **meshtech-node** - our Linux program on hilltop. One program,
  one process. It owns the radio.
- **The brain** (from the old meshtech-scope project) - the math
  that turns heard packets into the map: grid of 9 sections,
  node lists, routes between nodes, signal quality.
- **WebServe** - the network door where the finished data goes out
  to display devices.
- **scope-app** - the web page that draws the map. Today it runs on
  Brett's PC as the STAND-IN for the future phone app.
  SELF-CONTAINED RULE (Brett, 2026-09-21): the app always runs from
  the DEVICE IT SITS ON - Brett's PC serves the app to Brett's PC;
  hilltop serves its own copy to itself. Pages never travel between
  machines. The ONLY thing that crosses the network is the feed
  data. The app's network link REPLACES the companion-radio (BLE)
  link and nothing else - it carries the same feed packets the
  companion radio would have delivered off the air. Hilltop's data
  door hands out DATA to trusted devices; it never serves pages to
  them.
- **#scope** - the radio channel our devices will use LATER to put
  the map on the air and take update requests. It is an OUTPUT
  channel for our devices - not the thing we listen to for map
  data. Map data comes from hearing EVERYTHING.

---

## The end goal (so nothing drifts)

A phone app + a small companion radio. The companion hears the
map data on the air; the phone draws it. Brett's PC web app is the
rehearsal for that - same data, same screen, network instead of RF.

---

## Hard rules (learned the hard way)

1. Transmit stays OFF until Brett says otherwise. One toggle
   (txmode) controls it.
2. Never fabricate data. A missing number is shown as missing.
3. The radio hears ALL traffic. Header facts (path, hops, signal,
   timing) are taken from every packet. Adverts (name + position)
   are taken from every packet. Nothing else is readable without
   keys - and we do not need keys for the map.
4. One change at a time, described in plain words first, Brett
   says go, THEN it is built.
5. If Buffy does not know, Buffy says "I don't know" and finds out
   - never a guess dressed as a fact.
6. THE AUDIENCE RULE (Brett, 2026-09-26, proven live on the air):
   the node's OWN broadcasts (its map updates) fly only while a
   phone app is actually listening. The app proves it with a tiny
   HEARTBEAT packet every 2 minutes (plus its first map ask); 5
   minutes of silence and the node's broadcasts go quiet. No app
   listening = no airtime spent. The phone asks (a refresh) are
   ALWAYS answered. The repeater door (openhop transmitting
   through the modem) and the PC web app's TCP door are NOT the
   audience - they follow their own rules and never wake the map.

---

## The airtime heartbeat (the audience rule in bytes)

```
 phone app (open, map up)                 hilltop
       |                                     |
       |-- HEARTBEAT (8 bytes) ------------->|  window open:
       |     every 2 minutes                 |  node broadcasts flow
       |                                     |
       X  (app closed)                      |
       |  ... no heartbeat for 5 min ...    |
       |                                     |  window closes:
       |                                     |  node broadcasts quiet
```

- One new packet type on the feed wire: HEARTBEAT (0x5313), 8
  bytes, nothing in it but a sequence number - the SMALLEST packet
  the format can carry.
- Proven live 2026-09-26: heartbeat heard -> "AIR AUDIENCE up";
  app closed -> 5 min later "AIR AUDIENCE gone - broadcasts go
  quiet".
- App side: meshtech-app v00.000.028; node side: v00.000.058.

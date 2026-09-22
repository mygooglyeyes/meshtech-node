# ATTRIBUTION - sources, provenance, and licenses

Everything meshtech-node builds on, and what each piece is licensed
under. Licenses below were verified from the license files / package
metadata on the reference machines (2026-09-20) - not quoted from
memory.

## Code and design sources

| Source | What the node uses | License |
| --- | --- | --- |
| **openhop_core** (github.com, Lloyd Newton, MIT, (c) 2025) | The wire-protocol REFERENCE this project verified everything against: payload type constants, packet frame layout, the four crypto primitives (sha256, HMAC-SHA256, AES encrypt/decrypt) vendored line-for-line into `src/meshtech_node/packets.py`, and the golden crypto test vectors generated from its CryptoUtils. openhop_core is a Python reimplementation of MeshCore. | MIT |
| **meshtech-bot / lora-bot** (Brett Stewart, MIT, (c) 2026) | Proven code adapted into the node: the packet core (`core/mcp.py` -> `packets.py`), the cleanmodem controller client (`cleanmodem/client.py`), radio layer patterns, and the mesh answer-bot architecture that shaped the standalone design. Brett's own work. | MIT |
| **cleanmodem** (Brett Stewart) | Ships inside this repo (`cleanmodem/`): the radio server that owns the SX1262 (SPI/GPIO) and serves it over an authenticated loopback TCP port. Proven on hilltop before this project existed. Brett's own work. | MIT (project's terms) |
| **meshtech-scope** (Brett Stewart) | The scope brain: feed builder, budget/airtime rules, section election, grid geometry, and the wire packet formats (PULSE 5301, BACKGROUND 5302, LAYOUT 5305) adapted into `src/meshtech_node/`. Brett's own work. | MIT (project's terms) |
| **meshtech-plugin (answer bot)** (Brett Stewart) | The disk database: `core/store.py`'s Store adopted as `node_store.py` (nodes + repeaters tables, WAL + synchronous=NORMAL flash tuning, numbered migrations, upsert keeps-known-values rule) - Brett's own work, reused per his instruction (2026-09-21). Raw packet/message tables deliberately NOT adopted (scope rule). | MIT (project's terms) |
| **scope-app** (Brett Stewart) | The browser app served by the node from `app/` (built bundle). Brett's own work. | MIT (project's terms) |

## Data sources

| Source | Where it appears | License |
| --- | --- | --- |
| **Natural Earth** (naturalearthdata.com) 10m coastline/land polygons | Baked into the built web app's map layer (`app/`), originally via scope-app's `tools/bake_coast.py` | Public domain |

## Runtime dependencies (pip)

| Package | License | Role |
| --- | --- | --- |
| aiohttp | Apache-2.0 AND MIT (dual) | WebSocket feed server + modem link |
| pycryptodome | BSD, Public Domain | AES channel crypto |
| pytest / pytest-asyncio | MIT | Tests only - never runs in production |

## Protocol acknowledgment

The wire protocol meshtech-node speaks is the MeshCore-compatible
protocol that openhop_core implements. MeshCore (meshcore-dev/meshcore)
is the upstream protocol and firmware project this ecosystem derives
from.

## What is original here

`src/meshtech_node/` glue and everything under `app/`-serving,
WebServe, the manage.sh operator tooling, and this repo's packaging
are new work (2026, Brett Stewart) built from the sources above.

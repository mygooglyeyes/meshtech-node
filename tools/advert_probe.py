#!/usr/bin/env python3
"""Offline advert probe - decode + recipe-check a captured advert payload.

Companion to the reject capture in rawsource.py (the every-advert-
rejects hunt, 2026-09-23). Feed it the `prefix <hex>` from an
"advert REJECTED" capture line (or any advert payload as hex, args or
stdin, one blob per line - it also parses a pasted raw log line).

For each payload it prints:
- payload length, pubkey prefix, the 4-byte LE timestamp (raw + UTC)
- the parser's read of the advert (flags / lat / lon / name)
- the production gate's verdict (verify_advert_signature)
- the recipe battery's verdicts (advert_recipe_reports) - which, if
  any, of the plausible signing recipes verifies THIS payload

Run with the node's venv python (needs pynacl):
    python tools/advert_probe.py <hex>
    python tools/advert_probe.py < pasted-log-line.txt
"""
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from meshtech_node.packets import (  # noqa: E402
    advert_recipe_reports, parse_advert, verify_advert_signature,
)

_HEX = set("0123456789abcdefABCDEF")


def extract_hexes(text: str) -> list:
    """Pull hex payload blobs out of pasted log lines or bare hex."""
    out = []
    for line in text.splitlines():
        if "prefix" in line:                 # raw capture log line
            line = line.split("prefix", 1)[1]
        if "|" in line:                      # drop appended verdicts
            line = line.split("|", 1)[0]
        h = "".join(c for c in line if c in _HEX)
        if len(h) >= 200:                    # at least 100 bytes
            out.append(h)
    return out


def probe(payload: bytes) -> None:
    print("=" * 72)
    print(f"payload: {len(payload)} bytes")
    print(f"pubkey prefix : {payload[0:8].hex()}")
    ts = int.from_bytes(payload[32:36], "little")
    iso = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
    print(f"timestamp     : {ts} ({iso})")
    gate = verify_advert_signature(payload)
    print(f"production gate (reference recipe): "
          f"{'PASS' if gate else 'FAIL'}")
    info = parse_advert(payload)
    if info is not None:
        appdata = payload[100:]
        print(f"appdata ({len(appdata)} B): flags=0x{info.flags:02x} "
              f"lat={info.lat} lon={info.lon} name={info.name!r}")
        if len(appdata) > 0 and appdata[0] & 0x10 and len(appdata) < 10:
            print("  (capture cut inside the coordinates - full payload "
                  "was longer)")
        elif len(appdata) >= 10 and appdata[0] & 0x80 and len(appdata) <= 110:
            pass
        if len(payload) > 110:
            pass
    print("recipe battery:")
    reports = advert_recipe_reports(payload)
    if not reports:
        print("  (payload malformed or pynacl missing - nothing to try)")
    for name, ok in reports:
        print(f"  [{'VERIFIES' if ok else '   no   '}] {name}")
    if reports and not any(ok for _n, ok in reports):
        print("  -> NO known recipe verifies: the payload is neither a")
        print("     clean reception of any tested layout nor self-")
        print("     consistent. Paste the FULL capture line (length +")
        print("     hex) back to the agent - more variants may be needed.")
    print()


def main() -> None:
    text = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    blobs = extract_hexes(text)
    if not blobs:
        print(__doc__)
        sys.exit(1)
    for h in blobs:
        try:
            probe(bytes.fromhex(h))
        except ValueError as exc:
            print(f"bad hex blob ({exc}): {h[:40]}...")


if __name__ == "__main__":
    main()

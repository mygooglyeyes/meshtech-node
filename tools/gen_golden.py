"""Regenerate tests/golden_vectors.json from the NODE's codec.

The scope codec test (scope-app/src/lib/codec.test.ts) pins the same
hex, so both sides stay byte-compatible. Run after any wire change:

    .venv/Scripts/python tools/gen_golden.py

Writes only the JSON the tests consume (the human-readable GOLDEN.md
lives in the original meshtech-scope repo).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from meshtech_node import codec  # noqa: E402


def main() -> int:
    pulse = codec.encode_pulse(codec.Pulse(
        seq=0x0113, uptime_min=1234, rx_per_hour=4, feed_airtime_s_per_h=9,
        active_total=40, origin=0xB17E,
        section_counts=[9, 3, 5, 8, 2, 0, 1, 4, 6]))
    sect = codec.encode_sect_sum(codec.SectSum(
        seq=4, section_id=1, active_nodes=1, packet_count=126,
        delay_p50_s=48, delay_p90_s=816, origin=0xB17E,
        route_stubs=[0x0221, 0xABCD]))
    route = codec.encode_route(codec.Route(
        seq=2, section_id=1, route_id=0xBEEF, packet_count=56,
        delay_med_s=4, last_heard_min=17, origin=0xB17E,
        prefixes=[0x11, 0x22, 0x33]))
    layout = codec.encode_layout(codec.Layout(
        seq=3, grid=3, center_lat=1.2345, center_lon=-1.5, span_m=40000,
        origin=0xB17E, name="demo"))
    intro = codec.encode_intro(codec.Intro(
        seq=2, origin=0xB17E, center_lat=0.0, center_lon=0.0,
        span_m=40000.0,
        entries=[codec.IntroEntry(prefix=0x11, name="Hilltop",
                                  lat=0.0004, lon=0.0004),
                 codec.IntroEntry(prefix=0x22, name="Alice")]))
    refresh = codec.encode_refresh_req(codec.RefreshReq(
        seq=2, kind=codec.REFRESH_KIND_ROUTE, target=0xBEEF, nonce=0x1234,
        origin=0x0042, host=0xB17E))

    vectors = {
        "pulse": pulse.hex(),
        "sect_sum": sect.hex(),
        "route": route.hex(),
        "layout": layout.hex(),
        "intro": intro.hex(),
        "refresh": refresh.hex(),
    }
    out_json = Path(__file__).resolve().parent.parent / "tests" / \
        "golden_vectors.json"
    out_json.write_text(json.dumps(vectors, indent=2) + "\r\n",
                        encoding="utf-8")
    for name, hexstr in vectors.items():
        print(f"{name}: {hexstr}")
    print(f"\nwrote {out_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

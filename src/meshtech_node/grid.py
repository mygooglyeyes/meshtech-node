"""Section geometry - a grid x rows tiling of the square area.

Sections are numbered ROW-MAJOR FROM THE NORTH-WEST CORNER, and since
PROTOCOL v1.2 the numbering is 1-BASED EVERYWHERE (wire, logs, UI):
1 = NW, grid = NE, grid*rows = SE. Id 0 is RESERVED (whole-area marker
in refresh targets) and never a square - the old 0-based wire ids put
"section 0" in logs while screens showed "Section 1", which confused
humans (Brett 2026-09-20) and made "whole map" and "upper-left"
impossible to say apart on the wire. Both host and client derive
geometry from the LAYOUT alone - section corners never travel on the
wire.

3x4 (Brett, 2026-09-25): "since most phone screens are tall and
narrow, the 3x4 would be the better choice" - the area is cut 3 ACROSS
x 4 DOWN = 12 sections, numbered 1 (upper left) through 12 (lower
right), matching the 3x4 squares already drawn on the phone map.
`grid` stays the WIRE's name for the across-count (the LAYOUT byte);
`rows` is 0 in a legacy packet and means "same as grid" (square).
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# Metres per degree of latitude (good enough everywhere for a hobby map).
METERS_PER_DEGREE = 111320.0


@dataclass
class Section:
    section_id: int
    col: int
    row: int
    west: float
    east: float
    south: float
    north: float


@dataclass
class GridGeometry:
    grid: int
    center_lat: float
    center_lon: float
    span_m: int
    # 0 = legacy square (grid x grid, pre-3x4). 4 = Brett's phone
    # shape: grid across x rows down (2026-09-25).
    rows: int = 0

    def __post_init__(self) -> None:
        if self.rows <= 0:
            self.rows = self.grid  # legacy: the tiling was square

    @property
    def span_deg(self) -> float:
        return self.span_m / METERS_PER_DEGREE

    @property
    def west(self) -> float:
        return self.center_lon - self.span_deg / 2.0

    @property
    def east(self) -> float:
        return self.center_lon + self.span_deg / 2.0

    @property
    def north(self) -> float:
        return self.center_lat + self.span_deg / 2.0

    @property
    def south(self) -> float:
        return self.center_lat - self.span_deg / 2.0

    @property
    def section_count(self) -> int:
        return self.grid * self.rows

    def section(self, section_id: int) -> Section:
        """Geometry for one section id (row-major from NW, 1-based).

        grid across (columns) x rows down; 1 = upper left,
        grid*rows = lower right (Brett's 3x4, 2026-09-25)."""
        if not 1 <= section_id <= self.section_count:
            raise ValueError(f"section_id out of range: {section_id}")
        row, col = divmod(section_id - 1, self.grid)
        width = self.span_deg / self.grid   # one square: E-W size
        height = self.span_deg / self.rows  # one square: N-S size
        north = self.north - row * height
        south = north - height
        west = self.west + col * width
        return Section(
            section_id=section_id, col=col, row=row,
            west=west, east=west + width, south=south, north=north,
        )

    def section_for(self, lat: float, lon: float) -> int:
        """1-based section id containing a position, or -1 outside the area.

        -1 stays the honest "not on the map" answer (unchanged)."""
        if not (self.south <= lat <= self.north and self.west <= lon <= self.east):
            return -1
        width = self.span_deg / self.grid
        height = self.span_deg / self.rows
        col = int((lon - self.west) / width)
        row = int((self.north - lat) / height)
        col = min(col, self.grid - 1)
        row = min(row, self.rows - 1)
        return row * self.grid + col + 1


def geometry_from(center_lat: float, center_lon: float, span_m: float,
                  grid: int, rows: int = 0) -> GridGeometry:
    """rows=0 keeps the legacy square tiling (grid x grid)."""
    return GridGeometry(grid=grid, center_lat=center_lat,
                        center_lon=center_lon, span_m=int(span_m), rows=rows)


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres (for honest 'est. distance' labels)."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))

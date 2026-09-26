"""Grid geometry tests - section math is the map's foundation."""
import pytest

from meshtech_node.grid import GridGeometry, geometry_from


@pytest.fixture
def geo():
    # 40 km square centred on (37.0, -122.0)
    return geometry_from(37.0, -122.0, 40000.0, 3)


def test_corners_and_center(geo):
    assert geo.section_count == 9
    # centre of the area -> centre section (id 5 in a 3x3, v1.2 1-based)
    assert geo.section_for(37.0, -122.0) == 5
    # NW corner -> section 1 (v1.2: upper-left IS section 1)
    assert geo.section_for(geo.north - 0.0001, geo.west + 0.0001) == 1
    # NE corner -> section 3
    assert geo.section_for(geo.north - 0.0001, geo.east - 0.0001) == 3
    # SE corner -> section 9
    assert geo.section_for(geo.south + 0.0001, geo.east - 0.0001) == 9


def test_outside_area(geo):
    assert geo.section_for(50.0, -122.0) == -1
    assert geo.section_for(37.0, -140.0) == -1


def test_section_geometry_roundtrip(geo):
    for sid in range(1, 10):
        s = geo.section(sid)
        cx = (s.west + s.east) / 2
        cy = (s.south + s.north) / 2
        assert geo.section_for(cy, cx) == sid


def test_section_bounds_tile(geo):
    total_area = sum(
        (geo.section(i).north - geo.section(i).south)
        * (geo.section(i).east - geo.section(i).west)
        for i in range(1, 10))
    full = (geo.north - geo.south) * (geo.east - geo.west)
    assert abs(total_area - full) < 1e-12


def test_bad_section_id(geo):
    with pytest.raises(ValueError):
        geo.section(10)
    with pytest.raises(ValueError):
        geo.section(0)      # 0 is reserved (whole-area), never a square
    with pytest.raises(ValueError):
        geo.section(-1)


def test_3x4_phone_shape():
    """Brett's map (2026-09-25): 3 ACROSS x 4 DOWN = 12 sections,
    numbered 1 (upper left) through 12 (lower right) - the same
    squares the phone map draws."""
    geo = geometry_from(37.0, -122.0, 60000.0, 3, rows=4)
    assert geo.section_count == 12
    assert geo.section_for(geo.north - 0.0001, geo.west + 0.0001) == 1
    assert geo.section_for(geo.north - 0.0001, geo.east - 0.0001) == 3
    assert geo.section_for(geo.south + 0.0001, geo.west + 0.0001) == 10
    assert geo.section_for(geo.south + 0.0001, geo.east - 0.0001) == 12
    # row-major from NW: 1 starts the north row, 12 ends the south
    assert (geo.section(1).row, geo.section(1).col) == (0, 0)
    assert (geo.section(4).row, geo.section(4).col) == (1, 0)
    assert (geo.section(12).row, geo.section(12).col) == (3, 2)
    # every square round-trips through its own centre
    for sid in range(1, 13):
        s = geo.section(sid)
        assert geo.section_for((s.south + s.north) / 2,
                               (s.west + s.east) / 2) == sid
    # and the 12 squares tile the box exactly
    total = sum((geo.section(i).north - geo.section(i).south)
                * (geo.section(i).east - geo.section(i).west)
                for i in range(1, 13))
    full = (geo.north - geo.south) * (geo.east - geo.west)
    assert abs(total - full) < 1e-12


def test_legacy_rows_zero_is_square():
    """rows=0 (a legacy LAYOUT or old test) keeps the square tiling."""
    geo = GridGeometry(grid=3, center_lat=37.0, center_lon=-122.0,
                       span_m=40000)
    assert geo.rows == 3
    assert geo.section_count == 9

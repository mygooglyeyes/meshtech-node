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

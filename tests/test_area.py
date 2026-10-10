import pytest

from app.services.gee_analysis import nominal_pixel_footprint_hectares


def test_375_meter_nominal_pixel_footprint():
    assert nominal_pixel_footprint_hectares(1, 375) == pytest.approx(14.0625)


def test_1_kilometer_nominal_pixel_footprint():
    assert nominal_pixel_footprint_hectares(1, 1000) == pytest.approx(100)


def test_multiple_pixels_are_summed():
    assert nominal_pixel_footprint_hectares(3, 375) == pytest.approx(42.1875)


@pytest.mark.parametrize("count,size", [(-1, 375), (1, 0), (1, -1)])
def test_invalid_pixel_footprints_are_rejected(count, size):
    with pytest.raises(ValueError):
        nominal_pixel_footprint_hectares(count, size)

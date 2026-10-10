import pytest

from app.services.viirs_daily import is_viirs_fire_mask


@pytest.mark.parametrize("fire_class", [7, 8, 9, "7.0"])
def test_documented_fire_classes_are_accepted(fire_class):
    assert is_viirs_fire_mask(fire_class)


@pytest.mark.parametrize("non_fire_class", [None, "bad", 0, 1, 2, 3, 4, 5, 6, 8.4, 9.1, 10])
def test_non_fire_and_invalid_classes_are_rejected(non_fire_class):
    assert not is_viirs_fire_mask(non_fire_class)

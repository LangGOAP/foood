"""San Francisco by default (issue #31): where the kitchens, the home and the start points are."""

from __future__ import annotations

from foood.catalog import distance_km
from foood.launch import CUSTOMER_HOME, DRIVER_START
from foood.scenario import Bench
from foood.seed import RESTAURANTS

LAT, LON = (37.70, 37.82), (-122.53, -122.35)  # the city, Lake Merced to the Embarcadero


def point(text: str) -> tuple[float, float]:
    lat, lon = (float(part) for part in text.split(","))
    return lat, lon


def in_san_francisco(lat: float, lon: float) -> bool:
    return LAT[0] <= lat <= LAT[1] and LON[0] <= lon <= LON[1]


def test_every_seed_kitchen_is_in_san_francisco() -> None:
    for spec in RESTAURANTS:
        assert in_san_francisco(spec["latitude"], spec["longitude"]), spec["name"]


def test_home_start_and_drop_off_are_in_san_francisco() -> None:
    for lat, lon in (point(CUSTOMER_HOME), point(DRIVER_START), Bench().drop_off):
        assert in_san_francisco(lat, lon)


def test_downtown_kitchens_deliver_home_and_the_two_far_ones_do_not() -> None:
    """The cluster keeps its shape: two kitchens beyond the 8 km radius, the rest within 5 km."""
    home = point(CUSTOMER_HOME)
    away = {
        spec["name"]: distance_km(*home, spec["latitude"], spec["longitude"])
        for spec in RESTAURANTS
    }
    assert {name for name, km in away.items() if km > 8.0} == {"Cairo Kitchen", "Green Bowl"}
    assert all(
        km <= 5.0 for name, km in away.items() if name not in {"Cairo Kitchen", "Green Bowl"}
    )


def test_the_driver_starts_around_the_corner_and_the_drop_off_is_close() -> None:
    home, start = point(CUSTOMER_HOME), point(DRIVER_START)
    assert distance_km(*home, *start) < 2.0
    assert distance_km(*home, *Bench().drop_off) < 2.0

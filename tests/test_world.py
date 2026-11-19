"""The world (issue #43): weather, traffic and road closures that every engine reads through the
same MCP tools, that the scenario and the operator move, and that the simulation obeys."""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

import httpx
import pytest

from foood.app import create_app
from foood.orders import RIDE_SPEED_KMH
from foood.scenario import Injection, Kitchen, Scenario
from foood.store import CLOCK, Store, seed_demo
from foood.world import (
    CORRIDORS,
    ZONES,
    close_road,
    eta_minutes,
    get_traffic,
    get_weather,
    prep_factor,
    schedule,
    set_traffic,
    set_weather,
    speed_factor,
    tick,
    zone_of,
)
from foood.world_mcp import build_server
from tests.support import login
from tests.test_scenario import (
    QA_OWNER,
    _advance,
    _order,
)
from tests.test_tracking import _ready_order

QA_KITCHEN = (37.7731, -122.4167)
BURGER_PALACE = (37.7856, -122.4083)
THE_DOOR = (37.787, -122.3985)
GREEN_BOWL = (37.757, -122.503)


def _store() -> Store:
    return seed_demo(Store())


def test_every_kitchen_falls_in_a_zone_and_the_door_is_downtown() -> None:
    store = _store()
    assert zone_of(*QA_KITCHEN) == "civic"
    assert zone_of(*BURGER_PALACE) == "downtown"
    assert zone_of(*THE_DOOR) == "downtown"
    assert zone_of(*GREEN_BOWL) == "sunset"
    for restaurant in store.restaurants.values():
        assert zone_of(restaurant.latitude, restaurant.longitude) in ZONES
    assert ("civic", "downtown") in CORRIDORS.values()


def test_a_clear_world_changes_nothing() -> None:
    store = _store()
    for zone in ZONES:
        assert get_weather(store, zone) == {"zone": zone, "kind": "clear", "until": None}
    assert speed_factor(store, QA_KITCHEN, THE_DOOR) == 1.0
    assert prep_factor(store, "civic") == 1.0
    plain = eta_minutes(store, QA_KITCHEN, THE_DOOR)
    from foood.catalog import distance_km

    km = distance_km(*QA_KITCHEN, *THE_DOOR)
    assert plain == int(round(km / RIDE_SPEED_KMH * 60))
    traffic = get_traffic(store, QA_KITCHEN, THE_DOOR)
    assert traffic["delayFactor"] == 1.0 and traffic["closures"] == []


def test_a_storm_slows_the_ride_and_stretches_prep() -> None:
    store = _store()
    heard: list[dict] = []
    store.subscribe("world.changed", lambda change: heard.append(change))
    before = eta_minutes(store, QA_KITCHEN, THE_DOOR)
    set_weather(store, "civic", "storm")
    assert get_weather(store, "civic")["kind"] == "storm"
    assert speed_factor(store, QA_KITCHEN, THE_DOOR) < 1.0
    assert prep_factor(store, "civic") > 1.0
    assert eta_minutes(store, QA_KITCHEN, THE_DOOR) > before
    assert heard and heard[0]["weather"] == {"zone": "civic", "kind": "storm"}
    assert store.world.log[-1]["event"] == "weather" and store.world.log[-1]["zone"] == "civic"
    # a route that never touches the zone is untouched
    assert speed_factor(store, BURGER_PALACE, THE_DOOR) == 1.0


def test_a_closed_corridor_is_a_detour_and_traffic_is_a_delay() -> None:
    store = _store()
    before = eta_minutes(store, QA_KITCHEN, THE_DOOR)
    close_road(store, "market-street")
    detour = eta_minutes(store, QA_KITCHEN, THE_DOOR)
    assert detour > before * 2
    assert get_traffic(store, QA_KITCHEN, THE_DOOR)["closures"] == ["market-street"]
    assert eta_minutes(store, BURGER_PALACE, THE_DOOR) == eta_minutes(
        _store(), BURGER_PALACE, THE_DOOR
    )
    close_road(store, "market-street", closed=False)
    assert eta_minutes(store, QA_KITCHEN, THE_DOOR) == before
    set_traffic(store, "downtown", 2.0)
    assert get_traffic(store, QA_KITCHEN, THE_DOOR)["delayFactor"] > 1.0
    assert eta_minutes(store, QA_KITCHEN, THE_DOOR) > before
    with pytest.raises(ValueError):
        close_road(store, "golden-gate")
    with pytest.raises(ValueError):
        set_weather(store, "civic", "snow")


def test_changes_expire_and_the_schedule_runs_on_the_tick() -> None:
    store = _store()
    CLOCK.freeze()
    set_weather(store, "civic", "rain", for_seconds=60)
    assert get_weather(store, "civic")["until"] is not None
    schedule(store, 30, {"traffic": {"zone": "downtown", "factor": 1.5}})
    CLOCK.advance(31)
    tick(store)
    assert get_weather(store, "civic")["kind"] == "rain"
    assert store.world.traffic["downtown"] == 1.5
    CLOCK.advance(30)
    tick(store)
    assert get_weather(store, "civic") == {"zone": "civic", "kind": "clear", "until": None}
    events = [e["event"] for e in store.world.log]
    assert events == ["weather", "scheduled", "traffic", "weather"]


def _payload(result) -> dict:
    """An MCP tool result: the structured content when the server sends it, else the JSON text."""
    import json

    structured = getattr(result, "structuredContent", None) or getattr(
        result, "structured_content", None
    )
    return structured if structured is not None else json.loads(result.content[0].text)


@contextlib.asynccontextmanager
async def _served(**kwargs) -> AsyncIterator[httpx.AsyncClient]:
    """The app with its lifespan running, which the MCP transport needs (the fixtures skip it)."""
    app = create_app(simulate=False, **kwargs)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            client.store = app.state.store  # type: ignore[attr-defined]
            yield client


async def test_the_world_speaks_mcp() -> None:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async with _served() as client:
        http = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=client._transport.app),  # type: ignore[attr-defined]
            base_url="http://test",
        )
        async with streamable_http_client("http://test/mcp/", http_client=http) as (
            read,
            write,
            *_,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                names = {t.name for t in (await session.list_tools()).tools}
                assert {"get_weather", "get_traffic", "eta", "forecast", "zones"} <= names
                assert {"set_weather", "set_traffic", "close_road", "schedule"} <= names
                weather = await session.call_tool("get_weather", {"zone": "civic"})
                assert _payload(weather)["kind"] == "clear"
                moved = await session.call_tool(
                    "set_weather", {"zone": "civic", "kind": "storm", "for_seconds": 90}
                )
                assert _payload(moved)["kind"] == "storm"
        assert client.store.world.weather["civic"] == "storm"
        status = (await client.get("/api/_world")).json()
        assert status["weather"]["civic"] == "storm" and status["log"][-1]["event"] == "weather"


def test_the_world_server_leaves_the_process_logging_alone() -> None:
    """MCPServer configures the root logger at INFO with a rich handler unless told otherwise;
    that renders every library's INFO line in the process, the planner's included, inside the
    rounds the bench times. The world's server is a WARNING server."""
    assert build_server(Store()).settings.log_level == "WARNING"


def test_the_agent_tools_are_the_read_tools_only() -> None:
    from foood.world_mcp import read_tool_specs

    specs = read_tool_specs()
    names = {s["name"] for s in specs}
    assert names == {"get_weather", "get_traffic", "eta", "forecast", "zones"}
    assert all(s["description"] and "properties" in s["parameters"] for s in specs)


async def test_the_simulated_phone_slows_in_a_storm(client) -> None:
    from foood.catalog import distance_km
    from foood.tracking import simulate_step

    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    order = await _ready_order(
        client, sara, owner, deliveryLatitude=THE_DOOR[0], deliveryLongitude=THE_DOOR[1]
    )
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()

    async def position() -> tuple[float, float]:
        snap = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
        return snap["location"]["latitude"], snap["location"]["longitude"]

    simulate_step(client.store)
    first = await position()
    simulate_step(client.store)
    second = await position()
    clear_km = distance_km(*first, *second)
    set_weather(client.store, "civic", "storm")
    simulate_step(client.store)
    third = await position()
    storm_km = distance_km(*second, *third)
    assert 0 < storm_km < clear_km * 0.7


async def test_the_ride_is_judged_by_its_whole_leg(client) -> None:
    """A closed corridor slows the ride from the kitchen to the door even once the courier has
    crossed into the door's zone: the detour is the leg's, not the courier's current block."""
    from foood.catalog import distance_km
    from foood.store import now
    from foood.tracking import Location, simulate_step

    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    order = await _ready_order(
        client, sara, owner, deliveryLatitude=THE_DOOR[0], deliveryLongitude=THE_DOOR[1]
    )
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    for _ in range(80):  # the courier reaches the kitchen
        simulate_step(client.store)
    await client.post(f"/api/deliveries/{delivery['id']}/pickup", json={}, headers=driver)
    await client.post(f"/api/deliveries/{delivery['id']}/start", json={}, headers=driver)
    uid = client.store.user_by_email("driver@example.com").id
    # put the courier on the downtown side of the route, past the corridor
    moment = now()
    client.store.locations[uid] = Location(
        uid, 37.7845, -122.4045, recorded_at=moment, received_at=moment, simulated=True
    )

    async def position() -> tuple[float, float]:
        snap = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
        return snap["location"]["latitude"], snap["location"]["longitude"]

    before = await position()
    assert zone_of(*before) == "downtown"
    simulate_step(client.store)
    open_km = distance_km(*before, *(await position()))
    close_road(client.store, "market-street")
    mid = await position()
    simulate_step(client.store)
    closed_km = distance_km(*mid, *(await position()))
    assert 0 < closed_km < open_km * 0.5


async def test_the_kitchen_takes_longer_in_a_storm(scenario_client) -> None:
    scenario = Scenario(
        "stormy",
        kitchen=Kitchen(autopilot=True, start_after=5, ready_after=60),
        injections=(Injection("storm", order=1, after_seconds=0, for_seconds=600),),
    )
    async with scenario_client(scenario) as c:
        sara = await login(c, "demo.customer@example.com")
        order = await _order(c, sara)
        assert c.store.world.weather["civic"] == "storm"
        _advance(c.engine, 66)
        assert (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()[
            "status"
        ] == "PREPARING"
        _advance(c.engine, 30)
        assert (await c.get(f"/api/orders/{order['id']}", headers=sara)).json()[
            "status"
        ] == "READY_FOR_PICKUP"
        fired = [e for e in c.engine.log if e["event"] == "fired"]
        assert fired[0]["failure"] == "storm" and fired[0]["zone"] == "civic"


async def test_storm_road_and_traffic_injections_clear_after_their_window(scenario_client) -> None:
    scenario = Scenario(
        "weather",
        kitchen=Kitchen(autopilot=True),
        injections=(
            Injection("storm", order=1, after_seconds=10, for_seconds=30, zone="downtown"),
            Injection(
                "road_closed", order=1, after_seconds=0, for_seconds=20, corridor="market-street"
            ),
            Injection(
                "traffic", order=1, after_seconds=0, for_seconds=20, factor=2.0, zone="downtown"
            ),
        ),
    )
    async with scenario_client(scenario) as c:
        sara = await login(c, "demo.customer@example.com")
        await _order(c, sara)
        world = c.store.world
        assert "market-street" in world.closed and world.traffic["downtown"] == 2.0
        assert get_weather(c.store, "downtown")["kind"] == "clear"
        _advance(c.engine, 11)
        assert get_weather(c.store, "downtown")["kind"] == "storm"
        _advance(c.engine, 10)
        assert "market-street" not in world.closed and world.traffic.get("downtown", 1.0) == 1.0
        assert get_weather(c.store, "downtown")["kind"] == "storm"
        _advance(c.engine, 20)
        assert get_weather(c.store, "downtown")["kind"] == "clear"
        cleared = [e for e in c.engine.log if e["event"] == "cleared"]
        assert {e["failure"] for e in cleared} == {"storm", "road_closed", "traffic"}


async def test_a_driver_goes_offline_for_a_while(scenario_client) -> None:
    from foood.drivers import available_drivers

    scenario = Scenario(
        "thin",
        kitchen=Kitchen(autopilot=True),
        injections=(Injection("driver_offline", order=1, after_seconds=0, for_seconds=30),),
    )
    async with scenario_client(scenario) as c:
        sara = await login(c, "demo.customer@example.com")
        driver = await login(c, "driver@example.com")
        await c.post("/api/drivers/me/online", json={}, headers=driver)
        assert len(available_drivers(c.store)) == 1
        await _order(c, sara)
        assert available_drivers(c.store) == []
        me = (await c.get("/api/drivers/me", headers=driver)).json()
        assert me["status"] == "OFFLINE"
        _advance(c.engine, 31)
        assert len(available_drivers(c.store)) == 1


async def test_forecast_lists_what_is_scheduled_for_a_zone() -> None:
    from foood.world import forecast

    store = _store()
    CLOCK.freeze()
    schedule(store, 120, {"weather": {"zone": "civic", "kind": "rain"}})
    schedule(store, 900, {"weather": {"zone": "civic", "kind": "clear"}})
    schedule(store, 60, {"traffic": {"zone": "sunset", "factor": 2.0}})
    soon = forecast(store, "civic", minutes=10)
    assert [c["inSeconds"] for c in soon] == [120]
    assert soon[0]["change"] == {"weather": {"zone": "civic", "kind": "rain"}}
    assert forecast(store, "sunset", minutes=10)[0]["change"]["traffic"]["factor"] == 2.0


async def test_the_world_page_and_the_control_api(client) -> None:
    admin = await login(client, "admin@example.com")
    sara = await login(client, "demo.customer@example.com")
    assert (await client.get("/world")).status_code == 200
    denied = await client.post(
        "/api/_world/weather", json={"zone": "civic", "kind": "rain"}, headers=sara
    )
    assert denied.status_code == 403
    moved = await client.post(
        "/api/_world/weather", json={"zone": "civic", "kind": "rain"}, headers=admin
    )
    assert moved.status_code == 200 and moved.json()["weather"]["civic"] == "rain"
    closed = await client.post(
        "/api/_world/road", json={"corridor": "market-street", "closed": True}, headers=admin
    )
    assert closed.json()["closed"] == ["market-street"]
    bad = await client.post(
        "/api/_world/weather", json={"zone": "nowhere", "kind": "rain"}, headers=admin
    )
    assert bad.status_code == 400


async def test_a_rerouted_courier_takes_the_known_detour(client) -> None:
    """A closed corridor costs a courier who wanders into it the full detour; one told of the
    closure takes the known way round, slower than a clear day but not by as much."""
    from foood.store import now
    from foood.tracking import Location, simulate_step
    from foood.world import DETOUR_FACTOR, REROUTED_FACTOR, reroute

    sara = await login(client, "demo.customer@example.com")
    owner = await login(client, QA_OWNER)
    driver = await login(client, "driver@example.com")
    order = await _ready_order(
        client, sara, owner, deliveryLatitude=THE_DOOR[0], deliveryLongitude=THE_DOOR[1]
    )
    await client.post("/api/drivers/me/online", json={}, headers=driver)
    delivery = (await client.get(f"/api/deliveries/by-order/{order['id']}", headers=sara)).json()
    for _ in range(80):
        simulate_step(client.store)
    await client.post(f"/api/deliveries/{delivery['id']}/pickup", json={}, headers=driver)
    await client.post(f"/api/deliveries/{delivery['id']}/start", json={}, headers=driver)
    uid = client.store.user_by_email("driver@example.com").id
    moment = now()
    client.store.locations[uid] = Location(
        uid, 37.7760, -122.4150, recorded_at=moment, received_at=moment, simulated=True
    )
    assert speed_factor(client.store, QA_KITCHEN, THE_DOOR) == 1.0
    close_road(client.store, "market-street")
    assert speed_factor(client.store, QA_KITCHEN, THE_DOOR) == 1.0 / DETOUR_FACTOR
    assert speed_factor(client.store, QA_KITCHEN, THE_DOOR, rerouted=True) == 1.0 / REROUTED_FACTOR
    assert REROUTED_FACTOR < DETOUR_FACTOR

    async def position() -> tuple[float, float]:
        snap = (await client.get(f"/api/tracking/delivery/{delivery['id']}", headers=sara)).json()
        return snap["location"]["latitude"], snap["location"]["longitude"]

    from foood.catalog import distance_km

    a = await position()
    simulate_step(client.store)
    lost_km = distance_km(*a, *(await position()))
    rerouted = reroute(client.store, delivery["id"])
    assert rerouted["rerouted"] is True and delivery["id"] in client.store.world.rerouted
    assert client.store.world.log[-1]["event"] == "reroute"
    b = await position()
    simulate_step(client.store)
    known_km = distance_km(*b, *(await position()))
    assert lost_km < known_km < lost_km * DETOUR_FACTOR

"""The world outside the kitchens: weather, traffic and road closures (issue #43).

One state, kept on the store, that everything reads: the engines through the MCP tools in
``foood.world_mcp`` (or this module's functions in-process, which are the same functions),
the simulation (couriers slow down, kitchens take longer), the scenario engine (which moves
it on schedule to create decision points) and the operator (the ``/world`` page and its API).

The city is a handful of zones around the kitchens; a route is judged by the zones it joins.
A corridor is the road between two zones; a closed corridor turns that route into a detour.
Nothing here is random: the state only changes when something sets it, so a bench run under
a scenario and a seed sees the same world every time.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from foood.auth import CurrentUser, StoreDep
from foood.catalog import distance_km
from foood.errors import ApiError
from foood.orders import RIDE_SPEED_KMH
from foood.store import Store, World, iso, now

#: zone name -> centre; a point belongs to the nearest centre
ZONES: dict[str, tuple[float, float]] = {
    "downtown": (37.7875, -122.4082),
    "north-beach": (37.7988, -122.4084),
    "civic": (37.7737, -122.4180),
    "mission": (37.7450, -122.4200),
    "sunset": (37.7570, -122.5030),
    "lakeside": (37.7280, -122.4765),
}
#: corridor name -> the two zones it joins
CORRIDORS: dict[str, tuple[str, str]] = {
    "market-street": ("civic", "downtown"),
    "columbus-avenue": ("downtown", "north-beach"),
    "van-ness-avenue": ("mission", "civic"),
    "19th-avenue": ("sunset", "lakeside"),
}
#: weather kind -> (courier speed multiplier, kitchen prep multiplier)
WEATHER: dict[str, tuple[float, float]] = {
    "clear": (1.0, 1.0),
    "rain": (0.8, 1.2),
    "storm": (0.6, 1.5),
    "heat": (1.0, 1.1),
}
DETOUR_FACTOR = 2.5  # a closed corridor, driven around
READ_TOOLS = ("get_weather", "get_traffic", "eta", "forecast", "zones")
PAGE = Path(__file__).with_name("world.html")

Point = tuple[float, float]


# --- geography --------------------------------------------------------------------------------


def zone_of(latitude: float, longitude: float) -> str:
    return min(ZONES, key=lambda z: distance_km(latitude, longitude, *ZONES[z]))


def corridors_between(zone_a: str, zone_b: str) -> list[str]:
    pair = {zone_a, zone_b}
    return [name for name, ends in CORRIDORS.items() if set(ends) == pair]


def world_of(store: Store) -> World:
    return store.world


def kind_in(world: World, zone: str) -> str:
    return world.weather.get(zone, "clear")


# --- reading ----------------------------------------------------------------------------------


def speed_factor(store: Store, start: Point, end: Point) -> float:
    """How fast a courier moves on this route relative to a clear day (1.0)."""
    world = world_of(store)
    za, zb = zone_of(*start), zone_of(*end)
    weather = min(WEATHER[kind_in(world, za)][0], WEATHER[kind_in(world, zb)][0])
    traffic = max(world.traffic.get(za, 1.0), world.traffic.get(zb, 1.0))
    detour = DETOUR_FACTOR if any(c in world.closed for c in corridors_between(za, zb)) else 1.0
    return weather / traffic / detour


def prep_factor(store: Store, zone: str) -> float:
    """How much longer a kitchen in this zone takes relative to a clear day (1.0)."""
    return WEATHER[kind_in(world_of(store), zone)][1]


def eta_minutes(store: Store, start: Point, end: Point) -> int:
    km = distance_km(*start, *end)
    return int(round(km / (RIDE_SPEED_KMH * speed_factor(store, start, end)) * 60))


def get_weather(store: Store, zone: str) -> dict[str, Any]:
    _check_zone(zone)
    world = world_of(store)
    until = world.until.get(("weather", zone))
    return {"zone": zone, "kind": kind_in(world, zone), "until": iso(until) if until else None}


def get_traffic(store: Store, start: Point, end: Point) -> dict[str, Any]:
    world = world_of(store)
    za, zb = zone_of(*start), zone_of(*end)
    closures = [c for c in corridors_between(za, zb) if c in world.closed]
    traffic = max(world.traffic.get(za, 1.0), world.traffic.get(zb, 1.0))
    km = distance_km(*start, *end)
    base = int(round(km / RIDE_SPEED_KMH * 60))
    return {
        "from": za,
        "to": zb,
        "delayFactor": round(traffic * (DETOUR_FACTOR if closures else 1.0), 2),
        "closures": closures,
        "baseMinutes": base,
        "minutes": eta_minutes(store, start, end),
    }


def forecast(store: Store, zone: str, minutes: int = 30) -> list[dict[str, Any]]:
    """What is scheduled to change in this zone within the window, soonest first."""
    _check_zone(zone)
    horizon = now() + timedelta(minutes=minutes)
    out = []
    for due, change in sorted(world_of(store).scheduled, key=lambda s: s[0]):
        if due > horizon or not _touches(change, zone):
            continue
        out.append({"inSeconds": int((due - now()).total_seconds()), "change": change})
    return out


def _touches(change: dict[str, Any], zone: str) -> bool:
    for kind, body in change.items():
        if kind in {"weather", "traffic"} and body.get("zone") == zone:
            return True
        if kind == "road" and zone in CORRIDORS.get(body.get("corridor", ""), ()):
            return True
    return False


# --- moving -----------------------------------------------------------------------------------


def _check_zone(zone: str) -> None:
    if zone not in ZONES:
        raise ValueError(f"no zone {zone!r}; one of {sorted(ZONES)}")


def _expiry(store: Store, kind: str, key: str, for_seconds: float | None) -> datetime | None:
    world = world_of(store)
    if for_seconds is None:
        world.until.pop((kind, key), None)
        return None
    until = now() + timedelta(seconds=for_seconds)
    world.until[(kind, key)] = until
    return until


def set_weather(
    store: Store, zone: str, kind: str, for_seconds: float | None = None
) -> dict[str, Any]:
    _check_zone(zone)
    if kind not in WEATHER:
        raise ValueError(f"no weather {kind!r}; one of {sorted(WEATHER)}")
    world = world_of(store)
    if kind == "clear":
        world.weather.pop(zone, None)
    else:
        world.weather[zone] = kind
    until = _expiry(store, "weather", zone, for_seconds)
    _note(
        store,
        {"event": "weather", "zone": zone, "kind": kind, "until": iso(until) if until else None},
    )
    store.publish("world.changed", change={"weather": {"zone": zone, "kind": kind}})
    return get_weather(store, zone)


def set_traffic(
    store: Store, zone: str, factor: float, for_seconds: float | None = None
) -> dict[str, Any]:
    _check_zone(zone)
    if factor < 1.0:
        raise ValueError("a traffic factor is 1.0 (free flowing) or more")
    world = world_of(store)
    if factor == 1.0:
        world.traffic.pop(zone, None)
    else:
        world.traffic[zone] = factor
    until = _expiry(store, "traffic", zone, for_seconds)
    _note(
        store,
        {
            "event": "traffic",
            "zone": zone,
            "factor": factor,
            "until": iso(until) if until else None,
        },
    )
    store.publish("world.changed", change={"traffic": {"zone": zone, "factor": factor}})
    return {"zone": zone, "factor": factor, "until": iso(until) if until else None}


def close_road(
    store: Store, corridor: str, closed: bool = True, for_seconds: float | None = None
) -> dict[str, Any]:
    if corridor not in CORRIDORS:
        raise ValueError(f"no corridor {corridor!r}; one of {sorted(CORRIDORS)}")
    world = world_of(store)
    if closed:
        world.closed.add(corridor)
    else:
        world.closed.discard(corridor)
    until = _expiry(store, "road", corridor, for_seconds if closed else None)
    _note(
        store,
        {
            "event": "road",
            "corridor": corridor,
            "closed": closed,
            "until": iso(until) if until else None,
        },
    )
    store.publish("world.changed", change={"road": {"corridor": corridor, "closed": closed}})
    return {
        "corridor": corridor,
        "closed": closed,
        "zones": list(CORRIDORS[corridor]),
        "until": iso(until) if until else None,
    }


def apply(store: Store, change: dict[str, Any]) -> dict[str, Any]:
    """One change, as the schedule and the control tools pass it:
    ``{"weather": {"zone", "kind", "for_seconds"?}}``, ``{"traffic": {"zone", "factor", ...}}``
    or ``{"road": {"corridor", "closed", ...}}``."""
    results: dict[str, Any] = {}
    for kind, body in change.items():
        if kind == "weather":
            results[kind] = set_weather(store, body["zone"], body["kind"], body.get("for_seconds"))
        elif kind == "traffic":
            results[kind] = set_traffic(
                store, body["zone"], float(body["factor"]), body.get("for_seconds")
            )
        elif kind == "road":
            results[kind] = close_road(
                store, body["corridor"], bool(body.get("closed", True)), body.get("for_seconds")
            )
        else:
            raise ValueError(f"no such change {kind!r}; weather, traffic or road")
    return results


def schedule(store: Store, after_seconds: float, change: dict[str, Any]) -> dict[str, Any]:
    """Apply a change later; the simulator's tick (and the bench's) runs it when due."""
    _validate(change)
    due = now() + timedelta(seconds=after_seconds)
    world_of(store).scheduled.append((due, change))
    _note(
        store,
        {"event": "scheduled", "inSeconds": int(after_seconds), "due": iso(due), "change": change},
    )
    return {"due": iso(due), "change": change}


def _validate(change: dict[str, Any]) -> None:
    for kind, body in change.items():
        if kind in {"weather", "traffic"}:
            _check_zone(body["zone"])
            if kind == "weather" and body["kind"] not in WEATHER:
                raise ValueError(f"no weather {body['kind']!r}")
        elif kind == "road":
            if body["corridor"] not in CORRIDORS:
                raise ValueError(f"no corridor {body['corridor']!r}")
        else:
            raise ValueError(f"no such change {kind!r}")


def tick(store: Store) -> None:
    """Run the due schedule and let expired changes lapse; called on every simulator tick."""
    world = world_of(store)
    moment = now()
    due = [s for s in world.scheduled if s[0] <= moment]
    world.scheduled = [s for s in world.scheduled if s[0] > moment]
    for _, change in sorted(due, key=lambda s: s[0]):
        apply(store, change)
    for (kind, key), until in list(world.until.items()):
        if until > moment:
            continue
        world.until.pop((kind, key), None)
        if kind == "weather":
            set_weather(store, key, "clear")
        elif kind == "traffic":
            set_traffic(store, key, 1.0)
        elif kind == "road":
            close_road(store, key, closed=False)


def _note(store: Store, entry: dict[str, Any]) -> None:
    world_of(store).log.append({"at": iso(now()), **entry})


def install(store: Store) -> None:
    store.tickers.append(lambda: tick(store))


# --- what the engines call --------------------------------------------------------------------


class WorldTools:
    """The read tools, in-process, with the calls counted (the bench reports them per order).

    These are the same functions the MCP server exposes under the same names; an engine that
    binds the MCP tools to a model and one that calls these from code consult the same world.
    """

    def __init__(self, store: Store, order_id: str | None = None) -> None:
        self.store = store
        self.order_id = order_id

    def _count(self, tool: str) -> None:
        self.store.world.calls[tool] += 1
        if self.order_id:
            self.store.world.calls_by_order[self.order_id] += 1

    def zones(self) -> list[dict[str, Any]]:
        self._count("zones")
        return [{"zone": z, "latitude": lat, "longitude": lng} for z, (lat, lng) in ZONES.items()]

    def get_weather(self, zone: str) -> dict[str, Any]:
        self._count("get_weather")
        return get_weather(self.store, zone)

    def get_traffic(self, start: Point, end: Point) -> dict[str, Any]:
        self._count("get_traffic")
        return get_traffic(self.store, start, end)

    def eta(self, start: Point, end: Point) -> dict[str, Any]:
        self._count("eta")
        return {
            "minutes": eta_minutes(self.store, start, end),
            "speedFactor": round(speed_factor(self.store, start, end), 3),
        }

    def forecast(self, zone: str, minutes: int = 30) -> list[dict[str, Any]]:
        self._count("forecast")
        return forecast(self.store, zone, minutes)


def status(store: Store) -> dict[str, Any]:
    world = world_of(store)
    return {
        "zones": {z: {"latitude": lat, "longitude": lng} for z, (lat, lng) in ZONES.items()},
        "corridors": {c: list(ends) for c, ends in CORRIDORS.items()},
        "weather": {z: kind_in(world, z) for z in ZONES},
        "traffic": dict(world.traffic),
        "closed": sorted(world.closed),
        "scheduled": [
            {"due": iso(d), "change": c} for d, c in sorted(world.scheduled, key=lambda s: s[0])
        ],
        "calls": dict(world.calls),
        "log": list(world.log),
        "mcp": os.environ.get("FOOOD_MCP_URL", "/mcp"),
    }


# --- the operator's API and page --------------------------------------------------------------

router = APIRouter()


class WeatherChange(BaseModel):
    zone: str
    kind: str
    for_seconds: float | None = Field(default=None, ge=0)


class TrafficChange(BaseModel):
    zone: str
    factor: float = Field(ge=1.0)
    for_seconds: float | None = Field(default=None, ge=0)


class RoadChange(BaseModel):
    corridor: str
    closed: bool = True
    for_seconds: float | None = Field(default=None, ge=0)


class Scheduled(BaseModel):
    after_seconds: float = Field(ge=0)
    change: dict[str, Any]


def _admin(user: Any) -> None:
    if user.role != "ADMIN":
        raise ApiError(403, "Support only")


def _moved(store: Store, action: Any) -> dict[str, Any]:
    try:
        action()
    except ValueError as exc:
        raise ApiError(400, str(exc), error="Bad Request") from exc
    return status(store)


@router.get("/api/_world")
async def world_status(request: Request) -> dict[str, Any]:
    """The weather, the traffic and the road closures as they are now (not part of the contract)."""
    return status(request.app.state.store)


@router.post("/api/_world/weather")
async def world_weather(body: WeatherChange, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    _admin(user)
    return _moved(store, lambda: set_weather(store, body.zone, body.kind, body.for_seconds))


@router.post("/api/_world/traffic")
async def world_traffic(body: TrafficChange, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    _admin(user)
    return _moved(store, lambda: set_traffic(store, body.zone, body.factor, body.for_seconds))


@router.post("/api/_world/road")
async def world_road(body: RoadChange, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    _admin(user)
    return _moved(store, lambda: close_road(store, body.corridor, body.closed, body.for_seconds))


@router.post("/api/_world/schedule")
async def world_schedule(body: Scheduled, user: CurrentUser, store: StoreDep) -> dict[str, Any]:
    _admin(user)
    return _moved(store, lambda: schedule(store, body.after_seconds, body.change))


@router.get("/world", response_class=HTMLResponse, include_in_schema=False)
async def world_page() -> HTMLResponse:
    return HTMLResponse(PAGE.read_text(encoding="utf-8"))

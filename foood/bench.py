"""The scenario bench: a whole run with nobody at the apps, measured.

Autopilots stand in for the people: a customer who places the scenario's
orders and pays, drivers who go online and do what the driver app does when
the simulated phone arrives, and a support person who takes the recommended
answer after a while. The kitchen is the scenario's autopilot. Time is the
frozen clock advanced tick by tick, so a run with the same seed and schedule
repeats exactly. The result is a JSON file; nothing in a table is typed by
hand.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import math
import os
import statistics
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from foood import __version__
from foood.app import create_app
from foood.catalog import distance_km
from foood.deliveries import ACTIVE, current_delivery
from foood.drivers import driver_of
from foood.engine import make_engine
from foood.orders import TERMINAL
from foood.scenario import Kitchen, Scenario, load
from foood.store import CLOCK, PASSWORD, QA_PASSWORD, Store, iso, now
from foood.support import expire_due
from foood.tracking import simulate_step

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "results"
TICK_SECONDS = 5
MAX_VIRTUAL_SECONDS = 3 * 3600
ARRIVED_KM = 0.08
DRIVER_ACCOUNTS = [("driver@example.com", PASSWORD), ("qa.driver@delivery-plus.test", QA_PASSWORD)]
HAPPY_PATH = {"confirm", "create delivery", "assign driver"}


@dataclass
class Placed:
    number: int
    order_id: str
    placed_at: datetime
    notes: list[str] = field(default_factory=list)


class Run:
    def __init__(self, scenario: Scenario, engine_name: str, seed: int) -> None:
        kitchen = (
            scenario.kitchen
            if scenario.kitchen.autopilot
            else dataclasses.replace(scenario.kitchen, autopilot=True)
        )
        self.scenario = dataclasses.replace(scenario, seed=seed, kitchen=kitchen)
        self.engine_name = engine_name
        self.seed = seed
        self.app = create_app(
            simulate=False, engine=make_engine(engine_name), scenario=self.scenario
        )
        self.store: Store = self.app.state.store
        self.placed: list[Placed] = []
        self.ticks = 0
        self.target = self.scenario.bench.orders or max(
            [i.order for i in self.scenario.injections] + [2]
        )

    # --- autopilots -------------------------------------------------------------------------

    async def login(self, client: httpx.AsyncClient, email: str, password: str) -> dict[str, str]:
        r = await client.post("/api/auth/login", json={"email": email, "password": password})
        r.raise_for_status()
        return {"Authorization": f"Bearer {r.json()['accessToken']}"}

    async def customer_tick(self, client: httpx.AsyncClient, sara: dict[str, str]) -> None:
        bench = self.scenario.bench
        if len(self.placed) >= self.target:
            return
        due = self.ticks * TICK_SECONDS >= len(self.placed) * bench.interval_seconds
        if not due:
            return
        number = len(self.placed) + 1
        restaurant = next(r for r in self.store.restaurants.values() if r.name == bench.restaurant)
        menu = [
            i
            for i in self.store.menu_items.values()
            if i.restaurant_id == restaurant.id and i.available and not i.archived
        ]
        notes: list[str] = []
        wanted = [next((i for i in menu if i.name == name), None) for name in bench.items]
        for name, item in zip(bench.items, wanted, strict=True):
            if item is None:
                notes.append(f"{name} unavailable, picked another dish")
        chosen = [i for i in wanted if i is not None] or menu[:1]
        for item in chosen:
            await client.post(
                "/api/cart/items", json={"menuItemId": item.id, "quantity": 1}, headers=sara
            )
        lat, lng = bench.drop_off
        created = await client.post(
            "/api/orders",
            json={
                "deliveryAddress": "Bench drop-off",
                "deliveryLatitude": lat,
                "deliveryLongitude": lng,
            },
            headers=sara,
        )
        if created.status_code != 201:
            notes.append(f"order refused: {created.json().get('error')}")
            self.placed.append(Placed(number, "", now(), notes))
            return
        order = created.json()
        payment = await client.post("/api/payments", json={"orderId": order["id"]}, headers=sara)
        if payment.status_code == 201:
            await client.post(
                f"/api/payments/{payment.json()['id']}/process", json={}, headers=sara
            )
        self.placed.append(Placed(number, order["id"], now(), notes))

    async def driver_tick(
        self, client: httpx.AsyncClient, headers: dict[str, str], user_id: str
    ) -> None:
        driver = driver_of(self.store, user_id)
        if driver is None:
            return
        if driver.status == "OFFLINE":
            await client.post("/api/drivers/me/online", json={}, headers=headers)
            return
        job = current_delivery(self.store, driver)
        if job is None:
            return
        location = self.store.locations.get(user_id)
        if location is None:
            return
        restaurant = self.store.restaurants[job.restaurant_id]
        order = self.store.orders[job.order_id]
        if job.status == "DRIVER_ASSIGNED":
            target = (restaurant.latitude, restaurant.longitude)
            action = "pickup"
        elif job.status == "PICKED_UP":
            await client.post(f"/api/deliveries/{job.id}/start", json={}, headers=headers)
            return
        else:
            if order.delivery_latitude is None or order.delivery_longitude is None:
                return
            target = (order.delivery_latitude, order.delivery_longitude)
            action = "complete"
        if distance_km(location.latitude, location.longitude, *target) <= ARRIVED_KM:
            await client.post(f"/api/deliveries/{job.id}/{action}", json={}, headers=headers)

    async def support_tick(self, client: httpx.AsyncClient, admin: dict[str, str]) -> None:
        delay = self.scenario.bench.support_delay_seconds
        for question in list(self.store.questions.values()):
            if question.status == "OPEN" and (now() - question.asked_at).total_seconds() >= delay:
                await client.post(
                    f"/api/support/questions/{question.id}/answer",
                    json={"value": question.default},
                    headers=admin,
                )

    # --- the loop ---------------------------------------------------------------------------

    def finished(self) -> bool:
        if len(self.placed) < self.target:
            return False
        orders = [self.store.orders[p.order_id] for p in self.placed if p.order_id]
        if any(o.status not in TERMINAL for o in orders):
            return False
        return not any(d.status in ACTIVE for d in self.store.deliveries.values())

    async def execute(self) -> dict[str, Any]:
        CLOCK.freeze()  # at the wall clock so fresh tokens stay valid; then only ticks move it
        started = now()
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app), base_url="http://bench"
            ) as client:
                sara = await self.login(client, "demo.customer@example.com", PASSWORD)
                admin = await self.login(client, "admin@example.com", PASSWORD)
                drivers = []
                for email, password in DRIVER_ACCOUNTS[: self.scenario.bench.drivers]:
                    headers = await self.login(client, email, password)
                    me = (await client.get("/api/auth/me", headers=headers)).json()
                    drivers.append((headers, me["sub"]))
                while not self.finished() and self.ticks * TICK_SECONDS < MAX_VIRTUAL_SECONDS:
                    simulate_step(self.store, TICK_SECONDS)
                    for ticker in list(self.store.tickers):
                        ticker()
                    expire_due(self.store)
                    await self.customer_tick(client, sara)
                    for headers, user_id in drivers:
                        await self.driver_tick(client, headers, user_id)
                    await self.support_tick(client, admin)
                    CLOCK.advance(TICK_SECONDS)
                    self.ticks += 1
            return self.report(started)
        finally:
            CLOCK.reset()

    # --- measurement ------------------------------------------------------------------------

    def consistent(self, order_id: str) -> tuple[bool, list[str]]:
        store = self.store
        order = store.orders[order_id]
        problems = []
        if order.status not in TERMINAL:
            problems.append(f"order still {order.status}")
        payments = [p for p in store.payments.values() if p.order_id == order_id]
        delivery = next((d for d in store.deliveries.values() if d.order_id == order_id), None)
        if order.status == "DELIVERED":
            if order.total > 0 and not any(p.status == "COMPLETED" for p in payments):
                problems.append("delivered but not paid")
            if delivery is None or delivery.status != "DELIVERED":
                problems.append("delivered without a delivered delivery")
        if order.status in {"CANCELLED", "FAILED"}:
            if (
                any(p.status == "COMPLETED" for p in payments)
                and order.replaced_by_order_id is None
            ):
                problems.append("cancelled but the payment was kept")
            if delivery is not None and delivery.status in ACTIVE:
                problems.append("cancelled with an active delivery")
            if order.credit_applied > 0:
                problems.append("cancelled but the credit was kept")
        if any(q.status == "OPEN" for q in store.questions.values() if q.order_id == order_id):
            problems.append("a question is still open")
        return (not problems, problems)

    def report(self, started: datetime) -> dict[str, Any]:
        store = self.store
        fired: dict[str, list[str]] = {}
        if store.engine is not None:
            for event in store.engine.log:
                if event["event"] == "fired" and event.get("orderId"):
                    fired.setdefault(event["orderId"], []).append(event["failure"])
        rows: list[dict[str, Any]] = []
        for placed in self.placed:
            if not placed.order_id:
                rows.append(
                    {
                        "number": placed.number,
                        "orderId": None,
                        "status": "NOT_PLACED",
                        "notes": placed.notes,
                    }
                )
                continue
            order = store.orders[placed.order_id]
            final = order
            while (
                final.replaced_by_order_id
            ):  # follow a replacement to the order that reached the door
                final = store.orders[final.replaced_by_order_id]
            minutes = (
                (final.delivered_at - placed.placed_at).total_seconds() / 60
                if final.delivered_at
                else None
            )
            ok, problems = self.consistent(order.id)
            if final is not order:
                ok2, problems2 = self.consistent(final.id)
                ok, problems = ok and ok2, problems + problems2
            decisions = store.decisions.get(order.id, []) + (
                store.decisions.get(final.id, []) if final is not order else []
            )
            rows.append(
                {
                    "number": placed.number,
                    "orderId": order.id,
                    "finalOrderId": final.id,
                    "status": final.status,
                    "promisedAt": iso(order.promised_at) if order.promised_at else None,
                    "deliveredAt": iso(final.delivered_at) if final.delivered_at else None,
                    "withinPromise": bool(
                        final.delivered_at
                        and order.promised_at
                        and final.delivered_at <= order.promised_at
                    ),
                    "minutes": round(minutes, 1) if minutes is not None else None,
                    "consistent": ok,
                    "problems": problems,
                    "failures": fired.get(order.id, []),
                    "recovered": bool(fired.get(order.id)) and final.status == "DELIVERED",
                    "decisions": len(decisions),
                    "recoveryDecisions": sum(d["decision"] not in HAPPY_PATH for d in decisions),
                    "humanPrompts": sum(
                        q.order_id in {order.id, final.id} for q in store.questions.values()
                    ),
                    "llmCalls": sum(int(d.get("llmCalls", 0)) for d in decisions),
                    "decisionNames": [d["decision"] for d in decisions],
                    # money is spent only where a model was called; a planner's decisions
                    # carry a plan cost under their own key, not dollars
                    "cost": round(
                        sum((float(d.get("cost", 0)) for d in decisions if d.get("llmCalls")), 0.0),
                        4,
                    ),
                    "notes": placed.notes,
                }
            )
        placed_rows = [r for r in rows if r["orderId"]]
        prices = llm_prices()
        durations: list[float] = [r["minutes"] for r in placed_rows if r["minutes"] is not None]
        unrecovered = sorted(
            {f for r in placed_rows if r["status"] != "DELIVERED" for f in r["failures"]}
        )
        return {
            "scenario": self.scenario.name,
            "engine": self.engine_name,
            "seed": self.seed,
            "foood": __version__,
            "startedAt": iso(started),
            "virtualMinutes": round(self.ticks * TICK_SECONDS / 60, 1),
            "llmPrices": prices,
            "summary": {
                "orders": len(placed_rows),
                "delivered": sum(r["status"] == "DELIVERED" for r in placed_rows),
                "deliveredWithinPromise": sum(r["withinPromise"] for r in placed_rows),
                "cancelled": sum(r["status"] in {"CANCELLED", "FAILED"} for r in placed_rows),
                "consistent": sum(r["consistent"] for r in placed_rows),
                "meanMinutes": round(statistics.fmean(durations), 1) if durations else None,
                "p95Minutes": (
                    round(sorted(durations)[math.ceil(0.95 * len(durations)) - 1], 1)
                    if durations
                    else None
                ),
                "recoveryDecisions": sum(r["recoveryDecisions"] for r in placed_rows),
                "humanPrompts": sum(r["humanPrompts"] for r in placed_rows),
                "llmCalls": sum(r["llmCalls"] for r in placed_rows),
                "cost": round(sum(r["cost"] for r in placed_rows), 4),
                "llmPriced": all(value is not None for value in prices.values()),
                "failuresFired": sorted({f for r in placed_rows for f in r["failures"]}),
                "unrecovered": unrecovered,
            },
            "orders": rows,
        }


def run(
    scenario: str, engine: str = "hand-wired", seed: int = 7, out_dir: Path | None = None
) -> dict[str, Any]:
    """Run one scenario against one engine and write ``<scenario>-<engine>-seed<seed>.json``."""
    result = asyncio.run(Run(load(scenario), engine, seed).execute())
    folder = out_dir or RESULTS
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{result['scenario']}-{engine}-seed{seed}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    result["path"] = str(path)
    return result


def llm_prices() -> dict[str, float | None]:
    """The model prices a run was given (USD per million tokens), so its cost has provenance.

    Set FOOOD_LLM_PRICE_IN and FOOOD_LLM_PRICE_OUT (the bench targets read ``bench.env``);
    without both, nothing is priced and the table says so.
    """

    def read(name: str) -> float | None:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else None

    return {
        "inputPerMillionUsd": read("FOOOD_LLM_PRICE_IN"),
        "outputPerMillionUsd": read("FOOOD_LLM_PRICE_OUT"),
    }


def _label(engine: str, result: dict[str, Any]) -> str:
    model = result.get("model")
    return f"{engine}+{model}" if model else engine


def _histogram(values: Iterable[Any]) -> dict[str, int]:
    counts = Counter(values)
    return {str(value): counts[value] for value in sorted(counts, key=str)}


def _spread(values: list[float]) -> dict[str, float] | None:
    if not values:
        return None
    return {"min": min(values), "median": round(statistics.median(values), 4), "max": max(values)}


#: What a phrasing model changes between otherwise identical runs: tokens and their price. A
#: run's outcomes and decisions are compared without them.
LANGUAGE_FOOTPRINT = {"cost", "llmTokens", "messageCalls", "llmPriced"}


def same_outcomes(summaries: list[dict[str, Any]]) -> bool:
    """True when every summary agrees on everything but the language model's footprint."""
    strip = [{k: v for k, v in s.items() if k not in LANGUAGE_FOOTPRINT} for s in summaries]
    return all(s == strip[0] for s in strip)


def repeat(
    scenario: str,
    engine: str = "hand-wired",
    seed: int = 7,
    times: int = 10,
    out_dir: Path | None = None,
) -> dict[str, Any]:
    """Run the same scenario, engine and seed *times* times: the model is the only variation.

    Writes ``results/repeats/<scenario>-<engine>-seed<seed>-x<times>.json`` with every
    run's summary and the distribution of what differed: outcomes, decision calls, human
    prompts, the recovery decisions taken, model time and cost. An engine without a model
    is the control: its runs must be identical, and ``identical`` says whether they were.
    """
    if times < 1:
        raise ValueError("times must be at least 1")
    runs = [asyncio.run(Run(load(scenario), engine, seed).execute()) for _ in range(times)]
    summaries = [r["summary"] for r in runs]
    taken = Counter(
        name
        for r in runs
        for order in r["orders"]
        for name in order["decisionNames"]
        if name not in HAPPY_PATH
    )
    model_ms = [
        float(r["timing"]["modelMs"])
        for r in runs
        if r.get("timing") and r["timing"].get("modelMs") is not None
    ]
    result: dict[str, Any] = {
        "scenario": runs[0]["scenario"],
        "engine": engine,
        "model": runs[0].get("model"),
        "seed": seed,
        "times": times,
        "foood": __version__,
        "startedAt": runs[0]["startedAt"],
        "distribution": {
            "identical": same_outcomes(summaries),
            "delivered": _histogram(s["delivered"] for s in summaries),
            "deliveredWithinPromise": _histogram(s["deliveredWithinPromise"] for s in summaries),
            "cancelled": _histogram(s["cancelled"] for s in summaries),
            "llmCalls": _histogram(s["llmCalls"] for s in summaries),
            "humanPrompts": _histogram(s["humanPrompts"] for s in summaries),
            "unrecovered": _histogram(", ".join(s["unrecovered"]) or "-" for s in summaries),
            "decisionsTaken": dict(taken.most_common()),
            "modelMs": _spread(model_ms),
            "cost": _spread([float(s["cost"]) for s in summaries]),
            "llmPriced": any(bool(s.get("llmPriced")) for s in summaries),
        },
        "runs": summaries,
    }
    folder = (out_dir or RESULTS) / "repeats"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{result['scenario']}-{_label(engine, runs[0])}-seed{seed}-x{times}.json"
    path.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    result["path"] = str(path)
    return result


__all__ = ["Kitchen", "Run", "repeat", "run", "same_outcomes"]

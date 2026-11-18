"""Out of Pad Thai (issue #45): whoever proposes substitutes, the platform grounds them against
open menus and prices them, and the customer decides."""

from __future__ import annotations

import pytest

from foood.replacements import (
    answer_substitution,
    candidate_pool,
    offer_substitutes,
    substitutes,
)
from tests.support import login


def _item(store, restaurant: str, name: str):
    kitchen = next(r for r in store.restaurants.values() if r.name == restaurant)
    return next(
        i for i in store.menu_items.values() if i.restaurant_id == kitchen.id and i.name == name
    )


async def _sold_out_pasta(client) -> tuple[dict, dict]:
    """Sara orders QA Pasta from QA Kitchen with her address downtown; then it runs out there."""
    sara = await login(client, "demo.customer@example.com")
    restaurants = (await client.get("/api/restaurants?search=QA Kitchen")).json()["items"]
    menu = (await client.get(f"/api/menus/restaurants/{restaurants[0]['id']}/menu")).json()
    pasta = next(i for i in menu["items"] if i["name"] == "QA Pasta")
    await client.post(
        "/api/cart/items", json={"menuItemId": pasta["id"], "quantity": 1}, headers=sara
    )
    created = await client.post(
        "/api/orders",
        json={
            "deliveryAddress": "742 Evergreen Terrace",
            "deliveryLatitude": 37.787,
            "deliveryLongitude": -122.3985,
        },
        headers=sara,
    )
    assert created.status_code == 201, created.text
    _item(client.store, "QA Kitchen", "QA Pasta").available = False
    return sara, created.json()


async def test_the_pool_is_what_open_kitchens_can_make_now(client) -> None:
    sara, order = await _sold_out_pasta(client)
    pool = candidate_pool(client.store, client.store.orders[order["id"]])
    names = {(p["restaurant"], p["item"]) for p in pool}
    assert ("QA Kitchen", "QA Burger") in names  # the same kitchen can offer another dish
    assert ("QA Kitchen", "QA Pasta") not in names  # the sold-out item is not a candidate
    assert ("QA Bistro", "QA Pasta") in names  # another kitchen still makes it
    assert all(p["price"] and p["restaurantId"] for p in pool)
    closed = next(r for r in client.store.restaurants.values() if r.name == "Sakura Sushi")
    closed.status = "CLOSED"
    assert not any(
        p["restaurant"] == "Sakura Sushi"
        for p in candidate_pool(client.store, client.store.orders[order["id"]])
    )


async def test_substitutes_are_grounded_priced_and_ranked(client) -> None:
    sara, placed = await _sold_out_pasta(client)
    order = client.store.orders[placed["id"]]
    options = substitutes(client.store, order, ["QA Burger", "Spaghetti Carbonara"])
    kitchens = [(o.restaurant.name, [i.name for _, i in o.items]) for o in options]
    assert ("QA Kitchen", ["QA Burger"]) in kitchens  # the model's candidate, found and priced
    assert ("QA Bistro", ["QA Pasta"]) in kitchens  # the same item elsewhere counts too
    assert ("Pizza Napoli", ["Spaghetti Carbonara"]) in kitchens
    cheapest = options[0]
    assert cheapest.subtotal <= options[-1].subtotal
    own = next(o for o in options if o.restaurant.name == "QA Kitchen")
    assert own.substitutions == [("QA Pasta", "QA Burger")]
    assert own.to_json(order, client.store.users[order.customer_id])["substitutes"] == [
        {"for": "QA Pasta", "item": "QA Burger"}
    ]
    bistro = next(o for o in options if o.restaurant.name == "QA Bistro")
    assert bistro.substitutions == []


async def test_a_candidate_matches_by_name_or_by_its_words(client) -> None:
    sara, placed = await _sold_out_pasta(client)
    order = client.store.orders[placed["id"]]
    by_word = substitutes(client.store, order, ["burger"])
    assert {o.restaurant.name for o in by_word} >= {
        "QA Kitchen",
        "Burger Palace",
        "Smash Bros Burgers",
    }
    assert all(
        "burger" in item.name.casefold() or item.name == "QA Pasta"
        for o in by_word
        for _, item in o.items
    )


async def test_nothing_off_an_open_menu_is_ever_offered(client) -> None:
    sara, placed = await _sold_out_pasta(client)
    order = client.store.orders[placed["id"]]
    assert substitutes(client.store, order, ["Pad Thai", "Singapore Noodles"]) == [
        o for o in substitutes(client.store, order, []) if not o.substitutions
    ]
    _item(client.store, "QA Kitchen", "QA Burger").available = False
    assert not any(
        o.restaurant.name == "QA Kitchen" for o in substitutes(client.store, order, ["QA Burger"])
    )
    napoli = next(r for r in client.store.restaurants.values() if r.name == "Pizza Napoli")
    napoli.status = "CLOSED"
    assert not any(
        o.restaurant.name == "Pizza Napoli"
        for o in substitutes(client.store, order, ["Spaghetti Carbonara"])
    )


async def test_the_customer_is_offered_and_chooses(client) -> None:
    sara, placed = await _sold_out_pasta(client)
    store = client.store
    order = store.orders[placed["id"]]
    heard: list[tuple] = []
    store.subscribe(
        "order.substitution_offered", lambda order, offer: heard.append(("offered", offer))
    )
    store.subscribe(
        "order.substitution_answered",
        lambda order, offer, alternative: heard.append(("answered", offer, alternative)),
    )
    options = substitutes(store, order, ["QA Burger", "Spaghetti Carbonara"])
    offer = offer_substitutes(store, order, options, "QA Pasta just ran out")
    assert offer.status == "OPEN" and heard[0][0] == "offered"
    mine = (await client.get("/api/notifications", headers=sara)).json()["items"]
    told = next(n for n in mine if n["messageKey"] == "order.substitution_offered")
    assert "QA Pasta just ran out" in told["message"] and told["params"]["orderId"] == order.id

    shown = (await client.get(f"/api/orders/{order.id}/substitution", headers=sara)).json()
    assert shown["status"] == "OPEN" and shown["reason"] == "QA Pasta just ran out"
    assert [o["optionId"] for o in shown["options"]] == list(range(len(options)))
    assert all("difference" in o and "restaurantName" in o for o in shown["options"])

    stranger = await login(client, "driver@example.com")
    assert (
        await client.post(
            f"/api/orders/{order.id}/substitution", json={"accept": 0}, headers=stranger
        )
    ).status_code == 403
    chosen = next(i for i, o in enumerate(options) if o.restaurant.name == "QA Kitchen")
    answered = await client.post(
        f"/api/orders/{order.id}/substitution", json={"accept": chosen}, headers=sara
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["status"] == "ACCEPTED" and answered.json()["choice"] == chosen
    assert heard[-1][0] == "answered" and heard[-1][2].restaurant.name == "QA Kitchen"
    again = await client.post(
        f"/api/orders/{order.id}/substitution", json={"accept": 0}, headers=sara
    )
    assert again.status_code == 409


async def test_the_customer_may_decline(client) -> None:
    sara, placed = await _sold_out_pasta(client)
    store = client.store
    order = store.orders[placed["id"]]
    heard: list = []
    store.subscribe(
        "order.substitution_answered",
        lambda order, offer, alternative: heard.append(alternative),
    )
    offer_substitutes(store, order, substitutes(store, order, ["QA Burger"]), "ran out")
    declined = await client.post(
        f"/api/orders/{order.id}/substitution", json={"decline": True}, headers=sara
    )
    assert declined.status_code == 200 and declined.json()["status"] == "DECLINED"
    assert heard == [None]
    assert (await client.get(f"/api/orders/{order.id}/substitution", headers=sara)).json()[
        "status"
    ] == "DECLINED"


async def test_an_offer_needs_options_and_an_answer_needs_an_offer(client) -> None:
    sara, placed = await _sold_out_pasta(client)
    store = client.store
    order = store.orders[placed["id"]]
    with pytest.raises(ValueError):
        offer_substitutes(store, order, [], "ran out")
    assert (
        await client.get(f"/api/orders/{order.id}/substitution", headers=sara)
    ).status_code == 404
    nothing = await client.post(
        f"/api/orders/{order.id}/substitution", json={"decline": True}, headers=sara
    )
    assert nothing.status_code == 404
    with pytest.raises(ValueError):
        answer_substitution(store, order, 0, by=order.customer_id)


async def test_the_bench_customer_accepts_an_offer_after_a_while() -> None:
    """The headless customer answers like a willing one: the first option, after a short wait,
    so the engines' substitute flows run in the bench the way they would with a person."""
    import httpx

    from foood.bench import Run
    from foood.scenario import load
    from foood.store import CLOCK

    CLOCK.freeze()
    bench = Run(load("happy-path"), "hand-wired", 7)
    store = bench.store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=bench.app), base_url="http://bench"
    ) as client:
        client.store = store  # type: ignore[attr-defined]
        sara = await bench.login(client, "demo.customer@example.com", "password123")
        _, placed = await _sold_out_pasta(client)
        order = store.orders[placed["id"]]
        offer_substitutes(store, order, substitutes(store, order, ["QA Burger"]), "ran out")
        await bench.customer_tick(client, sara)
        assert store.offers[order.id].status == "OPEN"  # too soon: a person takes a moment
        CLOCK.advance(bench.scenario.bench.customer_delay_seconds)
        await bench.customer_tick(client, sara)
        assert store.offers[order.id].status == "ACCEPTED" and store.offers[order.id].choice == 0

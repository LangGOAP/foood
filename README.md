# foood

A food-delivery platform you can break on purpose.

`foood` is a Python web service that implements the public API of
[Delivery Plus](https://github.com/Delivery-Plus0), so its open-source customer,
driver and restaurant apps run against it unchanged. The point is not the
service, it is what happens when a restaurant rejects an order after accepting
it, a driver drops a job mid-route, or a card declines: the platform can fire
those failures on a schedule, and three branches show three ways of handling
them.

| Branch | What drives the order |
|---|---|
| `main` | The web service and the three apps, one command to launch. Hand-written order handling. |
| `langgraph` | The order lifecycle as a LangGraph graph with an LLM at intake. |
| `langgoap` | The same lifecycle as goals and actions, planned by [LangGOAP](https://github.com/LangGOAP/LangGOAP). |

## Run

Prerequisites: [uv](https://docs.astral.sh/uv/) and Node 22 or newer.

```bash
git clone --recurse-submodules https://github.com/LangGOAP/foood
cd foood
make install
make up
```

`make up` (also `uv run foood up`) starts the API on http://localhost:3000 with
the demo seed and serves the three apps as web builds:

| App | URL | Sign in |
|---|---|---|
| customer | http://localhost:8081 | `demo.customer@example.com` / `password123` |
| driver | http://localhost:8082 | `driver@example.com` / `password123` |
| restaurant | http://localhost:8083 | `owner@example.com` / `password123` (Burger Palace) |

The first run installs the apps' dependencies and exports each app with
`expo export --platform web` against the API address, which takes a few
minutes; later runs reuse the builds in `build/web/` until the address changes
(`foood up --rebuild` forces a new export). The restaurant used by the apps'
own end-to-end flows, QA Kitchen, belongs to `qa.restaurant@delivery-plus.test`
with the password `Qa-Only-Passw0rd!`, and `admin@example.com` / `password123`
can refund payments and cancel deliveries.

`make serve` runs the API alone with auto-reload; `make down` stops a
`foood up` left running in another terminal. The apps live in `apps/` as git
submodules of the Delivery Plus repositories and read the API address from
`EXPO_PUBLIC_API_BASE_URL`; the web builds get fixed browser positions so no
app asks for geolocation, and the API's simulated driver phone moves the
driver on the customer's map because a browser window cannot.

Every order placed in the customer app flows through payment, the restaurant
app's kitchen board, dispatch to an online driver, the driver app's pickup and
drop-off, live tracking and notifications. `CONTRACT.md` lists which of the
Delivery Plus API operations are implemented.

## The engine

The platform records facts and publishes them: a payment completed, the kitchen is
ready, a driver dropped the job. What happens next to the order is decided in one
place, the order engine (`foood/engine.py`). `main` ships `hand-wired`, fixed
reactions with one anticipated fallback per failure; the `langgraph` and `langgoap`
branches add theirs and change nothing else. `foood up --engine hand-wired` picks
one, and `GET /api/_engine?orderId=...` lists the decisions taken for an order and
why.

## Support, the human in the loop

The customer app has no approval screen and the apps stay unchanged, so when an
engine needs a person it asks support. http://localhost:3000/support (sign in as
`admin@example.com`) lists the open questions with their options or form, the
recent orders with the decisions taken for each, and the loaded scenario's log.
A question unanswered by its deadline takes the default the engine declared. The
hand-wired engine never asks; the LangGraph and LangGOAP engines do.

## Recovery actions

Engines recover with platform actions that exist whatever the engine. So far:
replacement orders (`foood/replacements.py`): when a kitchen fails, the open
restaurants that carry the same items inside the customer's reach are ranked
with the price difference, and a replacement is created with the payment carried
over, a cheaper kitchen refunding the difference and a dearer one waived; the
customer's substitution policy says whether a dearer kitchen needs support's
approval. Sister restaurants exist for this: QA Bistro next to QA Kitchen (owner
`qa.restaurant3@delivery-plus.test`), Luigi's Pizzeria next to Pizza Napoli and
Smash Bros Burgers next to Burger Palace. Support can do it by hand with
`POST /api/orders/{id}/replacement`. Backup cards: customers may hold more than
one card on file (the demo customer has two), every payment attempt names its
card, and an engine can retry a decline on the next card; `payment_declines`
in a scenario takes `times = 2` to decline both. The hand-wired engine does
neither: it refunds and cancels, as most shops do.

## Break it on purpose

```bash
make up SCENARIO=kitchen-trouble      # or: uv run foood up --scenario kitchen-trouble
```

A scenario is a TOML file in `scenarios/` that can put the kitchen on autopilot
(no one needs to sit in the restaurant app) and inject failures by order
number: the *n*-th order placed after the server starts. Each injection is a
flag on a simulator the platform already has, so with no scenario loaded
nothing behaves differently. The bundled ones:

| Scenario | What happens |
|---|---|
| `happy-path` | The kitchen cooks every order itself; nothing goes wrong. |
| `kitchen-trouble` | Order 1 is rejected, order 2 is accepted then cancelled when an item runs out, order 3 takes four minutes. |
| `delivery-trouble` | Order 1 waits 90 s for a driver, order 2's driver drops it before pickup, order 3's after pickup, order 4's courier crawls. |
| `payment-trouble` | Order 1's card is declined; order 2 is submitted twice and the duplicate is refused. |

The failures available to a scenario file are `restaurant_rejects`,
`accepts_then_cancels`, `prep_overrun`, `payment_declines`, `no_driver`,
`driver_drops` (before or after pickup), `courier_delay` and
`duplicate_submission`; `foood/scenario.py` documents their fields.
`GET /api/_scenario` shows what the loaded scenario has fired so far, and the
tests in `tests/test_scenario.py` prove each failure ends in a consistent
state across the order, its payment and its delivery.

## Credits

The mobile apps and the public API contract are the work of the
[Delivery Plus](https://github.com/Delivery-Plus0) project (MIT). The domain
model takes after Chris Richardson's
[FTGO](https://github.com/microservices-patterns/ftgo-application) (Apache-2.0).

An [Integrallis](https://www.integrallis.com) production. MIT licensed.

# foood

A food-delivery platform with scripted failures, for comparing order engines.

`foood` is a Python web service that implements the public API of [Delivery
Plus](https://github.com/Delivery-Plus0). The project's customer, driver and
restaurant apps run against it. Scenario files inject failures on a schedule: a
restaurant rejects an order after accepting it, a driver drops a job after
pickup, a card declines. Four branches handle them in four ways, each branch
built on the previous one.

| Branch | What drives the order |
|---|---|
| `main` | The web service, the three apps and the hand-wired order engine. |
| `llm` | One frontier model with the platform's tools, called on every order event. |
| `langgraph` | The order lifecycle as a LangGraph graph, with a model per node for intake, lookups, recovery decisions and messages. |
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

`make serve` runs the API alone with auto-reload; `make down` stops a `foood up`
left running in another terminal. The apps live in `apps/` as git submodules
from our forks of upstream `dev` (branch `foood/dev`), which add one change: the
build's `EXPO_PUBLIC_CURRENCY` is the default for amounts whose response carries
no currency. Upstream took the map that opens on the build's fixed location and
formats each amount in the currency its response carries, EGP when none. The
apps read the API address from `EXPO_PUBLIC_API_BASE_URL`. The web builds use
English and dollars and fixed browser positions in San Francisco (the customer
by Union Square, the driver near the kitchens); no app asks for geolocation; the
API's simulated driver phone moves the driver on the customer's map.

Every order placed in the customer app flows through payment, the restaurant
app's kitchen board, dispatch to an online driver, the driver app's pickup and
drop-off, live tracking and notifications, priced in US dollars (the contract
types the currency as a free string; its examples say EGP). `CONTRACT.md` lists which of the
Delivery Plus API operations are implemented.

## The engine

The platform publishes events: a payment completed, the kitchen is ready, a
driver dropped the job. The order engine (`foood/engine.py`) decides what
happens next. `main` ships `hand-wired`, one fixed reaction per failure; the
other branches add their engines and change nothing else. `foood up --engine
<name>` selects one, and `GET /api/_engine?orderId=...` lists the decisions
taken for an order with their reasons.

## One frontier model (the `llm` branch)

`foood up --engine llm` runs the order lifecycle with one model
(`FOOOD_LLM_MODEL`, default `gpt-5.4`; `OPENAI_API_KEY` from the environment or
a `.env` file) in `foood/agent_engine.py`. On every event that concerns an
order, the model receives the event and a set of tools and calls them until it
stops. The tools are the platform's actions (confirm the payment, send the order
out, dispatch, widen the search, replace the order at another kitchen, offer
substitutes and carry out the one the customer accepts, retry the backup card,
issue a credit, move the promise, ask support, wait), lookups (the order's
facts, the kitchens that can make it, the drivers online, the ride's ETA now)
and the world's MCP read tools (`get_weather`, `get_traffic`, `eta`, `forecast`,
`zones`); it has no access to the world's controls. Timers open further
episodes: a delivery nobody takes, a kitchen running long, a promise that
passes, and a change in the world for every order in flight.

Limits: six model rounds and ten tool calls per episode, a timeout and no
retries per call, and the hand-wired reaction when the model errs or exhausts
its budget without acting. Each round is recorded on the order as a decision
call with the model's name, tokens, latency and cost; each tool call is recorded
with what it did. `FOOOD_LLM=off` (or no key) falls back to the hand-wired
engine.

### Ordering in a sentence

```bash
uv run foood ask "two QA burgers and fries to the office, ring twice"
```

`POST /api/orders/intake` (outside the Delivery Plus contract) runs the same
loop with the cart as its tools: the model searches the open menus, fills the
cart from one kitchen and places the order through the app's own endpoint; the
same limits apply. When a dish is unknown or sold out, or the sentence is
ambiguous, it asks one question instead of guessing. The call is recorded on the
order as a language call.

### Messages in the model's words

Every notification keeps its `messageKey` and `params`, which the customer app
localises. With a model available, the server's `message` is written for the
situation (`foood/messages.py`): the kitchen's reason, the card that paid, the
credit added. On an error, an empty or overlong answer, or no model, the
template from `main` is used. Each phrasing call is recorded on the order's
decisions with its tokens.

## The LangGraph engine (the `langgraph` branch)

`foood up --engine langgraph` runs the lifecycle as a LangGraph `StateGraph`,
one thread per order, drawn in
[`foood/langgraph_engine.mmd`](foood/langgraph_engine.mmd). Action nodes
(`confirm`, `dispatch`, `cleanup`) call the same domain functions the hand-wired
engine calls; wait nodes interrupt until the platform publishes the event they
need (a payment outcome, the kitchen's status, a driver, a pickup, the
drop-off), and the engine resumes them with the event. Every failure routes to
`recover`. A SQLite checkpointer (`FOOOD_CHECKPOINTS`,
`build/langgraph-checkpoints.sqlite` under `foood up`) keeps every thread; a new
engine process resumes each order where it paused.

At `recover`, a model picks one of the platform's recovery tools: a replacement
order from another kitchen, the backup card, a credit, a wider dispatch,
waiting, a question to support (who picks the tool), or refund and cancel.
Limits: an allow-list of tools per failure (`foood/recovery.py`), a budget of
three recoveries per order, a timeout and no retries per call, structured
output, and the hand-wired reaction when the model errs, picks a tool outside
the list, or exhausts the budget. Every call is recorded on the order's
decisions with its tokens. The model is `FOOOD_LLM_MODEL` (default
`gpt-5.4-mini`) with `OPENAI_API_KEY` from the environment or a `.env` file;
`FOOOD_LLM=off` disables it and `recover` applies the hand-wired reaction. Set
`FOOOD_LLM_PRICE_IN` and `FOOOD_LLM_PRICE_OUT` (USD per million tokens) to price
the costs; otherwise only tokens are reported.

### A model per node

`models.toml` assigns a model to each node: a small model for intake
(extraction), the cheapest that writes well for the messages, a small
tool-calling model for the world lookups, a classifier for a kitchen's free-text
reason, a mid model with culinary knowledge for substitutes, a mid model for the
judgement at `recover`, and the frontier model for one call only, when
`decide`'s answer is unusable (an error, or a tool outside the allow-list); the
hand-wired reaction remains the fallback after that. Each node has its own
timeout and no retries. `FOOOD_MODELS` points at another file,
`FOOOD_MODEL_<NODE>` sets one node, `FOOOD_MODEL_ALL` sets every node (the
bench's model matrix), and `off` switches a node's model off so its
deterministic fallback applies. Every call is recorded on the order with its
node, its model, tokens, latency and cost at that model's price (`bench.env`),
and the results files sum them per node.

### Look up, then decide

Every failure goes through `lookup` before `recover`. The lookup node gathers
the world's facts for the fork in code, through the same MCP tools a model would
call: the weather in the kitchen's zone, the ride from the kitchen to the door
now against a clear day and the promise, closed corridors, what is scheduled for
the next half hour. A small tool-calling model (the roster's `lookup`) may look
further with the same tools, two rounds at most, and its calls are counted as
perception, apart from the decision. The decision node receives facts and no
tools. The allow-list depends on the conditions: `replace_order` is withheld
when no alternative kitchen is reachable, and in rain or a storm a wider driver
search comes first.

### A model proposes, the platform grounds, the customer decides

Two more small-model nodes sit between `lookup` and `recover` for the kitchen's
failures. `classify` reads the kitchen's free-text note into a kind ("the last
portion just went out", "the grill is down"). When a dish ran out and the
kitchen is fine, `suggest` proposes substitutes: a mid-size model picks up to
five dishes from what open kitchens within reach can make now, the platform
keeps the dishes that are on an open menu and prices each as a replacement
(`foood/replacements.py`), and the customer is asked. The thread waits at
`wait_customer`; an accepted option becomes the replacement order, a declined
one (or three minutes of silence) sends the fork its decision with that fact.
Both nodes are perception calls, counted apart from the decision, and without
them the fork behaves as before.

### Ordering in a sentence

```bash
uv run foood ask "two QA burgers and fries to the office, ring twice"
```

`POST /api/orders/intake` (outside the Delivery Plus contract) runs a small
LangGraph: a model reads the sentence against the open kitchens' menus into a
structured request, a deterministic step matches it to real menu items and turns
anything unclear into a question instead of an order (several kitchens could
make it, a dish nobody has, a sold-out item), and the order is placed through
the app's own endpoint; the same limits apply. The order appears in the customer
app like any other, with the intake call's tokens on its decisions.

## The LangGOAP engine (this branch)

`foood up --engine langgoap` runs the lifecycle as a goal and a set of actions
(`foood/goap_engine.py`), one plan per customer order that follows a replacement
to the door. A sensor derives the world from the platform before every plan:
paid or declined, the kitchen cooking, late, ready or failed, a driver being
searched for, assigned, the order picked up, lost or delivered, whether another
kitchen can make it, whether that needs approval, whether the promise is
breached and whether the customer was compensated. The actions are the same
recovery tools the other engines have, with preconditions, effects and costs:
the waits (payment, kitchen, driver, pickup, drop-off) pause until a platform
event moves one of their facts, as the sensor reports; a replacement order costs
its price difference, prep time and distance; widening the search is cheaper
than waiting longer; accepting a late kitchen costs more; a credit settles the
compensation the goal demands once the promise is breached. The goal is
`delivered` and `compensated_if_late`. When what a wait observes is not what the
plan expected, the planner re-plans from the real state; when no route to the
goal exists, the engine refunds and cancels and records the reason. Only the
substitution approval asks a person, as a typed form through support. The
planner makes no model calls for decisions; the drawing is in
[`foood/langgoap_engine.mmd`](foood/langgoap_engine.mmd) and the checkpointer
resumes every thread after a restart. In the `langgoap` rows of
[`BENCH.md`](BENCH.md), decision calls are zero; the other call columns count
the model's language and perception calls.

### The planner reads the world

The sensor reads the world through the same MCP tools the other engines use,
once per tick, counted against the order: the kitchen's and the door's zone,
rain or a storm, a slow or closed route, the ride's minutes now, whether the
promise is at risk (a courier leaving now could not make it) and whether the
driver pool is thin. These enter as facts and costs: a replacement's cost
includes the traffic on the new route; widening the driver search is the
cheapest action in rain or with a thin pool; waiting for a driver or accepting a
slow kitchen costs more once the promise is at risk; a credit for weather-caused
lateness is cheap. Every wait watches `promise_at_risk` with its own facts, and
a `world.changed` event wakes every waiting thread; a storm or a closed corridor
triggers a re-plan. Decision calls remain zero; world calls are counted in the
results.

### Planning ahead, and the model as a sensor

The goal is `delivered`, `compensated_if_late` and `promise_at_risk: False`. The
actions for it: `pre_dispatch` sends a courier to the kitchen before the food is
ready when the ride, not the kitchen, is the delay; `switch_kitchen_early` moves
the order to a kitchen that would have it at the door sooner, by prep time and
the ride from it now; `reroute` tells a courier already on the road the way
round a closure; and `accept_late_risk`, the most expensive, carries on when
nothing better exists and leaves the credit to settle it.

Inside the planner, models are used for perception only. The roster's `classify`
model reads a kitchen's free-text note into the fact `reason_kind` (a dish ran
out, or the kitchen is down), once per order, recorded with the model that read
it. When a dish ran out and no other kitchen makes the same food, the `suggest`
model proposes substitutes from what open kitchens can make, the platform keeps
those on a menu and prices them; `offer_substitutes` puts them to the customer
and waits (three minutes of silence counts as a decline);
`replace_with_substitute` carries out the accepted one. Both calls are counted
as perception; decision calls remain zero. `offer_pickup` (the customer
collecting the food when no courier can come) needs a pickup flow the platform
does not have and is not implemented.

## Support console

The customer app has no approval screen; an engine that needs a person asks
support. http://localhost:3000/support (sign in as `admin@example.com`) lists
the open questions with their options or form, the recent orders with the
decisions taken for each, and the loaded scenario's log. A question unanswered
by its deadline takes the default the engine declared. The hand-wired engine
never asks; the LangGraph and LangGOAP engines do. Each order also carries its
plan panel: the current route with the step it waits at, its cost, how often and
why it was re-planned, the kitchens the planner weighed with their price
difference, and, for the LangGraph engine, the model's last reason.

http://localhost:3000/map shows the evening on one map: the kitchens, every
order's door with the line from its kitchen coloured by its status, and the
drivers where their phones last reported, redrawn every two seconds from `GET
/api/_map` (not part of the contract, like `/api/_engine`).

The world (`foood/world.py`) holds the weather, the traffic and the road
closures of a city cut into zones around the kitchens, with a corridor between
each pair of zones that matters. Couriers slow down in rain and in traffic,
kitchens take longer in a storm, a closed corridor turns a route into a detour.
It is served as an MCP server at http://localhost:3000/mcp (streamable HTTP);
any MCP client can read it (`get_weather`, `get_traffic`, `eta`, `forecast`,
`zones`) or move it (`set_weather`, `set_traffic`, `close_road`, `schedule`);
the engines consult the same tools, and a scenario moves the same state on
schedule. The page at http://localhost:3000/world shows it and lets support move
it by hand.

## Recovery actions

Recovery actions are platform functions available to every engine. Replacement
orders (`foood/replacements.py`): when a kitchen fails, the open restaurants
that carry the same items inside the customer's reach are ranked with the price
difference, and a replacement is created with the payment carried over, a
cheaper kitchen refunding the difference and a dearer one waived; the customer's
substitution policy says whether a dearer kitchen needs support's approval.
Sister restaurants exist for this: QA Bistro next to QA Kitchen (owner
`qa.restaurant3@delivery-plus.test`), Luigi's Pizzeria next to Pizza Napoli and
Smash Bros Burgers next to Burger Palace. Support can do it by hand with `POST
/api/orders/{id}/replacement`. Backup cards: customers may hold more than one
card on file (the demo customer has two), every payment attempt names its card,
and an engine can retry a decline on the next card; `payment_declines` in a
scenario takes `times = 2` to decline both. Credits: a ledger per customer that
engines or support fill as compensation (`POST /api/credits`, one credit per
order); the next quote and order apply the balance, the payment charges the
rest, and a cancelled order gives its credit back (`GET /api/credits/me`).
Dispatch strategies (`foood/dispatch.py`): longest idle, nearest first by last
known position, and stacking a ready order onto a busy driver whose drop-off is
near the restaurant so it becomes their next job; a waiting delivery shows how
long it has searched. The hand-wired engine dispatches longest-idle and
otherwise refunds and cancels.

Substitutes (`candidate_pool`, `substitutes`, `offer_substitutes` in the same
module): when the item itself is gone, a model or a person proposes candidate
dishes by name; the platform keeps only what an open kitchen within reach can
make now (the order's own kitchen included, which spares a new delivery), prices
each option as a replacement, and puts them to the customer, who accepts one or
declines at `POST /api/orders/{id}/substitution`. The hand-wired engine does not
substitute; it cancels.

## Break it on purpose

```bash
make up SCENARIO=kitchen-trouble ENGINE=hand-wired   # or: uv run foood up --scenario kitchen-trouble --engine hand-wired
```

A scenario is a TOML file in `scenarios/` that can put the kitchen on autopilot
(no one needs to sit in the restaurant app) and inject failures by order number:
the *n*-th order placed after the server starts. Each injection is a flag on a
simulator the platform already has; with no scenario loaded nothing behaves
differently. The bundled ones:

| Scenario | What happens |
|---|---|
| `happy-path` | The kitchen cooks every order itself; nothing goes wrong. |
| `kitchen-trouble` | Order 1 is rejected, order 2 is accepted then cancelled when an item runs out, order 3 is ready twenty-five minutes after the kitchen started, past its promise. |
| `delivery-trouble` | Order 1 waits 90 s for a driver, order 2's driver drops it before pickup, order 3's after pickup, order 4's courier crawls. |
| `payment-trouble` | Order 1's card is declined; order 2 is submitted twice and the duplicate is refused; order 3 meets a minute of payment gateway outage. |
| `storm` | A storm over the kitchens as order 1 is confirmed: kitchens slower, couriers slower, a driver offline for five minutes; order 3 meets heavy traffic downtown. Nothing fails; the promises do. |
| `bridge-closed` | Market Street closes between the kitchens and the door a minute after order 1 for three quarters of an hour, the streets around it jammed: orders 1 and 2 ride the whole detour with promises made for the direct road; order 3 rides out of it as it reopens. |

The failures available to a scenario file are `restaurant_rejects`,
`accepts_then_cancels`, `prep_overrun`, `payment_declines`, `no_driver`,
`driver_drops` (before or after pickup), `courier_delay`,
`duplicate_submission`, `gateway_down`, and the world's `storm`, `traffic`,
`road_closed` and `driver_offline`; `foood/scenario.py` documents their fields.
`GET /api/_scenario` shows what the loaded scenario has fired so far, and the
tests in `tests/test_scenario.py` check that each failure ends in a consistent
state across the order, its payment and its delivery.

## Measure it

```bash
make bench                      # every bundled scenario against ENGINE (hand-wired) under SEED (7)
uv run foood bench --scenario delivery-trouble --engine hand-wired --seed 7
```

The bench runs a scenario with nobody at the apps: autopilots place and pay the
orders, drive the deliveries when the simulated phone arrives, answer support's
questions with the recommended option after a while, and accept the first
substitute offered after a moment's thought, on a frozen clock advanced tick by
tick. Each run writes `results/<scenario>-<engine>-seed<n>.json` with, per
order, the final status, whether it arrived within its promise, the minutes it
took, whether its state is consistent, the failures fired against it, the
engine's recovery decisions, human prompts, LLM calls and cost, and how long the
engine took to decide: the wall-clock of every planning round, the round trip of
every model call, re-plans and plan steps, plus the real time the whole run took
and the machine it ran on. [`BENCH.md`](BENCH.md) and the table below are
generated from those files by `make bench-table` and CI fails when either is
stale. The hand-wired rows are the baseline; their "Not recovered" column lists
the failures the engine does not recover from. `make bench-all` runs every
bundled scenario, including `incident` (seven orders, a failure in each),
against every engine under seeds 7, 11 and 13; an engine that calls a model is
labelled with the model and shown per seed.

`make bench-all` also runs the incident evening ten times per engine under one
seed; the model matrix (the langgraph engine's incident evening with one model
in every node, `FOOOD_MODEL_ALL`, for gpt-5.4-nano, gpt-5.4-mini and gpt-5.4);
and the substitution fixture (`tests/fixtures/substitutes.toml`): out-of-stock
cases with the dishes a person would accept, each answerable from the pool. The
model's proposals are grounded in order, and a case passes when the first that
is on an open menu within reach is an acceptable dish; the same dish at another
kitchen counts. An order the engine leaves mid-flight with no failure to blame
shows in the tables as `stalled <status>`, and a delivery is judged against the
promise the customer was first given, however often an engine moved it (the
moves are counted in "Promises moved"). `BENCH.md` carries all four tables.

<!-- bench:start -->
| Scenario | Engine | Seed | Orders | Delivered | Within promise | Promises moved | Cancelled | Consistent | Mean min | p95 min | Recovery decisions | Human prompts | Fallbacks | LLM calls, decisions | LLM calls, perception | LLM calls, language | World calls | LLM tokens | Cost (USD) | Planner ms | Model ms, decisions | Re-plans | Plan steps | Wall s | Not recovered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bridge-closed | langgraph | 7 | 3 | 3 | 0 | 0 | 0 | 3/3 | 33.1 | 34.9 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 3227 | 0.0011 | - | - | - | - | - | - |
| bridge-closed | langgraph | 11 | 3 | 3 | 0 | 0 | 0 | 3/3 | 33.1 | 34.9 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 3103 | 0.001 | - | - | - | - | - | - |
| bridge-closed | langgraph | 13 | 3 | 3 | 0 | 0 | 0 | 3/3 | 33.1 | 34.9 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 3252 | 0.0012 | - | - | - | - | - | - |
| bridge-closed | llm + gpt-5.4 | 7 | 3 | 3 | 3 | 0 | 0 | 3/3 | 33.9 | 37.4 | 10 | 0 | 0 | 38 | 0 | 23 | 13 | 56137 | 0.167 | - | - | - | - | - | - |
| bridge-closed | llm + gpt-5.4 | 11 | 3 | 3 | 1 | 0 | 0 | 3/3 | 33.9 | 37.4 | 9 | 0 | 0 | 41 | 0 | 19 | 14 | 59494 | 0.1733 | - | - | - | - | - | - |
| bridge-closed | llm + gpt-5.4 | 13 | 3 | 3 | 1 | 0 | 0 | 3/3 | 33.9 | 37.4 | 9 | 0 | 0 | 41 | 0 | 18 | 11 | 58607 | 0.1689 | - | - | - | - | - | - |
| delivery-trouble | hand-wired | 7 | 4 | 3 | 2 | 0 | 1 | 4/4 | 16.7 | 25.0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 5850 | 0.0067 | 0 | 0.0 | 0 | - | 18.8 | driver_drops |
| delivery-trouble | hand-wired | 11 | 4 | 3 | 2 | 0 | 1 | 4/4 | 16.7 | 25.0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 5766 | 0.0066 | 0 | 0.0 | 0 | - | 18.6 | driver_drops |
| delivery-trouble | hand-wired | 13 | 4 | 3 | 2 | 0 | 1 | 4/4 | 16.7 | 25.0 | 3 | 0 | 0 | 0 | 0 | 0 | 0 | 5769 | 0.0068 | 0 | 0.0 | 0 | - | 18.1 | driver_drops |
| delivery-trouble | langgoap | 7 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 7063 | 0.0081 | 2.8 | 0.0 | 2 | 6.0 | 23.8 | - |
| delivery-trouble | langgoap | 11 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 7028 | 0.0081 | 2.7 | 0.0 | 2 | 6.0 | 23.8 | - |
| delivery-trouble | langgoap | 13 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 4 | 0 | 0 | 0 | 0 | 0 | 0 | 7073 | 0.0081 | 2.9 | 0.0 | 2 | 6.0 | 22.8 | - |
| delivery-trouble | langgraph | 11 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 6 | 0 | 0 | 2 | 4 | 26 | 11 | 9375 | 0.0043 | - | - | - | - | - | - |
| delivery-trouble | langgraph | 13 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 6 | 0 | 0 | 2 | 4 | 26 | 15 | 10151 | 0.0047 | - | - | - | - | - | - |
| delivery-trouble | langgraph + gpt-5.4-mini | 7 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 4 | 0 | 0 | 2 | 0 | 0 | 0 | 8242 | 0.0097 | 0 | 2901.1 | 0 | - | 25.1 | - |
| delivery-trouble | langgraph + gpt-5.4-mini | 11 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 4 | 0 | 0 | 2 | 0 | 0 | 0 | 8268 | 0.0095 | 0 | 2589.6 | 0 | - | 24.6 | - |
| delivery-trouble | langgraph + gpt-5.4-mini | 13 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 4 | 0 | 0 | 2 | 0 | 0 | 0 | 8222 | 0.0096 | 0 | 3094.0 | 0 | - | 24.5 | - |
| delivery-trouble | llm + gpt-5.4 | 7 | 4 | 3 | 2 | 0 | 0 | 3/4 | 20.1 | 37.0 | 5 | 0 | 0 | 31 | 0 | 21 | 6 | 43021 | 0.1236 | - | - | - | - | - | driver_drops |
| delivery-trouble | llm + gpt-5.4 | 11 | 4 | 3 | 2 | 0 | 0 | 3/4 | 18.0 | 30.9 | 8 | 0 | 0 | 38 | 0 | 22 | 6 | 53764 | 0.1524 | - | - | - | - | - | driver_drops |
| delivery-trouble | llm + gpt-5.4 | 13 | 4 | 3 | 2 | 0 | 0 | 3/4 | 20.1 | 37.0 | 6 | 1 | 0 | 34 | 0 | 20 | 6 | 46802 | 0.134 | - | - | - | - | - | driver_drops |
| happy-path | hand-wired | 7 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2628 | 0.003 | 0 | 0.0 | 0 | - | 11.3 | - |
| happy-path | hand-wired | 11 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2624 | 0.0031 | 0 | 0.0 | 0 | - | 8.4 | - |
| happy-path | hand-wired | 13 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2618 | 0.003 | 0 | 0.0 | 0 | - | 8.7 | - |
| happy-path | langgoap | 7 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2602 | 0.0029 | 1.4 | 0.0 | 0 | 6.0 | 8.4 | - |
| happy-path | langgoap | 11 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2578 | 0.003 | 1.3 | 0.0 | 0 | 6.0 | 9.3 | - |
| happy-path | langgoap | 13 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2640 | 0.0031 | 1.2 | 0.0 | 0 | 6.0 | 9.9 | - |
| happy-path | langgraph | 11 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 2104 | 0.0007 | - | - | - | - | - | - |
| happy-path | langgraph | 13 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 2092 | 0.0008 | - | - | - | - | - | - |
| happy-path | langgraph + gpt-5.4-mini | 7 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2615 | 0.003 | 0 | 0.0 | 0 | - | 8.1 | - |
| happy-path | langgraph + gpt-5.4-mini | 11 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2603 | 0.003 | 0 | 0.0 | 0 | - | 8.6 | - |
| happy-path | langgraph + gpt-5.4-mini | 13 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 2600 | 0.003 | 0 | 0.0 | 0 | - | 8.3 | - |
| happy-path | llm + gpt-5.4 | 7 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 8 | 0 | 10 | 0 | 11449 | 0.0327 | - | - | - | - | - | - |
| happy-path | llm + gpt-5.4 | 11 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 8 | 0 | 10 | 0 | 11396 | 0.0326 | - | - | - | - | - | - |
| happy-path | llm + gpt-5.4 | 13 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 8 | 0 | 10 | 0 | 11378 | 0.0324 | - | - | - | - | - | - |
| incident | hand-wired | 7 | 7 | 2 | 2 | 0 | 5 | 7/7 | 15.8 | 15.8 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6668 | 0.0078 | 0 | 0.0 | 0 | - | 21.1 | accepts_then_cancels, driver_drops, gateway_down, payment_declines, restaurant_rejects |
| incident | hand-wired | 11 | 7 | 2 | 2 | 0 | 5 | 7/7 | 15.8 | 15.8 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6583 | 0.0076 | 0 | 0.0 | 0 | - | 22.3 | accepts_then_cancels, driver_drops, gateway_down, payment_declines, restaurant_rejects |
| incident | hand-wired | 13 | 7 | 2 | 2 | 0 | 5 | 7/7 | 15.8 | 15.8 | 6 | 0 | 0 | 0 | 0 | 0 | 0 | 6675 | 0.0077 | 0 | 0.0 | 0 | - | 22.4 | accepts_then_cancels, driver_drops, gateway_down, payment_declines, restaurant_rejects |
| incident | langgoap | 7 | 7 | 7 | 4 | 0 | 0 | 7/7 | 24.1 | 42.8 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 12901 | 0.0149 | 9.8 | 0.0 | 9 | 6.0 | 43.3 | - |
| incident | langgoap | 11 | 7 | 7 | 4 | 0 | 0 | 7/7 | 24.1 | 42.8 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 12951 | 0.0149 | 10.0 | 0.0 | 9 | 6.0 | 42.1 | - |
| incident | langgoap | 13 | 7 | 7 | 4 | 0 | 0 | 7/7 | 24.1 | 42.8 | 15 | 0 | 0 | 0 | 0 | 0 | 0 | 12914 | 0.0148 | 8.7 | 0.0 | 9 | 6.0 | 44.7 | - |
| incident | langgraph + gpt-5.4-mini | 7 | 7 | 7 | 4 | 0 | 0 | 7/7 | 24.1 | 42.8 | 9 | 0 | 0 | 5 | 0 | 0 | 0 | 15875 | 0.0185 | 0 | 6497.0 | 0 | - | 49.3 | - |
| incident | langgraph + gpt-5.4-mini | 11 | 7 | 7 | 4 | 0 | 0 | 7/7 | 24.1 | 42.8 | 9 | 0 | 0 | 5 | 0 | 0 | 0 | 15718 | 0.0181 | 0 | 6512.5 | 0 | - | 47.0 | - |
| incident | langgraph + gpt-5.4-mini | 13 | 7 | 7 | 4 | 0 | 0 | 7/7 | 24.1 | 42.8 | 9 | 0 | 0 | 5 | 0 | 0 | 0 | 15814 | 0.0183 | 0 | 5940.9 | 0 | - | 48.7 | - |
| kitchen-trouble | hand-wired | 7 | 3 | 1 | 0 | 0 | 2 | 3/3 | 36.1 | 36.1 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 3496 | 0.0041 | 0 | 0.0 | 0 | - | 10.2 | accepts_then_cancels, restaurant_rejects |
| kitchen-trouble | hand-wired | 11 | 3 | 1 | 0 | 0 | 2 | 3/3 | 36.1 | 36.1 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 3554 | 0.0042 | 0 | 0.0 | 0 | - | 10.4 | accepts_then_cancels, restaurant_rejects |
| kitchen-trouble | hand-wired | 13 | 3 | 1 | 0 | 0 | 2 | 3/3 | 36.1 | 36.1 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 3545 | 0.0041 | 0 | 0.0 | 0 | - | 11.1 | accepts_then_cancels, restaurant_rejects |
| kitchen-trouble | langgoap | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.7 | 37.8 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 5856 | 0.0068 | 4.4 | 0.0 | 3 | 6.0 | 20.3 | - |
| kitchen-trouble | langgoap | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.7 | 37.8 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 5892 | 0.0069 | 6.0 | 0.0 | 3 | 6.0 | 20.7 | - |
| kitchen-trouble | langgoap | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.7 | 37.8 | 7 | 0 | 0 | 0 | 0 | 0 | 0 | 5919 | 0.0069 | 4.3 | 0.0 | 3 | 6.0 | 19.5 | - |
| kitchen-trouble | langgraph | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.4 | 35.8 | 13 | 0 | 0 | 2 | 9 | 25 | 20 | 11975 | 0.0058 | - | - | - | - | - | - |
| kitchen-trouble | langgraph | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.4 | 35.8 | 13 | 0 | 0 | 2 | 9 | 25 | 20 | 12194 | 0.0056 | - | - | - | - | - | - |
| kitchen-trouble | langgraph + gpt-5.4-mini | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.1 | 35.8 | 6 | 0 | 0 | 3 | 0 | 0 | 0 | 8511 | 0.0098 | 0 | 3993.0 | 0 | - | 25.7 | - |
| kitchen-trouble | langgraph + gpt-5.4-mini | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.1 | 35.8 | 6 | 0 | 0 | 3 | 0 | 0 | 0 | 8559 | 0.0099 | 0 | 4173.2 | 0 | - | 25.6 | - |
| kitchen-trouble | langgraph + gpt-5.4-mini | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.1 | 35.8 | 6 | 0 | 0 | 3 | 0 | 0 | 0 | 8565 | 0.0098 | 0 | 4030.4 | 0 | - | 25.2 | - |
| kitchen-trouble | llm + gpt-5.4 | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 17.4 | 27.8 | 3 | 0 | 0 | 21 | 0 | 22 | 3 | 31122 | 0.0916 | - | - | - | - | - | - |
| kitchen-trouble | llm + gpt-5.4 | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 17.4 | 27.8 | 3 | 0 | 0 | 21 | 0 | 22 | 1 | 31164 | 0.0914 | - | - | - | - | - | - |
| kitchen-trouble | llm + gpt-5.4 | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 17.4 | 27.8 | 3 | 0 | 0 | 22 | 0 | 22 | 3 | 32615 | 0.0961 | - | - | - | - | - | - |
| payment-trouble | hand-wired | 7 | 3 | 1 | 1 | 0 | 2 | 3/3 | 12.1 | 12.1 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 1584 | 0.0019 | 0 | 0.0 | 0 | - | 5.4 | gateway_down, payment_declines |
| payment-trouble | hand-wired | 11 | 3 | 1 | 1 | 0 | 2 | 3/3 | 12.1 | 12.1 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 1545 | 0.0018 | 0 | 0.0 | 0 | - | 4.8 | gateway_down, payment_declines |
| payment-trouble | hand-wired | 13 | 3 | 1 | 1 | 0 | 2 | 3/3 | 12.1 | 12.1 | 2 | 0 | 0 | 0 | 0 | 0 | 0 | 1588 | 0.0019 | 0 | 0.0 | 0 | - | 5.0 | gateway_down, payment_declines |
| payment-trouble | langgoap | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 4537 | 0.0052 | 2.9 | 0.0 | 3 | 6.0 | 14.5 | - |
| payment-trouble | langgoap | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 4474 | 0.0052 | 3.2 | 0.0 | 3 | 6.0 | 14.7 | - |
| payment-trouble | langgoap | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 5 | 0 | 0 | 0 | 0 | 0 | 0 | 4440 | 0.0052 | 3.4 | 0.0 | 3 | 6.0 | 15.6 | - |
| payment-trouble | langgraph | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 5 | 0 | 0 | 2 | 4 | 16 | 12 | 7435 | 0.0037 | - | - | - | - | - | - |
| payment-trouble | langgraph | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 5 | 0 | 0 | 2 | 4 | 16 | 16 | 7963 | 0.0041 | - | - | - | - | - | - |
| payment-trouble | langgraph + gpt-5.4-mini | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 3 | 0 | 0 | 2 | 0 | 0 | 0 | 5485 | 0.0063 | 0 | 2655.3 | 0 | - | 16.4 | - |
| payment-trouble | langgraph + gpt-5.4-mini | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 3 | 0 | 0 | 2 | 0 | 0 | 0 | 5561 | 0.0065 | 0 | 2673.4 | 0 | - | 16.8 | - |
| payment-trouble | langgraph + gpt-5.4-mini | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 3 | 0 | 0 | 2 | 0 | 0 | 0 | 5565 | 0.0065 | 0 | 2397.0 | 0 | - | 17.4 | - |
| payment-trouble | llm + gpt-5.4 | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.7 | 34.8 | 14 | 0 | 0 | 42 | 0 | 16 | 1 | 53840 | 0.1523 | - | - | - | - | - | - |
| payment-trouble | llm + gpt-5.4 | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.7 | 34.8 | 14 | 0 | 0 | 44 | 0 | 16 | 1 | 56162 | 0.1591 | - | - | - | - | - | - |
| payment-trouble | llm + gpt-5.4 | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 16 | 0 | 0 | 49 | 0 | 17 | 4 | 63451 | 0.1796 | - | - | - | - | - | - |
| storm | langgraph | 7 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 5325 | 0.0019 | - | - | - | - | - | - |
| storm | langgraph | 11 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 5235 | 0.0018 | - | - | - | - | - | - |
| storm | langgraph | 13 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 5308 | 0.0017 | - | - | - | - | - | - |
| storm | llm + gpt-5.4 | 7 | 5 | 5 | 3 | 0 | 0 | 5/5 | 24.1 | 34.6 | 16 | 0 | 0 | 66 | 0 | 28 | 20 | 91990 | 0.2657 | - | - | - | - | - | - |
| storm | llm + gpt-5.4 | 11 | 5 | 5 | 3 | 0 | 0 | 5/5 | 24.1 | 34.6 | 17 | 0 | 0 | 66 | 0 | 30 | 20 | 99757 | 0.2852 | - | - | - | - | - | - |
| storm | llm + gpt-5.4 | 13 | 5 | 3 | 2 | 0 | 0 | 3/5 | 24.2 | 34.6 | 20 | 0 | 0 | 71 | 0 | 26 | 20 | 98372 | 0.2828 | - | - | - | - | - | driver_offline, stalled READY_FOR_PICKUP, storm |
<!-- bench:end -->

## Credits

The mobile apps and the public API contract are the work of the
[Delivery Plus](https://github.com/Delivery-Plus0) project (MIT). The domain
model takes after Chris Richardson's
[FTGO](https://github.com/microservices-patterns/ftgo-application) (Apache-2.0).

An [Integrallis](https://www.integrallis.com) production. MIT licensed.

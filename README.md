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
| bridge-closed | hand-wired | 7 | 3 | 3 | 0 | 0 | 0 | 3/3 | 33.1 | 34.9 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 3173 | 0.0011 | 0 | 0.0 | 0 | - | 17.0 | - |
| bridge-closed | hand-wired | 11 | 3 | 3 | 0 | 0 | 0 | 3/3 | 33.1 | 34.9 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 3163 | 0.0012 | 0 | 0.0 | 0 | - | 18.0 | - |
| bridge-closed | hand-wired | 13 | 3 | 3 | 0 | 0 | 0 | 3/3 | 33.1 | 34.9 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 3178 | 0.0012 | 0 | 0.0 | 0 | - | 17.3 | - |
| bridge-closed | langgoap | 7 | 3 | 3 | 0 | 0 | 0 | 3/3 | 28.5 | 31.4 | 10 | 0 | 0 | 0 | 0 | 18 | 80 | 3838 | 0.0012 | 5.6 | 0.0 | 4 | 5.3 | 18.4 | - |
| bridge-closed | langgoap | 11 | 3 | 3 | 0 | 0 | 0 | 3/3 | 28.5 | 31.4 | 10 | 0 | 0 | 0 | 0 | 18 | 80 | 3841 | 0.0012 | 6.1 | 0.0 | 4 | 5.3 | 17.6 | - |
| bridge-closed | langgoap | 13 | 3 | 3 | 0 | 0 | 0 | 3/3 | 28.5 | 31.4 | 10 | 0 | 0 | 0 | 0 | 18 | 80 | 3843 | 0.0012 | 6.2 | 0.0 | 4 | 5.3 | 17.3 | - |
| bridge-closed | langgraph | 7 | 3 | 3 | 0 | 0 | 0 | 3/3 | 33.1 | 34.9 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 3201 | 0.0012 | 0 | 0.0 | 0 | - | 17.9 | - |
| bridge-closed | langgraph | 11 | 3 | 3 | 0 | 0 | 0 | 3/3 | 33.1 | 34.9 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 3132 | 0.0011 | 0 | 0.0 | 0 | - | 16.8 | - |
| bridge-closed | langgraph | 13 | 3 | 3 | 0 | 0 | 0 | 3/3 | 33.1 | 34.9 | 0 | 0 | 0 | 0 | 0 | 15 | 0 | 3236 | 0.0012 | 0 | 0.0 | 0 | - | 17.9 | - |
| bridge-closed | llm + gpt-5.4 | 7 | 3 | 3 | 0 | 2 | 0 | 3/3 | 33.9 | 37.4 | 9 | 0 | 0 | 41 | 0 | 19 | 12 | 60352 | 0.1591 | 0 | 62370.0 | 0 | - | 84.4 | - |
| bridge-closed | llm + gpt-5.4 | 11 | 3 | 3 | 0 | 3 | 0 | 3/3 | 33.9 | 37.4 | 8 | 0 | 0 | 37 | 0 | 20 | 13 | 52958 | 0.1384 | 0 | 57444.0 | 0 | - | 81.0 | - |
| bridge-closed | llm + gpt-5.4 | 13 | 3 | 3 | 0 | 3 | 0 | 3/3 | 34.3 | 38.5 | 8 | 0 | 0 | 36 | 0 | 19 | 8 | 50846 | 0.1317 | 0 | 54370.0 | 0 | - | 76.5 | - |
| delivery-trouble | hand-wired | 7 | 4 | 3 | 2 | 0 | 1 | 4/4 | 16.7 | 25.0 | 3 | 0 | 0 | 0 | 0 | 22 | 0 | 4795 | 0.0018 | 0 | 0.0 | 0 | - | 25.4 | driver_drops |
| delivery-trouble | hand-wired | 11 | 4 | 3 | 2 | 0 | 1 | 4/4 | 16.7 | 25.0 | 3 | 0 | 0 | 0 | 0 | 22 | 0 | 4654 | 0.0015 | 0 | 0.0 | 0 | - | 25.4 | driver_drops |
| delivery-trouble | hand-wired | 13 | 4 | 3 | 2 | 0 | 1 | 4/4 | 16.7 | 25.0 | 3 | 0 | 0 | 0 | 0 | 22 | 0 | 4654 | 0.0015 | 0 | 0.0 | 0 | - | 26.2 | driver_drops |
| delivery-trouble | langgoap | 7 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 7 | 0 | 0 | 0 | 0 | 27 | 104 | 5843 | 0.0019 | 6.3 | 0.0 | 4 | 6.0 | 28.8 | - |
| delivery-trouble | langgoap | 11 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 7 | 0 | 0 | 0 | 0 | 27 | 104 | 5902 | 0.0021 | 6.8 | 0.0 | 4 | 6.0 | 26.0 | - |
| delivery-trouble | langgoap | 13 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 7 | 0 | 0 | 0 | 0 | 27 | 104 | 5816 | 0.0019 | 7.2 | 0.0 | 4 | 6.0 | 26.0 | - |
| delivery-trouble | langgraph | 7 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 6 | 0 | 0 | 2 | 4 | 26 | 13 | 9732 | 0.0045 | 0 | 2361.0 | 0 | - | 38.9 | - |
| delivery-trouble | langgraph | 11 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 6 | 0 | 0 | 2 | 4 | 26 | 14 | 9894 | 0.0047 | 0 | 2635.0 | 0 | - | 37.2 | - |
| delivery-trouble | langgraph | 13 | 4 | 4 | 2 | 0 | 0 | 4/4 | 18.8 | 25.0 | 6 | 0 | 0 | 2 | 4 | 26 | 11 | 9395 | 0.0043 | 0 | 2199.0 | 0 | - | 38.3 | - |
| delivery-trouble | llm + gpt-5.4 | 7 | 4 | 3 | 2 | 0 | 0 | 3/4 | 20.7 | 37.0 | 5 | 0 | 0 | 36 | 0 | 21 | 12 | 50249 | 0.1293 | 0 | 53293.0 | 0 | - | 76.6 | driver_drops |
| delivery-trouble | llm + gpt-5.4 | 11 | 4 | 4 | 2 | 0 | 0 | 4/4 | 24.3 | 37.0 | 7 | 0 | 0 | 39 | 0 | 27 | 7 | 55038 | 0.1387 | 0 | 58218.0 | 0 | - | 88.3 | - |
| delivery-trouble | llm + gpt-5.4 | 13 | 4 | 3 | 2 | 0 | 0 | 3/4 | 20.1 | 37.0 | 6 | 0 | 0 | 31 | 0 | 22 | 6 | 43711 | 0.1084 | 0 | 45836.0 | 0 | - | 72.4 | driver_drops |
| happy-path | hand-wired | 7 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 2166 | 0.0008 | 0 | 0.0 | 0 | - | 12.4 | - |
| happy-path | hand-wired | 11 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 2124 | 0.0007 | 0 | 0.0 | 0 | - | 12.6 | - |
| happy-path | hand-wired | 13 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 2135 | 0.0008 | 0 | 0.0 | 0 | - | 11.1 | - |
| happy-path | langgoap | 7 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 36 | 2099 | 0.0007 | 2.3 | 0.0 | 0 | 6.0 | 10.9 | - |
| happy-path | langgoap | 11 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 36 | 2128 | 0.0007 | 2.2 | 0.0 | 0 | 6.0 | 10.2 | - |
| happy-path | langgoap | 13 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 36 | 2121 | 0.0008 | 2.5 | 0.0 | 0 | 6.0 | 10.2 | - |
| happy-path | langgraph | 7 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 2069 | 0.0007 | 0 | 0.0 | 0 | - | 13.1 | - |
| happy-path | langgraph | 11 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 2117 | 0.0007 | 0 | 0.0 | 0 | - | 11.8 | - |
| happy-path | langgraph | 13 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 0 | 0 | 10 | 0 | 2100 | 0.0007 | 0 | 0.0 | 0 | - | 11.2 | - |
| happy-path | llm + gpt-5.4 | 7 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 8 | 0 | 10 | 0 | 11555 | 0.0257 | 0 | 10409.0 | 0 | - | 21.5 | - |
| happy-path | llm + gpt-5.4 | 11 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 8 | 0 | 10 | 0 | 11456 | 0.0255 | 0 | 10391.0 | 0 | - | 21.4 | - |
| happy-path | llm + gpt-5.4 | 13 | 2 | 2 | 2 | 0 | 0 | 2/2 | 11.8 | 11.8 | 0 | 0 | 0 | 8 | 0 | 10 | 0 | 11484 | 0.0255 | 0 | 10468.0 | 0 | - | 21.6 | - |
| incident | hand-wired | 7 | 7 | 2 | 2 | 0 | 5 | 7/7 | 15.8 | 15.8 | 6 | 0 | 0 | 0 | 0 | 25 | 0 | 5446 | 0.0019 | 0 | 0.0 | 0 | - | 28.5 | accepts_then_cancels, driver_drops, gateway_down, payment_declines, restaurant_rejects |
| incident | hand-wired | 11 | 7 | 2 | 2 | 0 | 5 | 7/7 | 15.8 | 15.8 | 6 | 0 | 0 | 0 | 0 | 25 | 0 | 5438 | 0.002 | 0 | 0.0 | 0 | - | 32.1 | accepts_then_cancels, driver_drops, gateway_down, payment_declines, restaurant_rejects |
| incident | hand-wired | 13 | 7 | 2 | 2 | 0 | 5 | 7/7 | 15.8 | 15.8 | 6 | 0 | 0 | 0 | 0 | 25 | 0 | 5478 | 0.002 | 0 | 0.0 | 0 | - | 31.0 | accepts_then_cancels, driver_drops, gateway_down, payment_declines, restaurant_rejects |
| incident | langgoap | 7 | 7 | 7 | 4 | 0 | 0 | 7/7 | 23.2 | 42.8 | 24 | 0 | 0 | 0 | 3 | 50 | 239 | 11885 | 0.0044 | 14.5 | 0.0 | 13 | 6.0 | 55.6 | - |
| incident | langgoap | 11 | 7 | 7 | 4 | 0 | 0 | 7/7 | 24.2 | 42.8 | 24 | 0 | 0 | 0 | 3 | 50 | 239 | 11841 | 0.0043 | 17.5 | 0.0 | 13 | 6.0 | 50.4 | - |
| incident | langgoap | 13 | 7 | 7 | 4 | 0 | 0 | 7/7 | 23.2 | 42.8 | 24 | 0 | 0 | 0 | 3 | 50 | 239 | 12000 | 0.0045 | 17.0 | 0.0 | 13 | 6.0 | 54.2 | - |
| incident | langgraph | 7 | 7 | 7 | 4 | 0 | 0 | 7/7 | 23.2 | 42.8 | 18 | 0 | 0 | 4 | 13 | 48 | 30 | 20069 | 0.0092 | 0 | 5287.0 | 0 | - | 75.8 | - |
| incident | langgraph | 11 | 7 | 7 | 4 | 0 | 0 | 7/7 | 23.2 | 42.8 | 18 | 0 | 0 | 4 | 13 | 48 | 32 | 20918 | 0.0097 | 0 | 4867.0 | 0 | - | 74.2 | - |
| incident | langgraph | 13 | 7 | 7 | 4 | 0 | 0 | 7/7 | 23.2 | 42.8 | 15 | 0 | 0 | 4 | 10 | 48 | 24 | 18489 | 0.0088 | 0 | 4611.0 | 0 | - | 68.2 | - |
| incident | llm + gpt-5.4 | 7 | 7 | 5 | 3 | 1 | 0 | 5/7 | 21.8 | 34.8 | 31 | 0 | 0 | 101 | 0 | 41 | 13 | 134515 | 0.3513 | 0 | 148353.0 | 0 | - | 196.4 | driver_drops, stalled READY_FOR_PICKUP |
| incident | llm + gpt-5.4 | 11 | 7 | 6 | 3 | 1 | 0 | 6/7 | 25.7 | 45.4 | 31 | 0 | 0 | 98 | 0 | 42 | 8 | 140193 | 0.365 | 0 | 136707.0 | 0 | - | 184.6 | stalled READY_FOR_PICKUP |
| incident | llm + gpt-5.4 | 13 | 7 | 5 | 3 | 0 | 0 | 5/7 | 21.8 | 34.8 | 29 | 0 | 0 | 93 | 0 | 38 | 9 | 122470 | 0.3201 | 0 | 136902.0 | 0 | - | 181.0 | stalled READY_FOR_PICKUP |
| kitchen-trouble | hand-wired | 7 | 3 | 1 | 0 | 0 | 2 | 3/3 | 36.1 | 36.1 | 2 | 0 | 0 | 0 | 0 | 13 | 0 | 2896 | 0.001 | 0 | 0.0 | 0 | - | 15.1 | accepts_then_cancels, restaurant_rejects |
| kitchen-trouble | hand-wired | 11 | 3 | 1 | 0 | 0 | 2 | 3/3 | 36.1 | 36.1 | 2 | 0 | 0 | 0 | 0 | 13 | 0 | 2892 | 0.001 | 0 | 0.0 | 0 | - | 14.4 | accepts_then_cancels, restaurant_rejects |
| kitchen-trouble | hand-wired | 13 | 3 | 1 | 0 | 0 | 2 | 3/3 | 36.1 | 36.1 | 2 | 0 | 0 | 0 | 0 | 13 | 0 | 2892 | 0.001 | 0 | 0.0 | 0 | - | 14.9 | accepts_then_cancels, restaurant_rejects |
| kitchen-trouble | langgoap | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.1 | 37.8 | 12 | 0 | 0 | 0 | 3 | 23 | 186 | 6150 | 0.0026 | 9.5 | 0.0 | 3 | 6.0 | 28.1 | - |
| kitchen-trouble | langgoap | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.8 | 37.8 | 12 | 0 | 0 | 0 | 3 | 23 | 186 | 6137 | 0.0024 | 9.9 | 0.0 | 3 | 6.0 | 25.0 | - |
| kitchen-trouble | langgoap | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.8 | 37.8 | 12 | 0 | 0 | 0 | 3 | 23 | 186 | 6065 | 0.0025 | 9.9 | 0.0 | 3 | 6.0 | 25.6 | - |
| kitchen-trouble | langgraph | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.4 | 35.8 | 13 | 0 | 0 | 2 | 9 | 25 | 14 | 11453 | 0.0052 | 0 | 2965.0 | 0 | - | 40.5 | - |
| kitchen-trouble | langgraph | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.4 | 35.8 | 13 | 0 | 0 | 2 | 9 | 25 | 14 | 11371 | 0.0053 | 0 | 1994.0 | 0 | - | 40.4 | - |
| kitchen-trouble | langgraph | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 20.2 | 35.8 | 13 | 0 | 0 | 2 | 9 | 25 | 19 | 11807 | 0.0056 | 0 | 2271.0 | 0 | - | 40.9 | - |
| kitchen-trouble | llm + gpt-5.4 | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.7 | 34.8 | 2 | 0 | 0 | 21 | 0 | 21 | 3 | 30805 | 0.0733 | 0 | 30724.0 | 0 | - | 63.1 | - |
| kitchen-trouble | llm + gpt-5.4 | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.7 | 34.8 | 2 | 0 | 0 | 18 | 0 | 21 | 1 | 26549 | 0.0615 | 0 | 26771.0 | 0 | - | 50.2 | - |
| kitchen-trouble | llm + gpt-5.4 | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.7 | 34.8 | 3 | 0 | 0 | 20 | 0 | 22 | 2 | 29512 | 0.0692 | 0 | 28727.0 | 0 | - | 55.4 | - |
| payment-trouble | hand-wired | 7 | 3 | 1 | 1 | 0 | 2 | 3/3 | 12.1 | 12.1 | 2 | 0 | 0 | 0 | 0 | 6 | 0 | 1337 | 0.0005 | 0 | 0.0 | 0 | - | 7.2 | gateway_down, payment_declines |
| payment-trouble | hand-wired | 11 | 3 | 1 | 1 | 0 | 2 | 3/3 | 12.1 | 12.1 | 2 | 0 | 0 | 0 | 0 | 6 | 0 | 1297 | 0.0005 | 0 | 0.0 | 0 | - | 6.8 | gateway_down, payment_declines |
| payment-trouble | hand-wired | 13 | 3 | 1 | 1 | 0 | 2 | 3/3 | 12.1 | 12.1 | 2 | 0 | 0 | 0 | 0 | 6 | 0 | 1269 | 0.0005 | 0 | 0.0 | 0 | - | 6.6 | gateway_down, payment_declines |
| payment-trouble | langgoap | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 6 | 0 | 0 | 0 | 0 | 17 | 93 | 3710 | 0.0013 | 5.1 | 0.0 | 4 | 6.0 | 17.9 | - |
| payment-trouble | langgoap | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 6 | 0 | 0 | 0 | 0 | 17 | 93 | 3579 | 0.0012 | 5.2 | 0.0 | 4 | 6.0 | 17.2 | - |
| payment-trouble | langgoap | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 6 | 0 | 0 | 0 | 0 | 17 | 93 | 3664 | 0.0013 | 4.9 | 0.0 | 4 | 6.0 | 23.6 | - |
| payment-trouble | langgraph | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 4 | 0 | 0 | 2 | 3 | 16 | 9 | 6517 | 0.0033 | 0 | 1990.0 | 0 | - | 23.2 | - |
| payment-trouble | langgraph | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 4 | 0 | 0 | 2 | 3 | 16 | 9 | 6441 | 0.0032 | 0 | 2483.0 | 0 | - | 23.8 | - |
| payment-trouble | langgraph | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 15.7 | 22.8 | 4 | 0 | 0 | 2 | 3 | 16 | 9 | 6526 | 0.0033 | 0 | 2755.0 | 0 | - | 25.7 | - |
| payment-trouble | llm + gpt-5.4 | 7 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.7 | 34.8 | 15 | 0 | 0 | 45 | 0 | 17 | 2 | 58117 | 0.1522 | 0 | 67458.0 | 0 | - | 87.0 | - |
| payment-trouble | llm + gpt-5.4 | 11 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.7 | 34.8 | 15 | 0 | 0 | 46 | 0 | 17 | 4 | 59498 | 0.1561 | 0 | 71344.0 | 0 | - | 90.1 | - |
| payment-trouble | llm + gpt-5.4 | 13 | 3 | 3 | 2 | 0 | 0 | 3/3 | 19.7 | 34.8 | 14 | 0 | 0 | 44 | 0 | 16 | 2 | 57114 | 0.1498 | 0 | 64659.0 | 0 | - | 87.8 | - |
| storm | hand-wired | 7 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 5250 | 0.0019 | 0 | 0.0 | 0 | - | 26.0 | - |
| storm | hand-wired | 11 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 5371 | 0.002 | 0 | 0.0 | 0 | - | 27.2 | - |
| storm | hand-wired | 13 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 5271 | 0.0018 | 0 | 0.0 | 0 | - | 28.4 | - |
| storm | langgoap | 7 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 16 | 0 | 0 | 0 | 0 | 28 | 139 | 6010 | 0.0021 | 10.1 | 0.0 | 11 | 6.0 | 27.6 | - |
| storm | langgoap | 11 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 16 | 0 | 0 | 0 | 0 | 28 | 139 | 5907 | 0.002 | 10.5 | 0.0 | 11 | 6.0 | 28.5 | - |
| storm | langgoap | 13 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 16 | 0 | 0 | 0 | 0 | 28 | 139 | 5916 | 0.0019 | 9.5 | 0.0 | 11 | 6.0 | 27.4 | - |
| storm | langgraph | 7 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 5284 | 0.0019 | 0 | 0.0 | 0 | - | 28.0 | - |
| storm | langgraph | 11 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 5316 | 0.0019 | 0 | 0.0 | 0 | - | 27.7 | - |
| storm | langgraph | 13 | 5 | 5 | 2 | 0 | 0 | 5/5 | 21.0 | 31.5 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 5340 | 0.002 | 0 | 0.0 | 0 | - | 32.1 | - |
| storm | llm + gpt-5.4 | 7 | 5 | 4 | 2 | 3 | 0 | 4/5 | 24.4 | 34.6 | 18 | 0 | 0 | 66 | 0 | 26 | 22 | 89969 | 0.2393 | 0 | 100446.0 | 0 | - | 131.9 | stalled READY_FOR_PICKUP |
| storm | llm + gpt-5.4 | 11 | 5 | 5 | 3 | 3 | 0 | 5/5 | 23.7 | 34.6 | 15 | 0 | 0 | 65 | 0 | 28 | 26 | 88834 | 0.2363 | 0 | 98988.0 | 0 | - | 129.4 | - |
| storm | llm + gpt-5.4 | 13 | 5 | 5 | 3 | 2 | 0 | 5/5 | 23.7 | 34.6 | 15 | 0 | 0 | 66 | 0 | 28 | 21 | 92546 | 0.244 | 0 | 96167.0 | 0 | - | 127.0 | - |
<!-- bench:end -->

## Credits

The mobile apps and the public API contract are the work of the
[Delivery Plus](https://github.com/Delivery-Plus0) project (MIT). The domain
model takes after Chris Richardson's
[FTGO](https://github.com/microservices-patterns/ftgo-application) (Apache-2.0).

An [Integrallis](https://www.integrallis.com) production. MIT licensed.

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
resumes every thread after a restart. The bench rows for `langgoap` in
[`BENCH.md`](BENCH.md) are produced with the model off.

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
scenario takes `times = 2` to decline both. A gateway outage (`gateway_down`,
`for_seconds`) refuses every payment processed in its window with a 503 and the
platform announces the gateway going down and coming back; the hand-wired engine
cancels, an engine that can wait retries the pending attempt
(`retry_pending_payment`) when it is back. Credits: a ledger per customer that
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

Add `--repeat 10` to run the same scenario, engine and seed ten times, so the
model is the only source of variation; `results/repeats/` keeps every run's
summary with the distribution of what differed, rendered as a second table in
`BENCH.md`. An engine without a model must produce identical runs.

The bench runs a scenario with nobody at the apps: autopilots place and pay the
orders, drive the deliveries when the simulated phone arrives, answer support's
questions with the recommended option after a while, and accept the first
substitute offered after a moment's thought, on a frozen clock advanced tick by
tick. Each run writes `results/<scenario>-<engine>-seed<n>.json` with, per
order, the final status, whether it arrived within its promise, the minutes it
took, whether its state is consistent, the failures fired against it, the
engine's recovery decisions, human prompts, LLM calls and cost. Cost is computed
with the model prices in `bench.env` (USD per million tokens, with the source
and date in the file); every results file records the prices it was given, and a
run without them says unpriced. [`BENCH.md`](BENCH.md) is generated from those
files by `make bench-table` and CI fails when it is stale. The hand-wired rows
are the baseline; their "Not recovered" column lists the failures the engine
does not recover from.

## Credits

The mobile apps and the public API contract are the work of the
[Delivery Plus](https://github.com/Delivery-Plus0) project (MIT). The domain
model takes after Chris Richardson's
[FTGO](https://github.com/microservices-patterns/ftgo-application) (Apache-2.0).

An [Integrallis](https://www.integrallis.com) production. MIT licensed.

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
submodules, all three from our forks, which add a build-time currency and a map
that opens on the city (both offered upstream), and read the API address from `EXPO_PUBLIC_API_BASE_URL`; the web builds are
made in English and dollars and get fixed browser positions in San Francisco (the customer at home by Union Square, the driver around the corner
from the kitchens) so no app asks for geolocation, and the API's simulated driver phone moves the
driver on the customer's map because a browser window cannot.

Every order placed in the customer app flows through payment, the restaurant
app's kitchen board, dispatch to an online driver, the driver app's pickup and
drop-off, live tracking and notifications, priced in US dollars (the contract
types the currency as a free string; its examples say EGP). `CONTRACT.md` lists which of the
Delivery Plus API operations are implemented.

## The engine

The platform records facts and publishes them: a payment completed, the kitchen is
ready, a driver dropped the job. What happens next to the order is decided in one
place, the order engine (`foood/engine.py`). `main` ships `hand-wired`, fixed
reactions with one anticipated fallback per failure; the `langgraph` and `langgoap`
branches add theirs and change nothing else. `foood up --engine hand-wired` picks
one, and `GET /api/_engine?orderId=...` lists the decisions taken for an order and
why.

## One frontier model (the `llm` branch)

`foood up --engine llm` gives the platform one model, the frontier model of its
family (`FOOOD_LLM_MODEL`, default `gpt-5.4`, with `OPENAI_API_KEY` from the
environment or a `.env` file), and lets it run the evening
(`foood/agent_engine.py`). This is the first refactoring, the one most teams
ship: on every event that concerns an order the model is told what happened and
given tools, and it calls what it likes until it stops. The tools are the
platform's actions (confirm the payment, send the order out, dispatch, widen the
search, replace the order at another kitchen, offer substitutes and carry out
the one the customer accepts, retry the backup card, issue a credit, move the
promise, ask support, wait), lookups (the order's facts, the kitchens that can
make it, the drivers online, the ride's ETA now) and the world's MCP read tools
(`get_weather`, `get_traffic`, `eta`, `forecast`, `zones`), never its controls.
Timers stand in for the forks a graph would draw: a delivery nobody takes, a
kitchen running long, a promise that passes, each raise one episode; so does a
change in the world, for every order in flight.

The guards are the ones a careful team would add and no more: six model rounds
and ten tool calls per episode, a timeout and no retries per call, and the
hand-wired reaction whenever the model errs or spends its budget without acting.
Every round is a decision call recorded on the order with the model's name,
tokens, latency and cost at that model's price; every tool call is recorded as
what it did. `FOOOD_LLM=off` (or no key) makes the engine the hand-wired one,
and the bench numbers match `main` exactly.

### Ordering in a sentence

```bash
uv run foood ask "two QA burgers and fries to the office, ring twice"
```

`POST /api/orders/intake` (outside the Delivery Plus contract) is the same loop
with the cart as its tools: the model searches the open menus, fills the cart
from one kitchen and places the order through the same door the app uses, so
every guard still applies; when a dish is unknown or sold out, or the sentence
is ambiguous, it asks one question back instead of guessing. The call is
recorded on the order as a language call.

### Messages in the model's words

Every notification keeps its `messageKey` and `params`, which the customer app
localises on its own; with a model available the server's `message` is written
for the situation (`foood/messages.py`): the kitchen's reason in plain words,
the card that paid, the credit added. Any error, an empty or overlong answer,
or no model leaves the template from `main` in place, and each phrasing call is
recorded on the order's decisions with its tokens.

## The LangGraph engine (the `langgraph` branch)

`foood up --engine langgraph` runs the lifecycle as a LangGraph `StateGraph`,
one thread per order, drawn in [`foood/langgraph_engine.mmd`](foood/langgraph_engine.mmd).
Action nodes (`confirm`, `dispatch`, `cleanup`) call the same domain functions
the hand-wired engine calls; wait nodes interrupt until the platform publishes
the event they need (a payment outcome, the kitchen's status, a driver, a
pickup, the drop-off), and the engine resumes them with the event. Every
failure routes to `recover`, the fork. A SQLite checkpointer
(`FOOOD_CHECKPOINTS`, `build/langgraph-checkpoints.sqlite` under `foood up`)
keeps every thread, so a new engine process resumes each order where it
paused.

At `recover`, a model picks one of the platform's recovery tools: a replacement
order from another kitchen, the backup card, a credit, a wider dispatch,
waiting, a question to support (who picks the tool), or refund and cancel. It
is corralled the way careful teams corral it: an allow-list of tools per
failure (`foood/recovery.py`), a budget of three recoveries per order, a
timeout and no retries per call, structured output, and the hand-wired
reaction as the fallback whenever the model errs, picks a tool it may not,
or the budget is spent. A poor choice costs money or minutes, never a
consistent state. Every call is recorded on the order's decisions with its
tokens, so the bench reports what the judgement cost. The model is
`FOOOD_LLM_MODEL` (default `gpt-5.4-mini`) with `OPENAI_API_KEY` from the
environment or a `.env` file; `FOOOD_LLM=off` disables it, and then `recover`
applies the hand-wired reaction and the bench numbers match `main` exactly.
Set `FOOOD_LLM_PRICE_IN` and `FOOOD_LLM_PRICE_OUT` (USD per million tokens)
to have costs priced; otherwise only tokens are reported.

### A model per node

The graph is what lets a business process use different models for different
needs, and `models.toml` says which: a small model for intake (extraction), the
cheapest that writes well for the messages, a small tool-calling model for the
world lookups, a classifier for a kitchen's free-text reason, a mid model with
culinary knowledge for substitutes, a mid model for the judgement at `recover`,
and the frontier model for one call only, when `decide`'s answer is unusable
(an error, or a tool outside the allow-list); the hand-wired reaction remains
the fallback after that. Each node has its own timeout and no retries.
`FOOOD_MODELS` points at another file, `FOOOD_MODEL_<NODE>` sets one node,
`FOOOD_MODEL_ALL` sets every node (the bench's model matrix), and `off`
switches a node's model off so its deterministic fallback applies. Every call
is recorded on the order with its node, its model, tokens, latency and cost at
that model's price (`bench.env`), and the results files sum them per node.

### Look up, then decide

Every failure now goes through `lookup` before `recover`. The lookup node
gathers the world's facts for the fork in code, through the same MCP tools a
model would call: the weather in the kitchen's zone, the ride from the kitchen
to the door now against a clear day and the promise, closed corridors, what is
scheduled for the next half hour. A small tool-calling model (the roster's
`lookup`) may look further with the same tools, two rounds at most, and its
calls are counted as perception, apart from the decision. The decision node
then sees facts and no tools at all, which is what lets it be a smaller model.
The allow-list reads the conditions: a kitchen behind a closed corridor is no
alternative tonight, so `replace_order` is withheld when none is reachable, and
in rain or a storm a wider driver search comes first.

### A model proposes, the platform grounds, the customer decides

Two more small-model nodes sit between `lookup` and `recover` for the
kitchen's failures. `classify` reads the kitchen's free-text note into a kind,
because "the last portion just went out" and "the grill is down" call for
different recoveries. When a dish ran out and the kitchen is fine, `suggest`
proposes substitutes: a mid model with culinary knowledge picks up to five
dishes from the pool of what open kitchens within reach can make now, the
platform keeps only what is really on an open menu and prices each option as a
replacement (`foood/replacements.py`), and the customer is asked. The thread
waits at `wait_customer`; an accepted option becomes the replacement order, a
declined one (or three minutes of silence) sends the fork its decision with that
fact. Both nodes are perception calls, counted apart from the decision, and
without them the fork behaves as before.

### Ordering in a sentence

```bash
uv run foood ask "two QA burgers and fries to the office, ring twice"
```

`POST /api/orders/intake` (outside the Delivery Plus contract) runs a small
LangGraph: a model reads the sentence against the open kitchens' menus into a
structured request, a deterministic step matches it to real menu items and
turns anything unclear into a question instead of an order (several kitchens
could make it, a dish nobody has, a sold-out item), and the order is placed
through the same door the app uses, so every guard still applies. The order
then shows up in the customer app like any other, with the intake call's
tokens on its decisions.

## The LangGOAP engine (this branch)

`foood up --engine langgoap` runs the lifecycle as a goal and a set of actions
(`foood/goap_engine.py`), one plan per customer order that follows a replacement
to the door. A sensor derives the world from the platform before every plan:
paid or declined, the kitchen cooking, late, ready or failed, a driver being
searched for, assigned, the order picked up, lost or delivered, whether another
kitchen can make it, whether that needs approval, whether the promise is
breached and whether the customer was compensated. The actions are the same
recovery tools the other engines have, with preconditions, effects and costs:
the waits (payment, kitchen, driver, pickup, drop-off) interrupt until the
platform publishes an event and return what they observe; a replacement order
costs its price difference, prep time and distance; widening the search is
cheaper than waiting longer; accepting a late kitchen is dear; a credit settles
the compensation the goal demands once the promise is breached. The goal is
`delivered` and `compensated_if_late`. When what a wait observes is not what
the plan expected, the planner re-plans from the real state; when no route to
the goal exists, the engine refunds and cancels and says why. Only the
substitution approval asks a person, as a typed form through support. No model
is called after intake; the drawing is in
[`foood/langgoap_engine.mmd`](foood/langgoap_engine.mmd) and the checkpointer
resumes every thread after a restart. The bench rows for `langgoap` in
[`BENCH.md`](BENCH.md) are produced with the model off.

## Support, the human in the loop

The customer app has no approval screen and the apps stay unchanged, so when an
engine needs a person it asks support. http://localhost:3000/support (sign in as
`admin@example.com`) lists the open questions with their options or form, the
recent orders with the decisions taken for each, and the loaded scenario's log.
A question unanswered by its deadline takes the default the engine declared. The
hand-wired engine never asks; the LangGraph and LangGOAP engines do. Each order
also carries its plan panel: the current route with the step it waits at, its
cost, how often and why it was re-planned, the kitchens the planner weighed with
their price difference, and, for the LangGraph engine, the model's last reason.

The whole evening is on one map at http://localhost:3000/map: the kitchens, every
order's door with the line from its kitchen coloured by its status, and the drivers
where their phones last reported, redrawn every two seconds from `GET /api/_map`
(not part of the contract, like `/api/_engine`).

The world outside the kitchens is a state of its own (`foood/world.py`): the
weather, the traffic and the road closures of a city cut into zones around the
kitchens, with a corridor between each pair of zones that matter. Couriers slow
down in rain and in traffic, kitchens take longer in a storm, a closed corridor
turns a route into a detour. It is served as an MCP server at
http://localhost:3000/mcp (streamable HTTP), so any MCP client can read it
(`get_weather`, `get_traffic`, `eta`, `forecast`, `zones`) or move it
(`set_weather`, `set_traffic`, `close_road`, `schedule`); the engines consult
the same tools, and a scenario moves the same state on schedule. The page at
http://localhost:3000/world shows it and lets support move it by hand.

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
in a scenario takes `times = 2` to decline both. Credits: a ledger per customer that
engines or support fill as compensation (`POST /api/credits`, one credit per
order); the next quote and order apply the balance, the payment charges the
rest, and a cancelled order gives its credit back (`GET /api/credits/me`). Dispatch strategies (`foood/dispatch.py`): longest idle, nearest first by
last known position, and stacking a ready order onto a busy driver whose
drop-off is near the restaurant so it becomes their next job; a waiting
delivery shows how long it has searched. The hand-wired engine uses none of
these: it dispatches longest-idle, refunds and cancels, as most shops do.

Substitutes go one step further (`candidate_pool`, `substitutes`,
`offer_substitutes` in the same module): when the item itself is gone, whoever
proposes candidate dishes, a model with its culinary knowledge or a person,
hands their names in; the platform keeps only what an open kitchen within reach
can make now (the order's own kitchen included, which spares a new delivery),
prices each option as a replacement, and puts them to the customer, who accepts
one or declines at `POST /api/orders/{id}/substitution`. Nothing not on an open
menu is ever offered, whatever was proposed. The hand-wired engine does not
substitute; it cancels, as before.

## Break it on purpose

```bash
make up SCENARIO=kitchen-trouble ENGINE=hand-wired   # or: uv run foood up --scenario kitchen-trouble --engine hand-wired
```

A scenario is a TOML file in `scenarios/` that can put the kitchen on autopilot
(no one needs to sit in the restaurant app) and inject failures by order
number: the *n*-th order placed after the server starts. Each injection is a
flag on a simulator the platform already has, so with no scenario loaded
nothing behaves differently. The bundled ones:

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
tests in `tests/test_scenario.py` prove each failure ends in a consistent
state across the order, its payment and its delivery.

## Measure it

```bash
make bench                      # every bundled scenario against ENGINE (hand-wired) under SEED (7)
uv run foood bench --scenario delivery-trouble --engine hand-wired --seed 7
```

The bench runs a scenario with nobody at the apps: autopilots place and pay
the orders, drive the deliveries when the simulated phone arrives, answer
support's questions with the recommended option after a while, and accept the
first substitute offered after a moment's thought, on a frozen clock advanced
tick by tick. Each run writes `results/<scenario>-<engine>-seed<n>.json`
with, per order, the final status, whether it arrived within its promise, the
minutes it took, whether its state is consistent, the failures fired against
it, the engine's recovery decisions, human prompts, LLM calls and cost, and how
long the engine took to decide: the wall-clock of every planning round, the round
trip of every model call, re-plans and plan steps, plus the real time the whole
run took and the machine it ran on.
[`BENCH.md`](BENCH.md) and the table below are generated from those files by
`make bench-table` and CI fails when either is stale; no number in them is
typed by hand. The hand-wired rows are the baseline every other engine is
compared with, and they say plainly which failures it does not recover from.
`make bench-all` runs every bundled scenario, including `incident` (one bad
evening, seven orders, six different failures), against every engine under
seeds 7, 11 and 13; an engine that calls a model is labelled with the model and
shown per seed so its variation is visible rather than averaged away.

<!-- bench:start -->
| Scenario | Engine | Seed | Orders | Delivered | Within promise | Cancelled | Consistent | Mean min | p95 min | Recovery decisions | Human prompts | LLM calls, decisions | LLM calls, language | LLM tokens | Cost (USD) | Planner ms | Model ms, decisions | Re-plans | Plan steps | Wall s | Not recovered |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| delivery-trouble | hand-wired | 7 | 4 | 3 | 2 | 1 | 4/4 | 15.2 | 22.2 | 3 | 0 | 0 | 22 | 5784 | unpriced | 0 | 0.0 | 0 | - | 19.7 | driver_drops |
| delivery-trouble | hand-wired | 11 | 4 | 3 | 2 | 1 | 4/4 | 15.2 | 22.2 | 3 | 0 | 0 | 22 | 5846 | unpriced | 0 | 0.0 | 0 | - | 19.4 | driver_drops |
| delivery-trouble | hand-wired | 13 | 4 | 3 | 2 | 1 | 4/4 | 15.2 | 22.2 | 3 | 0 | 0 | 22 | 5794 | unpriced | 0 | 0.0 | 0 | - | 21.3 | driver_drops |
| delivery-trouble | langgoap | 7 | 4 | 4 | 2 | 0 | 4/4 | 16.8 | 22.2 | 4 | 0 | 0 | 27 | 7224 | unpriced | 4.1 | 0.0 | 2 | 6.0 | 24.9 | - |
| delivery-trouble | langgoap | 11 | 4 | 4 | 2 | 0 | 4/4 | 16.8 | 22.2 | 4 | 0 | 0 | 27 | 7142 | unpriced | 4.9 | 0.0 | 2 | 6.0 | 24.2 | - |
| delivery-trouble | langgoap | 13 | 4 | 4 | 2 | 0 | 4/4 | 16.8 | 22.2 | 4 | 0 | 0 | 27 | 7183 | unpriced | 3.6 | 0.0 | 2 | 6.0 | 25.2 | - |
| delivery-trouble | langgraph + gpt-5.4-mini | 7 | 4 | 4 | 2 | 0 | 4/4 | 16.8 | 22.2 | 4 | 0 | 2 | 26 | 8227 | unpriced | 0 | 2511.4 | 0 | - | 25.9 | - |
| delivery-trouble | langgraph + gpt-5.4-mini | 11 | 4 | 4 | 2 | 0 | 4/4 | 16.8 | 22.2 | 4 | 0 | 2 | 26 | 8299 | unpriced | 0 | 2783.9 | 0 | - | 26.5 | - |
| delivery-trouble | langgraph + gpt-5.4-mini | 13 | 4 | 4 | 2 | 0 | 4/4 | 16.8 | 22.2 | 4 | 0 | 2 | 26 | 8247 | unpriced | 0 | 2628.6 | 0 | - | 26.3 | - |
| happy-path | hand-wired | 7 | 2 | 2 | 2 | 0 | 2/2 | 10.9 | 10.9 | 0 | 0 | 0 | 10 | 2571 | unpriced | 0 | 0.0 | 0 | - | 12.2 | - |
| happy-path | hand-wired | 11 | 2 | 2 | 2 | 0 | 2/2 | 10.9 | 10.9 | 0 | 0 | 0 | 10 | 2612 | unpriced | 0 | 0.0 | 0 | - | 9.6 | - |
| happy-path | hand-wired | 13 | 2 | 2 | 2 | 0 | 2/2 | 10.9 | 10.9 | 0 | 0 | 0 | 10 | 2626 | unpriced | 0 | 0.0 | 0 | - | 9.5 | - |
| happy-path | langgoap | 7 | 2 | 2 | 2 | 0 | 2/2 | 10.9 | 10.9 | 0 | 0 | 0 | 10 | 2631 | unpriced | 4.3 | 0.0 | 0 | 6.0 | 9.4 | - |
| happy-path | langgoap | 11 | 2 | 2 | 2 | 0 | 2/2 | 10.9 | 10.9 | 0 | 0 | 0 | 10 | 2626 | unpriced | 3.9 | 0.0 | 0 | 6.0 | 10.7 | - |
| happy-path | langgoap | 13 | 2 | 2 | 2 | 0 | 2/2 | 10.9 | 10.9 | 0 | 0 | 0 | 10 | 2626 | unpriced | 4.5 | 0.0 | 0 | 6.0 | 9.8 | - |
| happy-path | langgraph + gpt-5.4-mini | 7 | 2 | 2 | 2 | 0 | 2/2 | 10.9 | 10.9 | 0 | 0 | 0 | 10 | 2565 | unpriced | 0 | 0.0 | 0 | - | 9.3 | - |
| happy-path | langgraph + gpt-5.4-mini | 11 | 2 | 2 | 2 | 0 | 2/2 | 10.9 | 10.9 | 0 | 0 | 0 | 10 | 2592 | unpriced | 0 | 0.0 | 0 | - | 9.3 | - |
| happy-path | langgraph + gpt-5.4-mini | 13 | 2 | 2 | 2 | 0 | 2/2 | 10.9 | 10.9 | 0 | 0 | 0 | 10 | 2578 | unpriced | 0 | 0.0 | 0 | - | 11.0 | - |
| incident | hand-wired | 7 | 6 | 2 | 2 | 4 | 6/6 | 14.9 | 14.9 | 5 | 0 | 0 | 25 | 6635 | unpriced | 0 | 0.0 | 0 | - | 24.3 | accepts_then_cancels, driver_drops, payment_declines, restaurant_rejects |
| incident | hand-wired | 11 | 6 | 2 | 2 | 4 | 6/6 | 14.9 | 14.9 | 5 | 0 | 0 | 25 | 6696 | unpriced | 0 | 0.0 | 0 | - | 25.2 | accepts_then_cancels, driver_drops, payment_declines, restaurant_rejects |
| incident | hand-wired | 13 | 6 | 2 | 2 | 4 | 6/6 | 14.9 | 14.9 | 5 | 0 | 0 | 25 | 6687 | unpriced | 0 | 0.0 | 0 | - | 25.1 | accepts_then_cancels, driver_drops, payment_declines, restaurant_rejects |
| incident | langgoap | 7 | 6 | 6 | 4 | 0 | 6/6 | 17.9 | 28.0 | 10 | 0 | 0 | 43 | 11510 | unpriced | 7.3 | 0.0 | 5 | 6.0 | 40.5 | - |
| incident | langgoap | 11 | 6 | 6 | 4 | 0 | 6/6 | 17.9 | 28.0 | 10 | 0 | 0 | 43 | 11472 | unpriced | 9.2 | 0.0 | 5 | 6.0 | 40.2 | - |
| incident | langgoap | 13 | 6 | 6 | 4 | 0 | 6/6 | 17.9 | 28.0 | 10 | 0 | 0 | 43 | 11361 | unpriced | 6.0 | 0.0 | 5 | 6.0 | 41.5 | - |
| incident | langgraph + gpt-5.4-mini | 7 | 6 | 6 | 4 | 0 | 6/6 | 17.9 | 28.0 | 7 | 0 | 4 | 42 | 13958 | unpriced | 0 | 6147.6 | 0 | - | 46.0 | - |
| incident | langgraph + gpt-5.4-mini | 11 | 6 | 6 | 4 | 0 | 6/6 | 17.9 | 28.0 | 7 | 0 | 4 | 42 | 13991 | unpriced | 0 | 5311.7 | 0 | - | 45.1 | - |
| incident | langgraph + gpt-5.4-mini | 13 | 6 | 6 | 4 | 0 | 6/6 | 17.9 | 28.0 | 7 | 0 | 4 | 42 | 13881 | unpriced | 0 | 5225.2 | 0 | - | 45.7 | - |
| kitchen-trouble | hand-wired | 7 | 3 | 1 | 0 | 2 | 3/3 | 35.2 | 35.2 | 2 | 0 | 0 | 13 | 3485 | unpriced | 0 | 0.0 | 0 | - | 11.8 | accepts_then_cancels, restaurant_rejects |
| kitchen-trouble | hand-wired | 11 | 3 | 1 | 0 | 2 | 3/3 | 35.2 | 35.2 | 2 | 0 | 0 | 13 | 3455 | unpriced | 0 | 0.0 | 0 | - | 12.8 | accepts_then_cancels, restaurant_rejects |
| kitchen-trouble | hand-wired | 13 | 3 | 1 | 0 | 2 | 3/3 | 35.2 | 35.2 | 2 | 0 | 0 | 13 | 3463 | unpriced | 0 | 0.0 | 0 | - | 11.6 | accepts_then_cancels, restaurant_rejects |
| kitchen-trouble | langgoap | 7 | 3 | 3 | 2 | 0 | 3/3 | 18.8 | 35.9 | 7 | 0 | 0 | 22 | 5991 | unpriced | 6.4 | 0.0 | 3 | 6.0 | 19.2 | - |
| kitchen-trouble | langgoap | 11 | 3 | 3 | 2 | 0 | 3/3 | 18.8 | 35.9 | 7 | 0 | 0 | 22 | 5902 | unpriced | 5.9 | 0.0 | 3 | 6.0 | 20.6 | - |
| kitchen-trouble | langgoap | 13 | 3 | 3 | 2 | 0 | 3/3 | 18.8 | 35.9 | 7 | 0 | 0 | 22 | 5872 | unpriced | 6.4 | 0.0 | 3 | 6.0 | 20.4 | - |
| kitchen-trouble | langgraph + gpt-5.4-mini | 7 | 3 | 3 | 2 | 0 | 3/3 | 17.1 | 30.8 | 6 | 0 | 3 | 24 | 8464 | unpriced | 0 | 4376.8 | 0 | - | 28.1 | - |
| kitchen-trouble | langgraph + gpt-5.4-mini | 11 | 3 | 3 | 2 | 0 | 3/3 | 17.1 | 30.8 | 6 | 0 | 3 | 24 | 8465 | unpriced | 0 | 3988.0 | 0 | - | 27.0 | - |
| kitchen-trouble | langgraph + gpt-5.4-mini | 13 | 3 | 3 | 2 | 0 | 3/3 | 17.1 | 30.8 | 6 | 0 | 3 | 24 | 8466 | unpriced | 0 | 3695.6 | 0 | - | 24.8 | - |
| payment-trouble | hand-wired | 7 | 2 | 1 | 1 | 1 | 2/2 | 11.2 | 11.2 | 1 | 0 | 0 | 6 | 1601 | unpriced | 0 | 0.0 | 0 | - | 5.7 | payment_declines |
| payment-trouble | hand-wired | 11 | 2 | 1 | 1 | 1 | 2/2 | 11.2 | 11.2 | 1 | 0 | 0 | 6 | 1611 | unpriced | 0 | 0.0 | 0 | - | 5.6 | payment_declines |
| payment-trouble | hand-wired | 13 | 2 | 1 | 1 | 1 | 2/2 | 11.2 | 11.2 | 1 | 0 | 0 | 6 | 1576 | unpriced | 0 | 0.0 | 0 | - | 5.6 | payment_declines |
| payment-trouble | langgoap | 7 | 2 | 2 | 2 | 0 | 2/2 | 11.2 | 11.2 | 2 | 0 | 0 | 11 | 2923 | unpriced | 2.0 | 0.0 | 1 | 6.0 | 10.3 | - |
| payment-trouble | langgoap | 11 | 2 | 2 | 2 | 0 | 2/2 | 11.2 | 11.2 | 2 | 0 | 0 | 11 | 2895 | unpriced | 2.9 | 0.0 | 1 | 6.0 | 10.3 | - |
| payment-trouble | langgoap | 13 | 2 | 2 | 2 | 0 | 2/2 | 11.2 | 11.2 | 2 | 0 | 0 | 11 | 2879 | unpriced | 2.9 | 0.0 | 1 | 6.0 | 9.8 | - |
| payment-trouble | langgraph + gpt-5.4-mini | 7 | 2 | 2 | 2 | 0 | 2/2 | 11.2 | 11.2 | 1 | 0 | 1 | 11 | 3554 | unpriced | 0 | 1239.0 | 0 | - | 10.8 | - |
| payment-trouble | langgraph + gpt-5.4-mini | 11 | 2 | 2 | 2 | 0 | 2/2 | 11.2 | 11.2 | 1 | 0 | 1 | 11 | 3509 | unpriced | 0 | 1218.5 | 0 | - | 11.3 | - |
| payment-trouble | langgraph + gpt-5.4-mini | 13 | 2 | 2 | 2 | 0 | 2/2 | 11.2 | 11.2 | 1 | 0 | 1 | 11 | 3545 | unpriced | 0 | 1395.3 | 0 | - | 11.7 | - |
<!-- bench:end -->

## Credits

The mobile apps and the public API contract are the work of the
[Delivery Plus](https://github.com/Delivery-Plus0) project (MIT). The domain
model takes after Chris Richardson's
[FTGO](https://github.com/microservices-patterns/ftgo-application) (Apache-2.0).

An [Integrallis](https://www.integrallis.com) production. MIT licensed.

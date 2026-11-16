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
make serve        # API on http://localhost:3000, docs at /docs
```

The apps live in `apps/` as git submodules of the Delivery Plus repositories
and read the API address from `EXPO_PUBLIC_API_BASE_URL`.

## Credits

The mobile apps and the public API contract are the work of the
[Delivery Plus](https://github.com/Delivery-Plus0) project (MIT). The domain
model takes after Chris Richardson's
[FTGO](https://github.com/microservices-patterns/ftgo-application) (Apache-2.0).

An [Integrallis](https://www.integrallis.com) production. MIT licensed.

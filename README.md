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

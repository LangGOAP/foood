"""Command-line entry points: ``foood serve`` runs the API, ``foood up`` the whole demo."""

from __future__ import annotations

import os

import typer
import uvicorn

app = typer.Typer(no_args_is_help=True, add_completion=False)


SCENARIO_HELP = "A bundled scenario name (see scenarios/) or a path to a TOML file."
ENGINE_HELP = "The order engine that decides the lifecycle (see foood/engine.py)."


def _select(scenario: str | None, engine: str | None) -> None:
    if scenario:
        from foood.scenario import resolve

        os.environ["FOOOD_SCENARIO"] = str(resolve(scenario))
    if engine:
        from foood.engine import make_engine

        make_engine(engine)  # fail early on a typo
        os.environ["FOOOD_ENGINE"] = engine


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 3000,
    reload: bool = False,
    scenario: str | None = typer.Option(None, help=SCENARIO_HELP),
    engine: str | None = typer.Option(None, help=ENGINE_HELP),
) -> None:
    """Run the API on the port the Delivery Plus apps expect (3000)."""
    _select(scenario, engine)
    uvicorn.run("foood.app:create_app", host=host, port=port, reload=reload, factory=True)


@app.command()
def up(
    host: str = "127.0.0.1",
    port: int = 3000,
    apps: bool = typer.Option(True, help="Export and serve the three Delivery Plus apps."),
    rebuild: bool = typer.Option(False, help="Re-export the apps even when the build is current."),
    scenario: str | None = typer.Option(None, help=SCENARIO_HELP),
    engine: str | None = typer.Option(None, help=ENGINE_HELP),
) -> None:
    """Start the API with the demo seed and the customer, driver and restaurant apps."""
    from foood.launch import up as launch

    _select(scenario, engine)
    launch(host, port, apps, rebuild)


@app.command()
def bench(
    scenario: str = typer.Option(..., help=SCENARIO_HELP),
    engine: str = typer.Option("hand-wired", help=ENGINE_HELP),
    seed: int = 7,
    repeat: int = typer.Option(
        1, help="Run the same scenario, engine and seed this many times (results/repeats/)."
    ),
) -> None:
    """Run a scenario headless against an engine; writes a results/*.json file."""
    from foood.bench import repeat as repeat_runs
    from foood.bench import run

    if repeat > 1:
        repeated = repeat_runs(scenario, engine, seed, repeat)
        spread = repeated["distribution"]
        typer.echo(
            f"{repeated['scenario']} / {engine} / seed {seed} x{repeat}: "
            f"delivered {spread['delivered']}, decision calls {spread['llmCalls']}, "
            f"identical: {'yes' if spread['identical'] else 'no'} -> {repeated['path']}"
        )
        return
    result = run(scenario, engine, seed)
    summary = result["summary"]
    typer.echo(
        f"{result['scenario']} / {engine} / seed {seed}: "
        f"{summary['delivered']}/{summary['orders']} delivered, "
        f"{summary['deliveredWithinPromise']} within promise, "
        f"{summary['consistent']} consistent, "
        f"not recovered: {', '.join(summary['unrecovered']) or 'none'} -> {result['path']}"
    )


if __name__ == "__main__":
    app()


@app.command()
def ask(
    text: str = typer.Argument(..., help="The order, in a sentence."),
    api: str = typer.Option("http://localhost:3000", help="A running foood API."),
    email: str = typer.Option("demo.customer@example.com", help="The customer to order as."),
    password: str = typer.Option("password123"),
) -> None:
    """Order in a sentence through the running API's model intake (--engine llm)."""
    import httpx

    from foood.money import dollars

    session = httpx.post(f"{api}/api/auth/login", json={"email": email, "password": password})
    if session.status_code != 200:
        typer.echo(f"sign-in failed: {session.text}", err=True)
        raise typer.Exit(1)
    headers = {"Authorization": f"Bearer {session.json()['accessToken']}"}
    reply = httpx.post(
        f"{api}/api/orders/intake", json={"text": text}, headers=headers, timeout=120
    )
    body = reply.json()
    if reply.status_code != 200:
        typer.echo(f"{body.get('error')}: {body.get('message')}", err=True)
        raise typer.Exit(1)
    if body["status"] == "question":
        typer.echo(f"? {body['question']}")
        return
    order = body["order"]
    items = ", ".join(f"{i['quantity']}x {i['name']}" for i in order["items"])
    typer.echo(
        f"placed {order['id'][:8]}: {items} for {dollars(order['totalAmount'])}, "
        f"promised by {order['promisedAt']} ({body['usage']['calls']} model calls, "
        f"{body['usage']['tokens']} tokens)"
    )

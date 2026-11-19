"""Command-line entry points: ``foood serve`` runs the API, ``foood up`` the whole demo."""

from __future__ import annotations

import os

import typer
import uvicorn

from foood.money import dollars

app = typer.Typer(no_args_is_help=True, add_completion=False)


SCENARIO_HELP = "A bundled scenario name (see scenarios/) or a path to a TOML file."
ENGINE_HELP = "The order engine that decides the lifecycle (see foood/engine.py)."


def _load_dotenv() -> None:
    """KEY=VALUE lines from a .env in the working directory or the repo, never overriding."""
    from pathlib import Path

    for folder in (Path.cwd(), Path(__file__).resolve().parents[1]):
        dotenv = folder / ".env"
        if dotenv.is_file():
            for line in dotenv.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip("'\""))
            return


def _select(scenario: str | None, engine: str | None) -> None:
    _load_dotenv()
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


@app.command(name="bench-all")
def bench_all(
    engines: str = typer.Option(
        "hand-wired,llm,langgraph,langgoap", help="Comma-separated engines."
    ),
    seeds: str = typer.Option("7,11,13", help="Comma-separated seeds."),
    repeats: int = typer.Option(
        10, help="The incident evening this many times per engine (0 skips)."
    ),
    matrix_models: bool = typer.Option(
        True, "--matrix/--no-matrix", help="The langgraph engine with one model in every node."
    ),
    suggest: bool = typer.Option(
        True, "--suggest/--no-suggest", help="The substitution fixture, once per matrix model."
    ),
) -> None:
    """Every bundled scenario against every engine under every seed, the incident evening
    repeated, the model matrix and the substitution fixture; then the tables."""
    import subprocess
    import sys

    from foood.bench import MATRIX_MODELS, matrix, model_matrix, repeats_all

    _load_dotenv()
    chosen = engines.split(",")
    results = matrix(engines=chosen, seeds=[int(s) for s in seeds.split(",")])
    for result in results:
        summary = result["summary"]
        typer.echo(
            f"{result['scenario']:17} {result['engine']:11} seed {result['seed']:>2}: "
            f"{summary['delivered']}/{summary['orders']} delivered, "
            f"{summary['consistent']} consistent, llm {summary['llmCalls']}, "
            f"not recovered: {', '.join(summary['unrecovered']) or 'none'}"
        )
    if repeats > 1:
        for one in repeats_all(engines=chosen, times=repeats):
            typer.echo(
                f"repeats {one['engine']:11} x{one['times']}: identical "
                f"{'yes' if one['distribution']['identical'] else 'no'}"
            )
    if matrix_models and os.environ.get("OPENAI_API_KEY"):
        for result in model_matrix():
            summary = result["summary"]
            typer.echo(
                f"matrix {result['modelMatrix']:13}: {summary['delivered']}/{summary['orders']} "
                f"delivered, llm {summary['llmCalls']}, cost {summary['cost']}"
            )
    if suggest and os.environ.get("OPENAI_API_KEY"):
        from foood.suggest import OpenAISuggester
        from foood.suggest_eval import evaluate, write

        for model in MATRIX_MODELS:
            scored = evaluate(OpenAISuggester(model=model), model)
            write(scored)
            typer.echo(f"substitutes {model:13}: {scored['passed']}/{scored['cases']} acceptable")
    subprocess.run([sys.executable, "scripts/bench_table.py"], check=True)


@app.command()
def ask(
    text: str = typer.Argument(..., help="The order, in a sentence."),
    api: str = typer.Option("http://localhost:3000", help="A running foood API."),
    email: str = typer.Option("demo.customer@example.com", help="The customer to order as."),
    password: str = typer.Option("password123"),
) -> None:
    """Order in a sentence through the running API's LLM intake."""
    import httpx

    session = httpx.post(f"{api}/api/auth/login", json={"email": email, "password": password})
    if session.status_code != 200:
        typer.echo(f"sign-in failed: {session.text}", err=True)
        raise typer.Exit(1)
    headers = {"Authorization": f"Bearer {session.json()['accessToken']}"}
    reply = httpx.post(f"{api}/api/orders/intake", json={"text": text}, headers=headers, timeout=60)
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
        f"promised by {order['promisedAt']} (tokens: {body['usage'].get('tokens')})"
    )


if __name__ == "__main__":
    app()

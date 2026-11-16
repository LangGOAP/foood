"""Command-line entry points: ``foood serve`` runs the API, ``foood up`` the whole demo."""

from __future__ import annotations

import os

import typer
import uvicorn

app = typer.Typer(no_args_is_help=True, add_completion=False)


SCENARIO_HELP = "A bundled scenario name (see scenarios/) or a path to a TOML file."


def _select_scenario(scenario: str | None) -> None:
    if scenario:
        from foood.scenario import resolve

        os.environ["FOOOD_SCENARIO"] = str(resolve(scenario))


@app.command()
def serve(
    host: str = "127.0.0.1",
    port: int = 3000,
    reload: bool = False,
    scenario: str | None = typer.Option(None, help=SCENARIO_HELP),
) -> None:
    """Run the API on the port the Delivery Plus apps expect (3000)."""
    _select_scenario(scenario)
    uvicorn.run("foood.app:create_app", host=host, port=port, reload=reload, factory=True)


@app.command()
def up(
    host: str = "127.0.0.1",
    port: int = 3000,
    apps: bool = typer.Option(True, help="Export and serve the three Delivery Plus apps."),
    rebuild: bool = typer.Option(False, help="Re-export the apps even when the build is current."),
    scenario: str | None = typer.Option(None, help=SCENARIO_HELP),
) -> None:
    """Start the API with the demo seed and the customer, driver and restaurant apps."""
    from foood.launch import up as launch

    _select_scenario(scenario)
    launch(host, port, apps, rebuild)


if __name__ == "__main__":
    app()

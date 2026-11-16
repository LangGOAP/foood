"""Command-line entry points: ``foood serve`` runs the API, ``foood up`` the whole demo."""

from __future__ import annotations

import typer
import uvicorn

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 3000, reload: bool = False) -> None:
    """Run the API on the port the Delivery Plus apps expect (3000)."""
    uvicorn.run("foood.app:create_app", host=host, port=port, reload=reload, factory=True)


@app.command()
def up(
    host: str = "127.0.0.1",
    port: int = 3000,
    apps: bool = typer.Option(True, help="Export and serve the three Delivery Plus apps."),
    rebuild: bool = typer.Option(False, help="Re-export the apps even when the build is current."),
) -> None:
    """Start the API with the demo seed and the customer, driver and restaurant apps."""
    from foood.launch import up as launch

    launch(host, port, apps, rebuild)


if __name__ == "__main__":
    app()

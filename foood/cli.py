"""Command-line entry point: ``foood serve`` runs the API."""

from __future__ import annotations

import typer
import uvicorn

app = typer.Typer(no_args_is_help=True, add_completion=False)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 3000, reload: bool = False) -> None:
    """Run the API on the port the Delivery Plus apps expect (3000)."""
    uvicorn.run("foood.app:create_app", host=host, port=port, reload=reload, factory=True)


if __name__ == "__main__":
    app()

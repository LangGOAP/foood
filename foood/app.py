"""FastAPI application factory.

The service implements the subset of the Delivery Plus public API that the
Delivery Plus customer, driver and restaurant apps call, so those apps run
against it unchanged.
"""

from __future__ import annotations

from fastapi import FastAPI

from foood import __version__


def create_app() -> FastAPI:
    app = FastAPI(title="foood", version=__version__, docs_url="/docs")

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    return app

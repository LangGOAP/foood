"""FastAPI application factory.

The service implements the subset of the Delivery Plus public API that the
Delivery Plus customer, driver and restaurant apps call, so those apps run
against it unchanged.
"""

from __future__ import annotations

from fastapi import FastAPI

from foood import __version__
from foood.auth import router as auth_router
from foood.errors import install_error_handlers
from foood.store import Store, seed_demo


def create_app(store: Store | None = None) -> FastAPI:
    app = FastAPI(title="foood", version=__version__, docs_url="/docs")
    app.state.store = store if store is not None else seed_demo(Store())
    install_error_handlers(app)
    app.include_router(auth_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    return app

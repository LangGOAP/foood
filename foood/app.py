"""FastAPI application factory.

The service implements the subset of the Delivery Plus public API that the
Delivery Plus customer, driver and restaurant apps call, so those apps run
against it unchanged.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from foood import __version__
from foood.auth import router as auth_router
from foood.cart import router as cart_router
from foood.catalog import router as catalog_router
from foood.credits import router as credits_router
from foood.deliveries import router as deliveries_router
from foood.drivers import router as drivers_router
from foood.engine import OrderEngine, make_engine
from foood.engine import router as engine_router
from foood.errors import install_error_handlers
from foood.intake import IntakeAdvisor, intake_from_env
from foood.intake import router as intake_router
from foood.mapview import router as map_router
from foood.messages import Phraser, phraser_from_env
from foood.messages import install as install_phraser
from foood.notifications import install as install_notifications
from foood.notifications import router as notifications_router
from foood.orders import router as orders_router
from foood.payments import router as payments_router
from foood.replacements import router as replacements_router
from foood.scenario import Scenario, load
from foood.scenario import install as install_scenario
from foood.scenario import router as scenario_router
from foood.store import Store, seed_demo
from foood.support import install as install_support
from foood.support import router as support_router
from foood.tracking import install as install_tracking
from foood.tracking import router as tracking_router
from foood.tracking import run_simulator
from foood.world import install as install_world
from foood.world import router as world_router
from foood.world_mcp import build_server, http_app


def create_app(
    store: Store | None = None,
    simulate: bool = True,
    scenario: Scenario | None = None,
    engine: OrderEngine | None = None,
    intake: IntakeAdvisor | None = None,
    phraser: Phraser | None = None,
) -> FastAPI:
    """Build the service.

    ``engine`` decides the order lifecycle (``FOOOD_ENGINE``, default hand-wired);
    ``simulate`` runs the driver-phone simulator (and the scenario timers) while it
    serves; ``scenario`` injects failures, defaulting to ``FOOOD_SCENARIO`` when set.
    """

    the_store = store if store is not None else seed_demo(Store())
    world_mcp = http_app(build_server(the_store))

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with world_mcp.router.lifespan_context(world_mcp):  # the MCP transport's own lifespan
            task = asyncio.create_task(run_simulator(app.state.store)) if simulate else None
            try:
                yield
            finally:
                if task is not None:
                    task.cancel()

    app = FastAPI(title="foood", version=__version__, docs_url="/docs", lifespan=lifespan)
    app.state.store = the_store
    app.mount("/mcp", world_mcp)  # the world as MCP tools, for any client (and the model tier)
    app.add_middleware(  # the apps run as web builds on other local ports
        CORSMiddleware,
        allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # listeners run in subscription order: the customer hears about a stage before its side effects
    install_notifications(app.state.store)
    install_world(app.state.store)
    app.state.engine = engine or make_engine(os.environ.get("FOOOD_ENGINE", "hand-wired"))
    app.state.engine.install(app.state.store)
    app.state.intake = intake or intake_from_env()
    chosen_phraser = phraser or phraser_from_env()
    if chosen_phraser is not None:
        install_phraser(app.state.store, chosen_phraser)
    install_tracking(app.state.store)
    install_support(app.state.store)
    if scenario is None and os.environ.get("FOOOD_SCENARIO"):
        scenario = load(os.environ["FOOOD_SCENARIO"])
    if scenario is not None:
        install_scenario(app.state.store, scenario)
    install_error_handlers(app)
    app.include_router(auth_router)
    app.include_router(catalog_router)
    app.include_router(cart_router)
    app.include_router(orders_router)
    app.include_router(payments_router)
    app.include_router(drivers_router)
    app.include_router(deliveries_router)
    app.include_router(tracking_router)
    app.include_router(notifications_router)
    app.include_router(scenario_router)
    app.include_router(engine_router)
    app.include_router(support_router)
    app.include_router(map_router)
    app.include_router(world_router)
    app.include_router(replacements_router)
    app.include_router(credits_router)
    app.include_router(intake_router)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    return app

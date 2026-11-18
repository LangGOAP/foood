"""The world as an MCP server (issue #43).

The read tools (``get_weather``, ``get_traffic``, ``eta``, ``forecast``, ``zones``) are what a
model may be given; the control tools (``set_weather``, ``set_traffic``, ``close_road``,
``schedule``) are for the scenario, the operator and any MCP client you point at ``/mcp``
to create a decision point by hand. Both read and move the same state as ``foood.world``.

The server is mounted on the API at ``/mcp`` (streamable HTTP, stateless) so Claude Desktop,
the MCP Inspector or a script can connect; in the bench it is served in-process. A model never
sees the control tools: ``read_tool_specs`` is the allow-list.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette

from foood import world
from foood.store import Store

logging.getLogger("mcp").setLevel(logging.WARNING)

INSTRUCTIONS = (
    "The weather, the traffic and the road closures of the city foood delivers in. Zones are "
    "named areas; a route is judged by the zones it joins. Read tools answer from the current "
    "state; control tools change it, now or on a schedule."
)


def build_server(store: Store) -> MCPServer:
    """An MCP server over this store's world."""
    # the default INFO would reconfigure the root logger with a rich handler, and every library
    # line in the process (the planner's, inside its timed rounds) would be rendered through it
    server = MCPServer("foood-world", instructions=INSTRUCTIONS, log_level="WARNING")
    tools = world.WorldTools(store)

    @server.tool()
    def zones() -> list[dict[str, Any]]:
        """The city's zones with their centres."""
        return tools.zones()

    @server.tool()
    def get_weather(zone: str) -> dict[str, Any]:
        """The weather in a zone now (clear, rain, storm, heat) and until when, if set to pass."""
        return tools.get_weather(zone)

    @server.tool()
    def get_traffic(
        from_latitude: float, from_longitude: float, to_latitude: float, to_longitude: float
    ) -> dict[str, Any]:
        """Traffic on a route: delay factor against a free-flowing day, closures, minutes."""
        return tools.get_traffic((from_latitude, from_longitude), (to_latitude, to_longitude))

    @server.tool()
    def eta(
        from_latitude: float, from_longitude: float, to_latitude: float, to_longitude: float
    ) -> dict[str, Any]:
        """Minutes a courier needs for this route now, weather and traffic included."""
        return tools.eta((from_latitude, from_longitude), (to_latitude, to_longitude))

    @server.tool()
    def forecast(zone: str, minutes: int = 30) -> list[dict[str, Any]]:
        """Changes scheduled for a zone within the next minutes, soonest first."""
        return tools.forecast(zone, minutes)

    @server.tool()
    def set_weather(zone: str, kind: str, for_seconds: float | None = None) -> dict[str, Any]:
        """Set a zone's weather (clear, rain, storm, heat), optionally for a while only."""
        return world.set_weather(store, zone, kind, for_seconds)

    @server.tool()
    def set_traffic(zone: str, factor: float, for_seconds: float | None = None) -> dict[str, Any]:
        """Set a zone's traffic delay factor (1.0 free flowing, 2.0 twice as slow), for a while
        when for_seconds is given."""
        return world.set_traffic(store, zone, factor, for_seconds)

    @server.tool()
    def close_road(
        corridor: str, closed: bool = True, for_seconds: float | None = None
    ) -> dict[str, Any]:
        """Close (or reopen) a corridor between two zones; routes between them become a detour."""
        return world.close_road(store, corridor, closed, for_seconds)

    @server.tool()
    def schedule(after_seconds: float, change: dict[str, Any]) -> dict[str, Any]:
        """Apply a change later: {"weather": {"zone", "kind"}}, {"traffic": {"zone", "factor"}}
        or {"road": {"corridor", "closed"}}; each may carry "for_seconds"."""
        return world.schedule(store, after_seconds, change)

    return server


def http_app(server: MCPServer) -> Starlette:
    """The streamable HTTP app to mount at /mcp; local hosts only, like the rest of the demo."""
    security = TransportSecuritySettings(
        allowed_hosts=["localhost:*", "127.0.0.1:*", "test"],
        allowed_origins=["http://localhost:*", "http://127.0.0.1:*"],
    )
    return server.streamable_http_app(
        streamable_http_path="/",
        stateless_http=True,
        json_response=True,
        transport_security=security,
    )


def _run(coro: Any) -> Any:
    """Run a coroutine from sync code, inside or outside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    box: dict[str, Any] = {}

    def runner() -> None:
        try:
            box["value"] = asyncio.run(coro)
        except BaseException as exc:  # surfaced to the caller
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box["value"]


def tool_specs(server: MCPServer, names: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Name, description and JSON schema of the server's tools, in the shape a model is given."""
    listed = _run(server.list_tools())
    specs = []
    for tool in listed:
        if names is not None and tool.name not in names:
            continue
        specs.append(
            {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            }
        )
    return specs


def read_tool_specs(server: MCPServer | None = None) -> list[dict[str, Any]]:
    """The tools a model may have: the read tools only, never the controls."""
    return tool_specs(server or build_server(Store()), world.READ_TOOLS)


def call_tool(server: MCPServer, name: str, arguments: dict[str, Any]) -> Any:
    """Call a tool through the server (what an MCP client does), from sync code."""
    result = _run(server.call_tool(name, arguments))
    return result

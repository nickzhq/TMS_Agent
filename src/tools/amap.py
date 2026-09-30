"""Amap MCP client: launch the server over stdio + call_tool maps_geo.

Code-driven call_tool (deterministic), not LLM tool-use.
Used to geocode an address into coordinates; results can be cached locally (lazy loading).
"""
from __future__ import annotations

import os
from contextlib import AsyncExitStack

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class AmapMCPClient:
    """Wraps the amap-maps MCP server connection and geocoding calls (async context manager)."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "AmapMCPClient":
        params = StdioServerParameters(
            command="npx",
            args=["-y", "@amap/amap-maps-mcp-server"],
            env={**os.environ, "AMAP_MAPS_API_KEY": self.api_key},
        )
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._stack.aclose()

    async def maps_geo(self, address: str, city: str | None = None):
        """Geocoding: address -> longitude/latitude / adcode / level."""
        if self._session is None:
            raise RuntimeError("enter the `async with` context before calling")
        args: dict = {"address": address}
        if city:
            args["city"] = city
        return await self._session.call_tool("maps_geo", args)

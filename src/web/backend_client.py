"""Backend client for the Chainlit frontend (P8 front/back split).

Thin wrappers over the FastAPI Agent backend, so the frontend no longer runs the graph in-process:
  - login()      -> POST /auth/login (sync httpx; Chainlit's password_auth_callback is sync)
  - BackendWS    -> an authenticated WebSocket driving one inquiry (query/resume <-> streamed msgs)
  - SessionsREST -> the /sessions REST calls the history sidebar's data layer needs (async httpx)

Generic glue (no business logic) -> committed.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

# websockets >=13 moved the client; fall back to the legacy top-level connect for older versions.
try:
    from websockets.asyncio.client import connect as _ws_connect
except ImportError:  # pragma: no cover - depends on the installed websockets version
    from websockets import connect as _ws_connect

logger = logging.getLogger("estimate.web.backend")


def login(base_url: str, username: str, password: str, timeout: float = 10.0) -> dict | None:
    """POST /auth/login -> {access_token, uid, username}; None on bad creds / unreachable backend."""
    try:
        r = httpx.post(f"{base_url.rstrip('/')}/auth/login",
                       json={"username": username, "password": password}, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        logger.warning("backend login rejected: HTTP %s", r.status_code)
    except httpx.HTTPError as exc:
        logger.warning("backend login unreachable: %s", type(exc).__name__)
    return None


# In-process cache: Chainlit user identifier -> backend JWT. Chainlit CACHES the data layer as a
# singleton built lazily during login (before any session context exists) and rebuilds the user via
# data_layer.get_user(identifier) on each request, so a token captured at construction would be
# empty. We stash the backend JWT at login and resolve it per call by identifier. Single-process
# only — a frontend restart requires re-login (which re-populates this).
# uid -> backend JWT (keyed by user id, so it is multi-user safe).
_TOKEN_CACHE: dict[str, str] = {}
# thread_id -> backend JWT. Chainlit's data-layer thread/step methods run WITHOUT a session context
# (cl.context raises) and the data layer is a shared singleton, so they cannot ask "which user?" —
# they resolve the token by thread_id instead (bound at on_chat_start/resume, where the context IS
# available). thread_ids are unique per session -> no cross-user leakage. This REPLACES the old
# global "latest token" fallback, which borrowed the last login's token and leaked across users.
_THREAD_TOKEN: dict[str, str] = {}


def cache_token(identifier: str, token: str) -> None:
    """Stash the backend JWT for a Chainlit user identifier (called at login)."""
    if identifier:
        _TOKEN_CACHE[identifier] = token


def cached_token(identifier: str) -> str:
    """The stashed backend JWT for a user identifier ('' if none)."""
    return _TOKEN_CACHE.get(identifier, "")


def bind_thread_token(thread_id: str, token: str) -> None:
    """Bind a thread to its owner's JWT (called at on_chat_start/resume, where the session context
    IS available), so context-less data-layer calls can resolve it by thread_id later."""
    if thread_id and token:
        _THREAD_TOKEN[thread_id] = token


def thread_token(thread_id: str) -> str:
    """The JWT bound to a thread ('' if none -> the caller fails CLOSED with a 401, never borrows
    another user's token)."""
    return _THREAD_TOKEN.get(thread_id, "")


def single_cached_token() -> str:
    """The one cached login token, or '' when there are zero or several.

    Needed by the resume path: Chainlit calls data_layer.get_thread() BEFORE on_chat_resume (where the
    thread token is bound) and it runs the data layer WITHOUT a session context, so the FIRST open of a
    thread used to resolve no token at all -> 401 -> silent None -> the UI spun forever. With exactly
    one logged-in account its token is unambiguous; with several we still fail closed.
    """
    tokens = {t for t in _TOKEN_CACHE.values() if t}
    return next(iter(tokens)) if len(tokens) == 1 else ""


class BackendWS:
    """Authenticated WebSocket to the backend for one thread (async context manager).

    The backend pushes {greeting,message,synthesis,interrupt,done,error}; the frontend sends
    {query,resume}. Mirrors src/api/ws.py's protocol.
    """

    def __init__(self, ws_url: str, thread_id: str, token: str):
        self.url = f"{ws_url.rstrip('/')}/ws/{thread_id}?token={token}"
        self._ws: Any = None

    async def __aenter__(self) -> "BackendWS":
        # max_size=None: a synthesis payload (Top-15 detail table) can exceed the default frame cap.
        self._ws = await _ws_connect(self.url, max_size=None)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def send_query(self, text: str) -> None:
        await self._ws.send(json.dumps({"type": "query", "text": text}))

    async def send_resume(self, value: Any) -> None:
        await self._ws.send(json.dumps({"type": "resume", "value": value}))

    async def recv(self) -> dict:
        """Next backend message as a dict (raises on disconnect -> the caller surfaces it)."""
        return json.loads(await self._ws.recv())


class SessionsREST:
    """Async /sessions client for the frontend's history sidebar (Bearer-authenticated)."""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self.base_url = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.timeout = timeout

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base_url, headers=self.headers, timeout=self.timeout)

    async def list(self) -> list[dict]:
        async with self._client() as c:
            r = await c.get("/sessions")
            r.raise_for_status()
            return r.json()

    async def get(self, thread_id: str) -> dict | None:
        async with self._client() as c:
            r = await c.get(f"/sessions/{thread_id}")
            if r.status_code == 404:
                return None
            r.raise_for_status()
            return r.json()

    async def rename(self, thread_id: str, title: str) -> None:
        async with self._client() as c:
            r = await c.patch(f"/sessions/{thread_id}", json={"title": title})
            r.raise_for_status()

    async def ensure(self, thread_id: str, title: str = "") -> None:
        """Create the thread row (idempotent) on the FIRST real interaction.

        `title` is used only when the row is new (e.g. the first user sentence), so the chat shows
        up in the history list immediately; an existing title is never overwritten.
        """
        async with self._client() as c:
            r = await c.post(f"/sessions/{thread_id}/ensure", json={"title": title})
            r.raise_for_status()

    async def delete(self, thread_id: str) -> None:
        async with self._client() as c:
            r = await c.delete(f"/sessions/{thread_id}")
            r.raise_for_status()

    async def add_step(self, thread_id: str, step: dict) -> None:
        """Persist one Chainlit StepDict to the backend (single source of truth for history)."""
        async with self._client() as c:
            r = await c.post(f"/sessions/{thread_id}/steps", json={"step": step})
            r.raise_for_status()

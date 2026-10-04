"""WebSocket endpoint (P8): drive one inquiry over a socket, bridging HITL across processes.

Reuses the UI-agnostic run_flow (src/web/graph_runner): its `respond` callback becomes a
WSResponder that pushes the interrupt payload to the frontend and awaits the resume value the
frontend sends back. The frontend classifies/renders/parses (web/hitl.py) — the backend only
relays structured payloads. The socket is authenticated via ?token=<JWT> (see ws_uid) and thread_id
must embed the caller's uid.

Protocol: server->client {greeting,message,synthesis,interrupt,done,error};
client->server {query,resume,close}. Generic glue -> committed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from decimal import Decimal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.api.deps import get_app, get_memory, ws_uid
from src.graph.build import build_graph, default_config
from src.tools.mssql import MssqlClient, build_connection_string
from src.web.graph_runner import run_flow
from src.web.hitl import classify_interrupt, FOLLOWUP

logger = logging.getLogger("estimate.api.ws")
router = APIRouter()


def _json_default(o):
    """JSON fallback: DB values arrive as Decimal (prices/mileage/tolls) which the stdlib encoder
    rejects; render them as float. Anything else exotic (datetime/bytes) degrades to str."""
    if isinstance(o, Decimal):
        return float(o)
    return str(o)


async def _send(ws: WebSocket, data: dict) -> None:
    """Send as JSON with a Decimal-tolerant encoder. Starlette's send_json uses the stdlib default
    and raises TypeError on Decimal — and the synthesis payload carries DB decimals."""
    await ws.send_text(json.dumps(data, ensure_ascii=False, default=_json_default))


_ASK_TIMEOUT = 1800   # seconds to wait for a HITL reply (looking up mileage on the phone takes a while)
_RECALL_QUERY = "报价偏好：常发货主、常走线路、进出口方向、常用箱型柜型拖型"
_BG: set = set()      # keep refs to fire-and-forget learn tasks (a done-callback drops them)


class WSResponder:
    """run_flow's `respond` callback over a WebSocket: push the interrupt, await the resume value.

    The frontend renders the Ask* UI and parses the user's reply (web/hitl.py), then sends
    {"type":"resume","value":<truthy>}; the receive loop resolves self._pending with it.
    A truthy resume is required (run_flow refuses a falsy one -> HITL death loop).
    """

    def __init__(self, ws: WebSocket):
        self.ws = ws
        self._pending: asyncio.Future | None = None

    async def __call__(self, payload, state):
        # At the followup interrupt the three-price result is ready -> push it for rendering first.
        if classify_interrupt(payload) == FOLLOWUP:
            syn = state.get("synthesis") if isinstance(state, dict) else None
            if syn:
                await _send(self.ws, {"type": "synthesis", "data": syn})
        await _send(self.ws, {"type": "interrupt", "payload": payload})
        loop = asyncio.get_running_loop()
        self._pending = loop.create_future()
        try:
            return await asyncio.wait_for(self._pending, timeout=_ASK_TIMEOUT)
        except asyncio.TimeoutError:
            return "结束"          # a stalled HITL counts as "end" (truthy) so run_flow can finish
        finally:
            self._pending = None

    def resolve(self, value) -> None:
        """Called by the receive loop when the frontend sends a resume value."""
        if self._pending is not None and not self._pending.done():
            self._pending.set_result(value)


def _recall_note(uid: str) -> str:
    """mem0 preference recall -> a short greeting note ('' on any issue; never breaks connect)."""
    try:
        hits = get_memory().search(uid, _RECALL_QUERY, top_k=3)
    except Exception:
        logger.warning("mem0 recall failed", exc_info=True)
        return ""
    facts = [str(h.get("memory", "")).strip() for h in hits if isinstance(h, dict) and h.get("memory")]
    facts = [f for f in facts if f][:3]
    return ("（我记得你之前的偏好：" + "；".join(facts) + "）") if facts else ""


def _spawn_learn(uid: str, text: str) -> None:
    """Fire preference learning in a worker thread (guarded, non-blocking)."""
    async def _bg():
        try:
            await asyncio.to_thread(get_memory().add, uid, f"用户询价：{text}")
        except Exception:
            logger.warning("mem0 add failed", exc_info=True)
    t = asyncio.create_task(_bg())
    _BG.add(t)
    t.add_done_callback(_BG.discard)


async def _run_flow_task(websocket: WebSocket, graph, config, text: str, responder: WSResponder) -> None:
    """Run one inquiry to completion; the synthesis + interrupts are pushed by the responder.

    run_flow's `emit` progress hook is deliberately NOT wired to the socket: it emits internal
    debug events — the interrupt ask text (the responder already pushes it -> would duplicate) and
    the resume value's TYPE NAME ('dict'/'list'/'bool' -> would show as stray messages). P7's
    in-process Chainlit also ran run_flow without emit; keep the socket clean the same way."""
    try:
        await run_flow(graph, config, text, responder)
        await _send(websocket, {"type": "done"})
    except Exception as exc:
        logger.exception("run_flow failed")            # full traceback -> server log
        with contextlib.suppress(Exception):
            await _send(websocket, {"type": "error", "detail": type(exc).__name__})  # user sees only the type


@router.websocket("/ws/{thread_id}")
async def ws_inquiry(websocket: WebSocket, thread_id: str):
    await websocket.accept()
    uid = ws_uid(websocket)               # JWT from ?token= (browsers can't set WS headers)
    if not uid:
        await websocket.close(code=4401)  # unauthorized
        return
    app = get_app()
    existing = await app["sessions"].get(thread_id)
    if existing and existing.get("user_id") and existing.get("user_id") != uid:
        await websocket.close(code=4403)  # this thread belongs to another user
        return
    responder = WSResponder(websocket)
    db_stack = contextlib.ExitStack()
    graph = None
    flow_task: asyncio.Task | None = None

    with contextlib.suppress(Exception):
        await _send(websocket, {"type": "greeting", "text": _recall_note(uid)})

    try:
        while True:
            msg = await websocket.receive_json()
            mtype = msg.get("type")
            if mtype == "query":
                if graph is None:         # open this socket's DB conn + compile the graph once
                    s = app["settings"]
                    db = db_stack.enter_context(MssqlClient(build_connection_string(s.db)))
                    graph = build_graph(app["llm"], db, amap=app["amap"], checkpointer=app["checkpointer"])
                text = (msg.get("text") or "").strip()
                row = await app["sessions"].get(thread_id)
                if not row or not row.get("title"):    # first message -> title the thread + learn prefs
                    await app["sessions"].upsert_thread(thread_id, uid, text.replace("\n", " ")[:24] or "询价")
                    if text:
                        _spawn_learn(uid, text)
                config = default_config(thread_id)
                flow_task = asyncio.create_task(_run_flow_task(websocket, graph, config, text, responder))
            elif mtype == "resume":
                responder.resolve(msg.get("value"))
            elif mtype == "close":
                break
    except WebSocketDisconnect:
        logger.info("ws disconnected thread=%s", thread_id[:12])
    except Exception:
        logger.exception("ws error thread=%s", thread_id[:12])
        with contextlib.suppress(Exception):
            await _send(websocket, {"type": "error", "detail": "internal error"})
    finally:
        if flow_task is not None and not flow_task.done():
            flow_task.cancel()
        db_stack.close()

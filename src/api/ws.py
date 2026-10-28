"""WebSocket endpoint (P8): drive one inquiry over a socket, bridging HITL across processes.

Reuses the UI-agnostic run_flow (src/web/graph_runner): its `respond` callback becomes a
WSResponder that pushes the interrupt payload to the frontend and awaits the resume value the
frontend sends back. The frontend classifies/renders/parses (web/hitl.py) — the backend only
relays structured payloads. The socket is authenticated via ?token=<JWT> (see ws_uid) and thread_id
must embed the caller's uid.

Protocol: server->client {greeting,message,synthesis,interrupt,progress,bill,bill_close,chart(base64),done,error};
client->server {query,resume,close}. Generic glue -> committed.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from decimal import Decimal

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from src.api.deps import get_app, get_memory, ws_uid
from src.graph.build import build_graph, default_config
from src.tools.mssql import MssqlClient, build_connection_string
from src.web.graph_runner import run_flow
from src.web.hitl import classify_interrupt, FOLLOWUP, BILL_CLOSE_CONFIRM

logger = logging.getLogger("estimate.api.ws")
router = APIRouter()


def _is_uuid(value: str) -> bool:
    """Chainlit thread ids are UUIDs. Test clients may use arbitrary ids; creating a history row
    for them leaves a "ghost" chat (a title but no steps) that cannot be opened."""
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


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
        # Dedupe by CONTENT within ONE run: a single run can push the bill summary at the
        # close-confirm interrupt and the same markdown again at the end -> push once.
        # It MUST be reset at the start of every run (begin_run): with a socket-lifetime memo
        # an identical later reply was swallowed too (ask the same period query twice ->
        # second reply never rendered; observed 2026-10-22).
        self.bill_sent_md = ""

    def begin_run(self) -> None:
        """Reset the per-run push memo (called once in _run_flow_task before run_flow)."""
        self.bill_sent_md = ""

    async def __call__(self, payload, state):
        # At the followup interrupt the three-price result is ready -> push it for rendering first.
        if classify_interrupt(payload) == FOLLOWUP:
            syn = state.get("synthesis") if isinstance(state, dict) else None
            if syn:
                await _send(self.ws, {"type": "synthesis", "data": syn})
        # 封账询问时账单摘要已就绪 -> 先推摘要（用户看到账再决定封不封）
        if classify_interrupt(payload) == BILL_CLOSE_CONFIRM:
            await self.push_bill(state)
        await _send(self.ws, {"type": "interrupt", "payload": payload})
        loop = asyncio.get_running_loop()
        self._pending = loop.create_future()
        try:
            return await asyncio.wait_for(self._pending, timeout=_ASK_TIMEOUT)
        except asyncio.TimeoutError:
            return "结束"          # a stalled HITL counts as "end" (truthy) so run_flow can finish
        finally:
            self._pending = None

    async def push_bill(self, state) -> bool:
        """Push the bill / profit markdown; identical content is not repeated."""
        if not isinstance(state, dict):
            return False
        # 本轮域优先：发票主数据 > 对账单 > 利润表 > 解释 > 账单 > 利润
        md = (state.get("invoice_md") or state.get("wt_render_md")
              or state.get("statement_render_md") or state.get("explain_md")
              or state.get("bill_render_md") or state.get("profit_render_md"))
        if not md or md == self.bill_sent_md:
            return False
        await _send(self.ws, {"type": "bill", "data": {"markdown": md}})
        self.bill_sent_md = md
        return True

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


async def _pump_progress(ws: WebSocket, queue: asyncio.Queue) -> None:
    """把出账进度从队列推到前端（每条覆盖上一条，避免刷屏）。

    出账耗时以分钟计（取数 + 上百张凭证），没有进度反馈用户会以为卡死。
    """
    while True:
        text = await queue.get()
        with contextlib.suppress(Exception):
            await _send(ws, {"type": "progress", "text": str(text)})


async def _run_flow_task(websocket: WebSocket, graph, config, text: str, responder: WSResponder,
                         progress_queue: asyncio.Queue | None = None) -> None:
    """Run one inquiry to completion; the synthesis + interrupts are pushed by the responder.

    run_flow's `emit` progress hook is deliberately NOT wired to the socket: it emits internal
    debug events — the interrupt ask text (the responder already pushes it -> would duplicate) and
    the resume value's TYPE NAME ('dict'/'list'/'bool' -> would show as stray messages). P7's
    in-process Chainlit also ran run_flow without emit; keep the socket clean the same way.

    账单域的**业务进度**（出账到了哪一步）走独立的 progress_queue，与上面的 emit 是两回事。"""
    # The push memo lives for ONE run only: the same query asked twice must render twice.
    responder.begin_run()
    pump = None
    if progress_queue is not None:
        pump = asyncio.create_task(_pump_progress(websocket, progress_queue))
    try:
        state = await run_flow(graph, config, text, responder)
        # 摘要没推过时补推（如账期查询旁路：没有 interrupt，直接结束）
        await responder.push_bill(state)
        # 多月利润报表：趋势图（base64 PNG，不落盘）单独推给前端显示（协议新增 chart 消息）
        charts = (((state or {}).get("profit_result") or {}).get("charts") or [])
        for item in charts:
            if isinstance(item, dict) and item.get("b64"):
                with contextlib.suppress(Exception):
                    await _send(websocket, {"type": "chart", "data": item})
        close_md = (state or {}).get("bill_close_md")
        if close_md:
            await _send(websocket, {"type": "bill_close", "data": {"markdown": close_md}})
        await _send(websocket, {"type": "done"})
    except Exception as exc:
        logger.exception("run_flow failed")            # full traceback -> server log
        with contextlib.suppress(Exception):
            await _send(websocket, {"type": "error", "detail": type(exc).__name__})  # user sees only the type
    finally:
        if pump is not None:
            pump.cancel()


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
                # 账单域的出账进度队列（节点用 call_soon_threadsafe 投递到这里 -> 事件循环线程安全）
                progress_queue: asyncio.Queue = asyncio.Queue()

                def _sink(msg: str) -> None:
                    progress_queue.put_nowait(str(msg))
                row = await app["sessions"].get(thread_id)
                # Only Chainlit-style UUID threads become history rows: a non-UUID test thread would
                # otherwise show up as a chat with a title but zero steps (cannot be opened).
                if (row is None or not row.get("title")) and _is_uuid(thread_id):
                    await app["sessions"].upsert_thread(thread_id, uid, text.replace("\n", " ")[:24] or "询价")
                    if text:
                        _spawn_learn(uid, text)
                # Output folder uses the LOGIN NAME (readable: b32test), not the uid hash
                # (b4f6bbe8...). The JWT only carries uid, so resolve the username from the user
                # store; fall back to the uid so a lookup failure never blocks billing.
                bill_user = uid
                user_role = "viewer"          # fail-closed：拿不到角色就当只读
                with contextlib.suppress(Exception):
                    user_row = await app["users"].get_by_id(uid)
                    bill_user = (user_row or {}).get("username") or uid
                    user_role = (user_row or {}).get("role") or "viewer"
                config = default_config(thread_id, bill_progress=_sink,
                                        bill_user=bill_user, user_role=user_role)
                flow_task = asyncio.create_task(
                    _run_flow_task(websocket, graph, config, text, responder, progress_queue))
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

"""Chainlit frontend (P8 front/back split): a thin UI over the FastAPI Agent backend.

It no longer runs the graph in-process. Instead it: logs into the backend (real auth, replacing
the P7 mock), opens ONE authenticated WebSocket per chat, and renders what the backend streams —
interrupts become Chainlit Ask*/AskAction (parsed back via web/hitl.py), synthesis becomes Markdown
(web/render.py). The left history sidebar reads the backend's /sessions REST via HttpSessionsDataLayer.

The graph state / checkpointer / mem0 / DB all live in the BACKEND now; thread_id is Chainlit's own
(opaque UUID) and is the shared key across the WS, the checkpointer and the sessions store.

Run the backend first (uvicorn src.api.app:app), then: chainlit run src/web/chainlit_app.py
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

# Ensure the project root is importable when launched via `chainlit run src/web/chainlit_app.py`.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import chainlit as cl

from src.config.settings import load_settings
from src.web.backend_client import BackendWS, login, cache_token, cached_token, bind_thread_token
from src.web.hitl import (
    classify_interrupt, parse_mileage, parse_params_edit, candidate_label, fmt_params,
    PARAMS, AMBIGUITY, MILEAGE, FOLLOWUP,
)
from src.web.render import render_synthesis
from src.web.http_data_layer import HttpSessionsDataLayer

logger = logging.getLogger("estimate.web")
_SETTINGS = load_settings()
_ASK_TIMEOUT = 1800   # seconds to wait for a HITL reply (looking up mileage on the phone takes a while)


# ---- auth + data layer ----

@cl.password_auth_callback
def password_auth_callback(username: str, password: str):
    """Real auth (P8): forward credentials to the backend /auth/login. On success, stash the backend
    JWT + uid in the Chainlit user's metadata (the WS/REST clients present it). None -> login fails.
    Chainlit still signs its own session cookie with CHAINLIT_AUTH_SECRET (two tokens, see plan)."""
    res = login(_SETTINGS.backend.base_url, username, password)
    if not res:
        return None
    # Stash the backend JWT by uid: Chainlit rebuilds the user via data_layer.get_user(identifier) on
    # later requests (dropping metadata) and caches the data layer as a singleton, so the token must
    # be resolvable by identifier — not captured once. See backend_client._TOKEN_CACHE.
    cache_token(res["uid"], res["access_token"])
    return cl.User(identifier=res["uid"], display_name=res.get("username") or res["uid"],
                   metadata={"token": res["access_token"]})


@cl.data_layer
def _data_layer():
    """Chainlit builds the data layer ONCE (a cached singleton), lazily — often during login auth,
    before any session context exists. So it must NOT capture a token here: HttpSessionsDataLayer
    resolves the backend token per call (current context user's metadata, else the login-time cache
    by identifier). create_user/get_user are local no-ops that need no token."""
    return HttpSessionsDataLayer(_SETTINGS.backend.base_url)


def _token() -> str:
    """The logged-in user's backend JWT (resolved by uid). Fails CLOSED ('' -> the backend returns
    401) rather than borrowing another user's token. cl.context RAISES with no session, so guard it."""
    try:
        user = cl.context.user
        if user is not None:
            return (user.metadata or {}).get("token", "") or cached_token(user.identifier)
    except Exception:
        pass
    return ""


async def _open_ws() -> BackendWS | None:
    """Open this chat's authenticated WS to the backend (thread_id = Chainlit's own). None on failure.

    Also binds thread_id -> token HERE (the session context is available), so the context-less
    data-layer thread/step calls can later resolve the right user's token by thread_id."""
    thread_id = getattr(getattr(cl.context, "session", None), "thread_id", None) or "anon"
    token = _token()
    bind_thread_token(thread_id, token)
    try:
        ws = await BackendWS(_SETTINGS.backend.ws_url, thread_id, token).__aenter__()
    except Exception as exc:
        logger.warning("backend WS connect failed: %s", type(exc).__name__)
        return None
    cl.user_session.set("ws", ws)
    cl.user_session.set("thread_id", thread_id)
    return ws


# ---- lifecycle ----

@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("flow_done", False)
    ws = await _open_ws()
    if ws is None:
        await cl.Message(content="无法连接后端服务，请先启动后端：uvicorn src.api.app:app").send()
        return
    note = ""
    try:
        greet = await ws.recv()          # backend sends a greeting (mem0 recall note) on connect
        note = greet.get("text", "") if isinstance(greet, dict) and greet.get("type") == "greeting" else ""
    except Exception:
        logger.warning("greeting recv failed", exc_info=True)
    await cl.Message(
        content="你好，我是物流报价助手。请描述一条线路，例如：天津港到汉沽的运费，进口，单拖。" + note
    ).send()


@cl.on_message
async def on_message(message: cl.Message):
    ws = cl.user_session.get("ws") or await _open_ws()
    if ws is None:
        await cl.Message(content="后端未连接，无法处理。请确认后端已启动后重试。").send()
        return
    try:
        await ws.send_query(message.content or "")
    except Exception:
        logger.exception("send_query failed")
        await cl.Message(content="发送失败：与后端的连接已断开。").send()
        return
    await _pump(ws)


@cl.on_chat_resume
async def on_chat_resume(thread):
    """Rehydrate a past thread: Chainlit re-renders the stored steps (via the data layer). We open a
    fresh WS so follow-ups continue this thread's backend state (checkpointer keyed by the same id).

    Known limitation (carried from P7): resuming mid-inquiry re-asks the pending question — the graph
    state IS checkpointed, so nothing is lost. Seamless mid-flow resume is a later P8 batch."""
    cl.user_session.set("flow_done", False)
    ws = await _open_ws()
    if ws is None:
        await cl.Message(content="无法连接后端服务。").send()
        return
    try:
        await ws.recv()                  # consume the greeting so the next pump starts clean
    except Exception:
        logger.warning("resume greeting recv failed", exc_info=True)


@cl.on_chat_end
async def on_chat_end():
    ws = cl.user_session.get("ws")
    if ws is not None:
        try:
            await ws.__aexit__(None, None, None)
        except Exception:
            pass
        cl.user_session.set("ws", None)


# ---- backend message pump + HITL rendering ----

async def _pump(ws: BackendWS) -> None:
    """Receive backend messages until done/error: render synthesis + progress, bridge interrupts."""
    while True:
        try:
            msg = await ws.recv()
        except Exception as exc:
            logger.warning("WS recv failed: %s", type(exc).__name__)
            await cl.Message(content="与后端的连接中断。").send()
            return
        t = msg.get("type")
        if t == "synthesis":
            await cl.Message(content=render_synthesis(msg.get("data") or {})).send()
        elif t == "message":
            txt = msg.get("text", "")
            if txt:
                await cl.Message(content=txt).send()
        elif t == "interrupt":
            resume_val = await _render_interrupt(msg.get("payload"))
            try:
                await ws.send_resume(resume_val)
            except Exception:
                logger.exception("send_resume failed")
                await cl.Message(content="回传失败：与后端的连接已断开。").send()
                return
        elif t == "done":
            cl.user_session.set("flow_done", True)
            return
        elif t == "error":
            await cl.Message(content=f"处理出错：{msg.get('detail', '未知错误')}（详见服务端日志）").send()
            return


async def _render_interrupt(payload) -> object:
    """Render one interrupt with Chainlit Ask* and return a TRUTHY resume value. Mirrors the old
    in-process responder, now fed by the backend's payload over WS; parsing stays in web/hitl.py.
    Every branch returns a truthy value — a falsy resume would loop the graph forever."""
    kind = classify_interrupt(payload)
    ask = payload.get("ask", "") if isinstance(payload, dict) else str(payload)

    if kind == FOLLOWUP:
        res = await cl.AskUserMessage(content=ask + "\n\n（无后续请回复「结束」）", timeout=_ASK_TIMEOUT).send()
        return _reply_text(res).strip() or "结束"

    if kind == MILEAGE:
        tpl = ("请按格式填写（或回复 skip 用 DB 兜底）：\n"
               "走高速：里程 __ km，桥费 __ 元\n"
               "不走高速：里程 __ km，桥费 __ 元")
        for _ in range(3):
            res = await cl.AskUserMessage(content=ask + "\n\n" + tpl, timeout=_ASK_TIMEOUT).send()
            parsed = parse_mileage(_reply_text(res))
            if parsed is not None:
                return parsed                  # list of routes, or "skip" (both truthy)
            await cl.Message(content="没解析出里程/桥费，请照格式重试（或回复 skip）。").send()
        return "skip"

    if kind == AMBIGUITY:
        cands = payload.get("candidates", [])
        actions = [cl.Action(name="pick", label=candidate_label(c), payload={"idx": i})
                   for i, c in enumerate(cands)]
        res = await cl.AskActionMessage(content=ask, actions=actions, timeout=_ASK_TIMEOUT).send()
        idx = _action_payload(res).get("idx", 0)
        if cands and isinstance(idx, int) and 0 <= idx < len(cands):
            return cands[idx]
        return cands[0] if cands else "结束"

    if kind == PARAMS:
        view = (payload.get("params_view") or payload.get("params", {})) if isinstance(payload, dict) else {}
        actions = [cl.Action(name="confirm", label="确认默认值", payload={"choice": "confirm"}),
                   cl.Action(name="modify", label="修改", payload={"choice": "modify"})]
        res = await cl.AskActionMessage(content=ask + "\n\n" + fmt_params(view), actions=actions, timeout=_ASK_TIMEOUT).send()
        if _action_payload(res).get("choice") == "modify":
            keys = " / ".join(view.keys()) if isinstance(view, dict) else "参数"
            m = await cl.AskUserMessage(
                content=f"可修改：{keys}\n（键=值，空格分隔，值可用中文名），如：半径km=30 结果数=5 公里单价=8 柜型=40尺 箱型=冷箱",
                timeout=_ASK_TIMEOUT).send()
            edits = parse_params_edit(_reply_text(m))
            return edits or True               # edited dict, or True if nothing parsed
        return True                            # confirm defaults (TRUTHY — never {})

    res = await cl.AskUserMessage(content=ask or "请补充信息", timeout=_ASK_TIMEOUT).send()   # UNKNOWN fallback
    return _reply_text(res).strip() or "确认"


def _reply_text(res) -> str:
    """User text from an AskUserMessage.send() result (a StepDict -> 'output'; be defensive)."""
    if res is None:
        return ""
    if isinstance(res, dict):
        return str(res.get("output") or res.get("content") or "")
    return str(getattr(res, "content", "") or "")


def _action_payload(res) -> dict:
    """Clicked action's payload from an AskActionMessage.send() result (AskActionResponse TypedDict)."""
    p = res.get("payload") if isinstance(res, dict) else getattr(res, "payload", None)
    return p if isinstance(p, dict) else {}

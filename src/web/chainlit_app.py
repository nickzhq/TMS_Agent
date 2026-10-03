"""Chainlit web app (P7 batch-2): runs the LangGraph estimate flow in-process.

Topology: Chainlit directly imports and runs src/graph — no separate
FastAPI. HITL is bridged in-process (LangGraph interrupt -> Chainlit Ask* -> Command(resume)).
Graph state lives in the LangGraph SqliteSaver checkpointer (per thread_id); the session
list in sessions_db. Business logic stays in src/graph + src/agents; this file is glue.

Lifecycle:
  - app-level (built once, shared): settings / llm / checkpointer / sessions_db / memory / amap.
    mem0 + checkpointer are shared on purpose — Qdrant local is single-process (one client),
    and SqliteSaver is keyed by thread_id, so one instance serves all sessions.
  - session-level (per chat): thread_id + DB connection + compiled graph (DB built lazily on
    the first message so opening a chat never blocks on the DB).

Run: chainlit run src/web/chainlit_app.py
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import sys
from pathlib import Path

# Ensure the project root is importable when launched via `chainlit run src/web/chainlit_app.py`
# (Chainlit puts the target file's dir on sys.path, not the repo root).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import chainlit as cl

from src.config.settings import load_settings
from src.llm.openai_compat import OpenAICompatClient
from src.tools.mssql import MssqlClient, build_connection_string
from src.tools.amap import AmapMCPClient
from src.memory.factory import build_memory
from src.persistence.checkpointer import async_sqlite_checkpointer
from src.persistence.sessions_db import SessionsDB, make_thread_id
from src.graph.build import build_graph, default_config
from src.web.graph_runner import run_flow
from src.web.hitl import (
    classify_interrupt, parse_mileage, parse_params_edit, candidate_label, fmt_params,
    PARAMS, AMBIGUITY, MILEAGE, FOLLOWUP,
)
from src.web.render import render_synthesis
from src.web.chainlit_data_layer import SessionsDataLayer

_APP: dict = {}                              # app-level singletons
_APP_STACK = contextlib.AsyncExitStack()     # holds the async checkpointer (entered at startup)
logger = logging.getLogger("estimate.web")


def _app_deps() -> dict:
    """Build app-level singletons once (shared across sessions).

    Built into a local dict and published only after EVERY dep succeeds, so a mid-init
    failure can never leave _APP partially populated (which would later KeyError on a
    missing key such as 'amap'). The checkpointer is added in on_app_startup (it is async);
    mem0 is lazy — see _get_memory() (Qdrant local is single-process, don't open it eagerly).
    """
    if _APP:
        return _APP
    s = load_settings()
    built = {
        "settings": s,
        "llm": OpenAICompatClient(s.llm),
        "sessions": SessionsDB(s.persistence.sessions_db_path),
        "amap": AmapMCPClient(s.amap.api_key) if s.amap.api_key else None,
    }
    _APP.update(built)   # publish atomically (only after every dep built OK)
    return _APP


def _get_memory():
    """Lazily build mem0 (guarded). Qdrant local is single-process; on lock/init failure,
    degrade to Noop so a memory problem never crashes a chat session."""
    app = _app_deps()
    if "memory" not in app:
        s = app["settings"]
        try:
            app["memory"] = build_memory(s.memory, s.llm)
        except Exception as exc:
            import logging
            logging.getLogger("estimate.web").warning(
                "mem0 init failed (%s); memory disabled this run", type(exc).__name__)
            from src.memory.base import NoopMemory
            app["memory"] = NoopMemory()
    return app["memory"]


_BG_TASKS: set = set()


def _spawn(coro) -> None:
    """Run a coroutine as a tracked fire-and-forget task (keeps a ref; done-callback drops it)."""
    t = asyncio.create_task(coro)
    _BG_TASKS.add(t)
    t.add_done_callback(_BG_TASKS.discard)


def _recall_note(uid: str) -> str:
    """Search mem0 for saved preferences -> a short note for the greeting (guarded; '' on any issue)."""
    try:
        hits = _get_memory().search(uid, "报价偏好：常发货主、常走线路、进出口方向、常用箱型柜型拖型", top_k=3)
    except Exception:
        logger.warning("mem0 recall failed", exc_info=True)
        return ""
    facts = [str(h.get("memory", "")).strip() for h in hits if isinstance(h, dict) and h.get("memory")]
    facts = [f for f in facts if f][:3]
    logger.info("mem0 recall uid=%s -> %d fact(s)", uid, len(facts))
    return ("\n\n（我记得你之前的偏好：" + "；".join(facts) + "）") if facts else ""


def _learn(uid: str, content: str) -> None:
    """Extract preferences from the inquiry into mem0 (runs in a worker thread; guarded)."""
    try:
        _get_memory().add(uid, content)
        logger.info("mem0 learned for uid=%s", uid)
    except Exception:
        logger.warning("mem0 add failed", exc_info=True)


async def _ensure_graph():
    """Lazily open this session's DB connection + compile the graph (first message only)."""
    graph = cl.user_session.get("graph")
    if graph is not None:
        return graph
    app = _app_deps()
    s = app["settings"]
    stack = contextlib.ExitStack()
    db = stack.enter_context(MssqlClient(build_connection_string(s.db)))
    graph = build_graph(app["llm"], db, amap=app["amap"], checkpointer=app["checkpointer"])
    cl.user_session.set("db_stack", stack)
    cl.user_session.set("graph", graph)
    return graph


@cl.data_layer
def _sessions_data_layer():
    """Factory Chainlit calls (lazily) for the data layer. Registering via the @cl.data_layer
    decorator is REQUIRED: assigning `cl.data_layer = X` only rebinds the name and is ignored
    (get_data_layer reads config.code.data_layer, which this decorator sets)."""
    return SessionsDataLayer(_app_deps()["sessions"])


@cl.password_auth_callback
def password_auth_callback(username: str, password: str):
    """Mock single-user auth (P8: replace with real auth). Chainlit's thread-history endpoint
    (/project/threads) returns 401 without an authenticated user -> the left history sidebar stays
    empty. For the local single-user MVP we accept any credentials and map everyone to a fixed
    'default' user (matches the mock uid='default' design; the data layer keys threads by it)."""
    return cl.User(identifier="default", display_name="默认用户", metadata={})


@cl.on_app_startup
async def on_app_startup():
    """Enter the async checkpointer once at server start. The graph runs via ainvoke/astream,
    which needs the checkpointer's async methods — a sync SqliteSaver raises NotImplementedError."""
    app = _app_deps()
    s = app["settings"]
    app["checkpointer"] = await _APP_STACK.enter_async_context(
        async_sqlite_checkpointer(s.persistence.checkpointer_path))
    await app["sessions"].init()
    logger.info("app startup: async checkpointer + sessions schema ready")


@cl.on_app_shutdown
async def on_app_shutdown():
    await _APP_STACK.aclose()


@cl.on_chat_start
async def on_chat_start():
    _app_deps()
    # Align the graph thread with Chainlit's thread_id so the data layer (history) and the
    # checkpointer (state) share one key -> resume works. The thread row is created implicitly
    # by the data layer on the first persisted step.
    thread_id = getattr(getattr(cl.context, "session", None), "thread_id", None) or make_thread_id("default")
    cl.user_session.set("uid", "default")             # single-user MVP; auth placeholder (P8)
    cl.user_session.set("thread_id", thread_id)
    cl.user_session.set("config", default_config(thread_id))
    cl.user_session.set("graph", None)                # built lazily on first message (needs DB)
    cl.user_session.set("db_stack", None)
    cl.user_session.set("flow_done", False)
    cl.user_session.set("titled", False)
    note = _recall_note("default")          # long-term preference recall (mem0); '' if none/disabled
    await cl.Message(
        content="你好，我是物流报价助手。请描述一条线路，例如：天津港到汉沽的运费，进口，单拖。" + note
    ).send()


_ASK_TIMEOUT = 1800   # seconds to wait for a HITL reply (looking up mileage on the phone takes a while)


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


async def _chainlit_respond(payload, state):
    """Chainlit HITL responder: render the interrupt, collect input, return a TRUTHY resume value.

    Kind dispatch mirrors the graph's interrupt payloads (web/hitl.classify_interrupt).
    Every branch returns a truthy value — a falsy resume would loop forever.
    Chainlit 2.12: cl.Action takes payload=<dict> (not value=); AskActionMessage.send() returns
    an AskActionResponse whose 'payload' is the clicked action's payload; AskUserMessage.send()
    returns a StepDict whose 'output' is the user's text.
    """
    kind = classify_interrupt(payload)
    ask = payload.get("ask", "") if isinstance(payload, dict) else str(payload)

    if kind == FOLLOWUP:                       # three-price result is ready in state -> render, then ask
        syn = state.get("synthesis")
        if syn:
            await cl.Message(content=render_synthesis(syn)).send()
        res = await cl.AskUserMessage(content=ask + "\n\n（无后续请回复「结束」）", timeout=_ASK_TIMEOUT).send()
        return _reply_text(res).strip() or "结束"

    if kind == MILEAGE:                        # text template -> parse; retry a few times, else skip
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

    if kind == AMBIGUITY:                      # candidate buttons -> return the chosen candidate
        cands = payload.get("candidates", [])
        actions = [cl.Action(name="pick", label=candidate_label(c), payload={"idx": i})
                   for i, c in enumerate(cands)]
        res = await cl.AskActionMessage(content=ask, actions=actions, timeout=_ASK_TIMEOUT).send()
        idx = _action_payload(res).get("idx", 0)
        if cands and isinstance(idx, int) and 0 <= idx < len(cands):
            return cands[idx]
        return cands[0] if cands else "结束"

    if kind == PARAMS:                         # confirm defaults / edit
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


@cl.on_chat_resume
async def on_chat_resume(thread):
    """Rehydrate the session when a past thread is opened from the sidebar. Chainlit re-renders
    the stored steps; we restore the graph thread_id so follow-ups continue this thread's state.

    Known MVP limitation: switching away mid-inquiry (while an Ask* prompt is pending) abandons the
    in-flight run_flow responder loop. The graph state IS checkpointed, so on return the pending
    question is simply re-asked (recoverable; no data loss). Seamless mid-flow resume is a P8 item."""
    _app_deps()
    thread_id = thread.get("id") if isinstance(thread, dict) else getattr(thread, "id", None)
    cl.user_session.set("uid", "default")
    cl.user_session.set("thread_id", thread_id)
    cl.user_session.set("config", default_config(thread_id))
    cl.user_session.set("graph", None)
    cl.user_session.set("db_stack", None)
    cl.user_session.set("flow_done", False)
    cl.user_session.set("titled", True)               # already-named past thread


@cl.on_message
async def on_message(message: cl.Message):
    app = _app_deps()
    logger.info("on_message invoked")
    graph = await _ensure_graph()
    # First user message of a thread = the inquiry: name the thread (feeds the sidebar) AND fire
    # preference learning NOW (background). Don't defer it to after run_flow — if the user abandons
    # the flow mid-followup (e.g. clicks New Chat), run_flow never returns and learning would be skipped.
    if not cl.user_session.get("titled"):
        cl.user_session.set("titled", True)
        query = (message.content or "").strip()
        await app["sessions"].rename(cl.user_session.get("thread_id"), query.replace("\n", " ")[:24] or "询价")
        _spawn(asyncio.to_thread(_learn, cl.user_session.get("uid", "default"), f"用户询价：{query}"))
    config = cl.user_session.get("config")
    try:
        await run_flow(graph, config, message.content, _chainlit_respond)
        cl.user_session.set("flow_done", True)
    except Exception as exc:
        logger.exception("on_message failed")   # full traceback -> server log
        await cl.Message(content=f"处理出错：{type(exc).__name__}（详见服务端日志）").send()  # user sees only the type


@cl.on_chat_end
async def on_chat_end():
    stack = cl.user_session.get("db_stack")
    if stack is not None:
        stack.close()

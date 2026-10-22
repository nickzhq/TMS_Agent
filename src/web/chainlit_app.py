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
from src.web.backend_client import (
    BackendWS, SessionsREST, bind_thread_token, cache_token, cached_token, login,
)
from src.web.hitl import (
    classify_interrupt, parse_mileage, parse_params_edit, candidate_label, fmt_params,
    parse_bill_period_edit, fmt_bill_view, parse_bill_run_pick, parse_bill_shipper_pick,
    looks_like_detail_question,
    PARAMS, AMBIGUITY, MILEAGE, FOLLOWUP, BILL_PERIOD, BILL_CLOSE_CONFIRM,
    BILL_RUN_PICK, BILL_SHIPPER_PICK,
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
    """The logged-in user's backend JWT (resolved by uid). NOTE: the user hangs off the SESSION
    (cl.context.session.user) — ChainlitContext has NO .user attribute, so cl.context.user raises
    AttributeError (this was masked by the old latest-token fallback). Fails CLOSED ('' -> the backend
    returns 401), never borrowing another user's token."""
    try:
        user = getattr(cl.context.session, "user", None)
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


async def _create_thread_row(text: str) -> None:
    """Create the backend row on the FIRST real interaction (idempotent; never renames an old row).

    This is the ONLY place a chat row is born — besides the example buttons, which call
    _retitle_thread. Opening or refreshing a chat writes NOTHING: doing that used to leave a
    "新对话" entry on every refresh, which users read as "refresh creates a new chat"
    (reported 2026-10-22). Must run BEFORE any message step is persisted, otherwise Chainlit's
    step writes race the row creation and get dropped (the assistant reply vanished, observed).
    """
    try:
        tid = getattr(getattr(cl.context, "session", None), "thread_id", None)
        token = _token()
        if tid and token:
            await SessionsREST(_SETTINGS.backend.base_url, token).ensure(
                tid, title=(text or "")[:24])
    except Exception:
        logger.warning("create thread row failed", exc_info=True)


# ---- lifecycle ----

# ---- welcome / examples ----
# One single source of truth for the user-facing examples: the welcome table, the clickable
# Starter buttons and the 「帮助」 reply are all rendered from _EXAMPLES, so they can never drift.
# (scene, what you can say, what you get)
_EXAMPLES: list[tuple[str, str, str]] = [
    ("线路报价", "天津港到汉沽的运费，进口，单拖",
     "三价对比（协议价 / 历史成交价 / 理论报价）+ 建议报价区间 + 历史成交明细"),
    ("查账期与封账状态", "查一下账期和封账状态",
     "各账期的出账次数、账单总额、箱数与封账状态；说出账期还能看逐份出账记录（含出账编号），照着挑一份封账（首次各 10 条，说「加载更多」继续，每次 20 条）"),
    ("追一笔费用怎么算的", "查一下箱号 XXXX1234567 的提箱费怎么算的",
     "逐期逐笔（台账）+ 原始费用行（业务库，能看「3 天 × 1,200.00」这种计算过程）"),
    ("出账", "出 2026-10 的账单",
     "先确认账期与货主 → 生成账单三件套（中途显示进度）→ 账单摘要"),
    ("封账", "把 2026-10 那份账单封成 2026-10 的应收",
     "说了「第几份」或出账编号就直接确认那一份，没说清才列候选让你选 → 确认归属期 → 写封账记录"),
    ("改封账", "把 2026-10 那份改成 2026-09 的应收",
     "提示受影响账期，并告知下一期基线已指向这份账单"),
]

_WELCOME_HEAD = """你好，我是物流业务助手，能帮你做两件事：**线路报价**和**月度账单**。
下面每一行都是一句可以直接说的话——**点下面的按钮直接跑**，或挑一句复制发给我、照着自己改。"""

_WELCOME_TAIL = """> 两点补充：
> - 报价里的**理论报价**需要你在手机高德「货车模式」查双路线里程与桥费给我（货车路线是收费接口，暂未接入）。
> - **出账**耗时较长（取数 + 生成上百张凭证），中途会显示进度；**封账**若同账期有多份账单，我会先列候选让你选，绝不默认取最新。
>
> 忘了怎么说？直接发「帮助」或「示例」就行。
>
> **一个对话办一件事**：报价和账单（出账 / 查账 / 封账）建议分开对话——两者上下文不同，混在一个对话里历史列表会乱；若上一条还没处理完就改说别的，那条会被放弃。"""

# NOTE on clickable starters: Chainlit only renders @cl.set_starters while the thread is EMPTY.
# on_chat_start sends the welcome message immediately, so starters get disabled and unmounted within
# ~1-3s -> the user can never click them (verified in the browser). Message-level cl.Action buttons
# have no such limitation, so the clickable examples live ON the welcome message instead.
#
# Only the examples that can really run end-to-end are clickable: the billing-generate one would write
# a ledger row, and the box-trace one needs the user's own box number (XXXX1234567 is a placeholder),
# so those two stay copy-only to avoid a stray click producing a real invoice or an empty lookup.
_CLICKABLE = (0, 1)   # indexes into _EXAMPLES: 线路报价 / 查账期

# Short help phrases -> reply locally with the example table (no backend / LLM round-trip).
_HELP_HINTS = ("帮助", "怎么用", "能做什么", "会做什么", "有什么功能", "有哪些功能",
               "示例", "例子", "help")


def _examples_md() -> str:
    """Example table (scene | what to say | what you get)."""
    lines = ["| 我想… | 你可以这样说 | 你会得到 |", "| --- | --- | --- |"]
    for scene, say, got in _EXAMPLES:
        lines.append(f"| {scene} | 「{say}」 | {got} |")
    return "\n".join(lines)


def _welcome_md() -> str:
    return _WELCOME_HEAD + "\n\n" + _examples_md() + "\n\n" + _WELCOME_TAIL


def _is_help(text: str) -> bool:
    """Short sentence + a help word -> answer with the example table (never reaches the graph)."""
    t = (text or "").strip().lower()
    return bool(t) and len(t) <= 16 and any(w in t for w in _HELP_HINTS)


def _example_actions() -> list:
    """Clickable example buttons attached to the welcome message (read-only examples only).

    The action NAME doubles as the label on purpose: Chainlit keys action callbacks by name AND uses
    that name as the initial thread title, so an internal id like "run_example" leaks straight into the
    history list. A human-readable name fixes the title with no rename round-trip needed.
    """
    return [
        cl.Action(name=_EXAMPLES[i][0], label=_EXAMPLES[i][0],
                  payload={"message": _EXAMPLES[i][1]})
        for i in _CLICKABLE
    ]


@cl.on_chat_start
async def on_chat_start():
    cl.user_session.set("flow_done", False)
    cl.user_session.set("busy", False)
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
    await cl.Message(content=_welcome_md() + note, actions=_example_actions()).send()


async def _retitle_thread(text: str) -> None:
    """Example buttons: make the row (if new) and name it after the real sentence.

    Chainlit names a fresh thread after the clicked Action name, which is why the action names are
    human-readable scene names in the first place; this keeps the stored title equal to the sentence.
    """
    try:
        tid = getattr(getattr(cl.context, "session", None), "thread_id", None)
        if not tid or not text:
            return
        title = text.replace("\n", " ")[:24]
        rest = SessionsREST(_SETTINGS.backend.base_url, _token())
        await rest.ensure(tid, title=title)     # create if missing (keeps an existing title)
        await rest.rename(tid, title)           # explicit rename: the action name may be the title
    except Exception:
        logger.warning("retitle thread failed", exc_info=True)


async def _run_query(text: str, echo: str = "") -> None:
    """Send one sentence to the backend and pump the replies (shared by typing and example buttons)."""
    ws = cl.user_session.get("ws") or await _open_ws()
    if ws is None:
        await cl.Message(content="后端未连接，无法处理。请确认后端已启动后重试。").send()
        return
    if cl.user_session.get("busy"):
        await cl.Message(content=(
            "上一条还没结束：先处理上面的卡片（或回复「结束」放弃它）。"
            "要办别的事，建议点左上角「新对话」。"
        )).send()
        return
    if echo:
        await cl.Message(content=echo).send()
    cl.user_session.set("busy", True)
    try:
        await ws.send_query(text)
        await _pump(ws)
    except Exception:
        logger.exception("send_query failed")
        await cl.Message(content="发送失败：与后端的连接已断开。").send()
    finally:
        cl.user_session.set("busy", False)


@cl.on_message
async def on_message(message: cl.Message):
    text = (message.content or "").strip()
    if not text:
        return
    # First real interaction -> create the row (and its title) BEFORE any step is persisted.
    # Local-only replies ("帮助") need this too: they never reach the backend WS, so without it
    # the chat would never enter the history list.
    await _create_thread_row(text)
    # 「帮助 / 示例」 locally: no backend hop, and it can never be mistaken for a real request
    if _is_help(text):
        await cl.Message(content=_welcome_md()).send()
        return
    await _run_query(text)


async def on_example_clicked(action: cl.Action):
    """A clickable example on the welcome message -> behaves exactly like typing that sentence."""
    text = str((action.payload or {}).get("message") or "").strip()
    if not text:
        return
    await _retitle_thread(text)                       # best effort: persisted row carries the sentence
    await _run_query(text, echo=f"按示例执行：{text}")
    await _retitle_thread(text)                       # re-apply in case it was overwritten mid-flow


# One registration per example scene name (Chainlit keys action callbacks by action name; the name is
# also the thread title, so it must stay human-readable).
for _scene_name in (_EXAMPLES[i][0] for i in _CLICKABLE):
    cl.action_callback(_scene_name)(on_example_clicked)


@cl.on_chat_resume
async def on_chat_resume(thread):
    """Rehydrate a past thread: Chainlit re-renders the stored steps (via the data layer). We open a
    fresh WS so follow-ups continue this thread's backend state (checkpointer keyed by the same id).

    Known limitation (carried from P7): resuming mid-inquiry re-asks the pending question — the graph
    state IS checkpointed, so nothing is lost. Seamless mid-flow resume is a later P8 batch."""
    cl.user_session.set("flow_done", False)
    cl.user_session.set("busy", False)
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
    progress_msg = None

    async def _progress(txt: str) -> None:
        """出账进度：覆盖同一个消息（不刷屏）；出账耗时以分钟计，没有反馈用户会以为卡死。"""
        nonlocal progress_msg
        # Chainlit 2.x Message.update() takes NO arguments: assign .content first, then update.
        # Guard it: a progress-render failure MUST NOT kill the pump, otherwise the bill summary
        # and the close-confirm interrupt never arrive and the UI sits on the first progress line
        # forever (this actually happened: update(content=...) raised TypeError on the 2nd update).
        try:
            if progress_msg is None:
                progress_msg = cl.Message(content=f"出账中：{txt}")
                await progress_msg.send()
            else:
                progress_msg.content = f"出账中：{txt}"
                await progress_msg.update()
        except Exception:
            logger.warning("progress render failed", exc_info=True)

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
        elif t == "progress":
            txt = msg.get("text", "")
            if txt:
                await _progress(txt)
        elif t == "bill":
            md = (msg.get("data") or {}).get("markdown") or ""
            if md:
                await cl.Message(content=md).send()
        elif t == "bill_close":
            md = (msg.get("data") or {}).get("markdown") or ""
            if md:
                await cl.Message(content=md).send()
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

    if kind == BILL_PERIOD:
        view = (payload.get("bill_view") or payload.get("bill", {})) if isinstance(payload, dict) else {}
        actions = [cl.Action(name="confirm", label="确认，出账", payload={"choice": "confirm"}),
                   cl.Action(name="modify", label="修改", payload={"choice": "modify"})]
        res = await cl.AskActionMessage(content=ask + "\n\n" + fmt_bill_view(view),
                                        actions=actions, timeout=_ASK_TIMEOUT).send()
        if _action_payload(res).get("choice") == "modify":
            m = await cl.AskUserMessage(
                content="可修改：账期 / 货主\n（键=值，空格分隔），如：账期=2026-09；或直接说「改成9月」",
                timeout=_ASK_TIMEOUT).send()
            edits = parse_bill_period_edit(_reply_text(m))
            return edits or True           # edited dict, or True if nothing parsed
        return {"确认": True}               # TRUTHY — never {}

    if kind == BILL_RUN_PICK:
        # TEXT input instead of buttons: users often cannot pick straight away — they first want to
        # ask about a candidate ("第 1 份的详情" / "这两份有什么区别"). Buttons forced a 2-way
        # choice with no way to ask. The node answers the question and then asks again.
        res = await cl.AskUserMessage(content=ask, timeout=_ASK_TIMEOUT).send()
        text = _reply_text(res)
        if looks_like_detail_question(text):
            return {"追问": text}            # TRUTHY -> node replies with details, then re-asks
        return parse_bill_run_pick(text)

    if kind == BILL_SHIPPER_PICK:
        cands = payload.get("bill_shipper_candidates") or []
        actions = [cl.Action(name="pick", label=str(c.get("货主") or c.get("货主key") or c),
                             payload={"choice": "pick", "key": c.get("货主key")})
                   for c in cands]
        res = await cl.AskActionMessage(content=ask, actions=actions, timeout=_ASK_TIMEOUT).send()
        key = _action_payload(res).get("key")
        if key:
            return {"货主": str(key)}       # TRUTHY
        return parse_bill_shipper_pick(_reply_text(res))

    if kind == BILL_CLOSE_CONFIRM:
        view = (payload.get("bill_close_view") or payload.get("bill_close", {})) if isinstance(payload, dict) else {}
        actions = [cl.Action(name="close", label="按默认账期封账", payload={"choice": "close"}),
                   cl.Action(name="other", label="改成别的账期", payload={"choice": "other"}),
                   cl.Action(name="skip", label="暂不封账", payload={"choice": "skip"})]
        res = await cl.AskActionMessage(content=ask + "\n\n" + fmt_bill_view(view),
                                        actions=actions, timeout=_ASK_TIMEOUT).send()
        choice = _action_payload(res).get("choice")
        if choice == "skip":
            return {"不封账": True}         # TRUTHY — never {}
        if choice == "other":
            m = await cl.AskUserMessage(content="封到哪个应收账期？（如：9月 / 2026-09）",
                                        timeout=_ASK_TIMEOUT).send()
            edits = parse_bill_period_edit(_reply_text(m))
            if edits.get("账期"):
                return {"归属账期": edits["账期"]}
            return {"不封账": True}
        return {"确认": True}               # TRUTHY — never {}

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

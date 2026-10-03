"""UI-agnostic LangGraph runner with HITL interrupt bridging (P7 batch-2).

Drives the estimate graph: runs it, and whenever it pauses at an interrupt, delegates to a
`respond` callback to obtain the resume value. The Chainlit app renders Ask*/AskAction for
`respond`; the headless test auto-answers. Keeping this separate from Chainlit makes the
flow logic testable without a browser.

CRITICAL: the resume value MUST be truthy — an empty {} / None / '' makes LangGraph treat it
as "no resume" and re-pause forever (HITL death loop). run_flow refuses a falsy resume loudly.
"""
from __future__ import annotations

from typing import Any, Awaitable, Callable

from langgraph.types import Command

MAX_INTERRUPTS = 12   # soft guard against runaway interrupt loops (recursion_limit is the hard stop)

Responder = Callable[[Any, dict], Awaitable[Any]]    # (interrupt payload, current state) -> truthy resume value
Emitter = Callable[[str, str], Awaitable[None]]      # (kind, text) -> None  (progress/log hook)


def _ask_of(payload: Any) -> str:
    return payload.get("ask", "") if isinstance(payload, dict) else str(payload)


async def run_flow(
    graph,
    config: dict,
    user_query: str,
    respond: Responder,
    emit: Emitter | None = None,
) -> dict:
    """Run the graph to completion, bridging every interrupt through `respond`.

    Returns the final state dict (contains `synthesis` once the inquiry completes).
    """
    async def _emit(kind: str, text: str) -> None:
        if emit is not None:
            await emit(kind, text)

    state = await graph.ainvoke({"user_query": user_query}, config)
    guard = 0
    while "__interrupt__" in state and guard < MAX_INTERRUPTS:
        guard += 1
        payload = state["__interrupt__"][0].value
        await _emit("interrupt", _ask_of(payload))
        resume_val = await respond(payload, state)   # state passed so the responder can render results (e.g. synthesis at the followup interrupt)
        if not resume_val:            # falsy resume => LangGraph death loop; refuse loudly
            raise ValueError(f"HITL resume value must be truthy, got {resume_val!r} (ask={_ask_of(payload)!r})")
        await _emit("resume", f"{type(resume_val).__name__}")
        state = await graph.ainvoke(Command(resume=resume_val), config)
    if "__interrupt__" in state:
        raise RuntimeError(f"flow exceeded MAX_INTERRUPTS={MAX_INTERRUPTS} (possible loop)")
    return state

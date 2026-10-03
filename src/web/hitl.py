"""HITL helpers for the Chainlit bridge (P7 batch-2): classify interrupts + parse input.

Pure functions (NO Chainlit import) so they are unit-testable headless. The Chainlit app
renders based on classify_interrupt() and feeds the user's reply back through the parsers.
Business logic stays in src/graph; this is UI-bridge glue (generic) -> committed.
"""
from __future__ import annotations

import re

# interrupt kinds (mirror the graph's interrupt payloads)
PARAMS = "params"        # {"ask", "params": {...}}            -> confirm / edit defaults
AMBIGUITY = "ambiguity"  # {"ask", "candidates": [...]}         -> pick one candidate
MILEAGE = "mileage"      # {"ask"(货车模式), "format"}          -> fill 2 routes or skip
FOLLOWUP = "followup"    # {"ask"(三价对照/加载更多)}            -> free text or end
UNKNOWN = "unknown"

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_ROUTE_SPLIT = re.compile(r"[\n;；]+")
_KV = re.compile(r"([\u4e00-\u9fa5A-Za-z_]+)\s*[=：:]\s*([^\s,，;；]+)")


def classify_interrupt(payload) -> str:
    """Map a graph interrupt payload to a UI kind (order matters)."""
    if not isinstance(payload, dict):
        return UNKNOWN
    if payload.get("candidates"):
        return AMBIGUITY
    ask = payload.get("ask", "") or ""
    if payload.get("format") or "货车模式" in ask:
        return MILEAGE
    if payload.get("params") is not None:
        return PARAMS
    if any(w in ask for w in ("加载更多", "三价对照", "换货主", "不走高速价")):
        return FOLLOWUP
    return UNKNOWN


def candidate_label(c) -> str:
    """Display label for an ambiguity candidate (district name or shipper name)."""
    if not isinstance(c, dict):
        return str(c)
    return str(c.get("值") or c.get("货主简称") or c.get("ID") or c.get("货主ID") or c)


def parse_mileage(text: str):
    """Parse the mileage template -> routes list; 'skip' -> "skip"; unparseable -> None.

    Lenient: one route per line; first number = mileage, second = toll; the route name is
    inferred from keywords on the line.
    """
    if text is None:
        return None
    t = text.strip()
    if not t:
        return None
    if t.lower().startswith("skip") or t.startswith("跳过"):
        return "skip"
    routes: list[dict] = []
    for line in _ROUTE_SPLIT.split(t):
        line = line.strip()
        if not line:
            continue
        nums = [float(x) for x in _NUM.findall(line)]
        if not nums:
            continue
        if any(w in line for w in ("不走", "少收费", "省钱")):
            name = "不走高速"
        elif "高速" in line:
            name = "走高速"
        else:
            name = re.split(r"[：:]", line)[0].strip() or f"路线{len(routes) + 1}"
        routes.append({"name": name, "mileage": nums[0], "toll": nums[1] if len(nums) > 1 else 0.0})
    return routes or None


def parse_params_edit(text: str) -> dict:
    """Parse 'key=value' edits (keys may be non-ASCII) -> dict; numeric values coerced; empty -> {}.

    Values that look numeric become int/float, others stay strings; keys must match the
    graph's parameter names.
    """
    out: dict = {}
    if not text:
        return out
    for k, v in _KV.findall(text):
        if _NUM.fullmatch(v):
            out[k] = float(v) if "." in v else int(v)
        else:
            out[k] = v
    return out


def fmt_params(params: dict) -> str:
    """Render the params dict as a readable bullet list for the confirm prompt."""
    if not isinstance(params, dict) or not params:
        return "（无参数）"
    return "\n".join(f"- {k}：{v}" for k, v in params.items())

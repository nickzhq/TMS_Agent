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
BILL_PERIOD = "bill_period"                 # {"ask", "bill": {...}, "bill_view": {...}} -> 确认/修改 账期+货主
BILL_CLOSE_CONFIRM = "bill_close_confirm"   # {"ask", "bill_close": {...}} -> 封账 / 不封 / 改归属期
BILL_RUN_PICK = "bill_run_pick"             # {"ask", "bill_runs": [...]} -> 从候选账单里选一份（批3.2）
BILL_SHIPPER_PICK = "bill_shipper_pick"     # {"ask", "bill_shipper_candidates": [...]} -> 选货主（批3.2）
PROFIT_RANGE = "profit_range"               # {"ask", "profit_view": {...}} -> 确认/修改月份区间
PROFIT_FORECAST = "profit_forecast"         # {"ask", "forecast_view": {...}} -> 确认/修改预测参数
WT_PERIOD = "wt_period"                     # {"ask", "wt_view": {...}} -> 确认/修改对账单账期
WT_CONFIRM = "wt_confirm"                   # {"ask", "wt_view", "wt_manual"} -> 确认 / 跳过（都照常出账）
INVOICE_CONFIRM = "invoice_confirm"         # {"ask", "invoice_view", "invoice_preview"} -> 确认 / 放弃（写发票库前）
WT_CLOSE_CONFIRM = "wt_close_confirm"       # {"ask", "wt_close_view", "wt_close_diff"} -> 封账 / 改封账前确认
WT_CLOSE_PROMPT = "wt_close_prompt"         # {"ask", "wt_close_view", "wt_close_prompt": True} -> 出账后问“封不封”
UNKNOWN = "unknown"

_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_ROUTE_SPLIT = re.compile(r"[\n;；]+")
_KV = re.compile(r"([\u4e00-\u9fa5A-Za-z_]+)\s*[=：:]\s*([^\s,，;；]+)")


def classify_interrupt(payload) -> str:
    """Map a graph interrupt payload to a UI kind (order matters)."""
    if not isinstance(payload, dict):
        return UNKNOWN
    # 批3.2 的两种账单旁路中断必须排在 AMBIGUITY 之前（它们不叫 candidates，但顺序上更明确）
    if payload.get("bill_runs") is not None:
        return BILL_RUN_PICK
    if payload.get("bill_shipper_candidates") is not None:
        return BILL_SHIPPER_PICK
    if payload.get("profit_view") is not None:
        return PROFIT_RANGE
    if payload.get("forecast_view") is not None:
        return PROFIT_FORECAST
    if payload.get("wt_can_skip") is not None:
        return WT_CONFIRM
    if payload.get("invoice_view") is not None:
        return INVOICE_CONFIRM
    if payload.get("wt_close_prompt"):
        return WT_CLOSE_PROMPT
    if payload.get("wt_close_view") is not None:
        return WT_CLOSE_CONFIRM
    if payload.get("wt_view") is not None:
        return WT_PERIOD
    if payload.get("candidates"):
        return AMBIGUITY
    if payload.get("bill_close") is not None:
        return BILL_CLOSE_CONFIRM
    if payload.get("bill") is not None:
        return BILL_PERIOD
    ask = payload.get("ask", "") or ""
    if payload.get("format") or "货车模式" in ask:
        return MILEAGE
    if payload.get("params") is not None:
        return PARAMS
    if any(w in ask for w in ("加载更多", "三价对照", "换货主", "不走高速价")):
        return FOLLOWUP
    return UNKNOWN


_FORECAST_KEYS = {"目标月份", "回看", "固定成本", "工资", "每司机趟数", "每车趟数", "备用率"}


def parse_forecast_edit(text: str) -> dict:
    """解析预测参数的「键=值」修改（如：目标月份=2026-11 固定成本=260000）。"""
    out: dict = {}
    for m in _KV.finditer(text or ""):
        k, v = m.group(1), m.group(2)
        if k in _FORECAST_KEYS:
            out[k] = v
    return out


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


# ---- 账单域（批3.1）解析器 ----

_PERIOD_KEYS = ("账期", "归属账期", "归属应收账期")
_SHIPPER_KEYS = ("货主", "货主简称")
_NEGATIVE = ("不封", "暂不", "先不", "不用封", "算了", "取消")


def parse_bill_period_edit(text: str) -> dict:
    """账单参数确认的修改回传 -> {"账期": "...", "货主": "..."}（可能为空 dict）。

    支持 "账期=2026-09 货主=<货主名>" 与口语 "改成9月"。
    """
    out: dict = {}
    t = (text or "").strip()
    if not t:
        return out
    for k, v in _KV.findall(t):
        if k in _PERIOD_KEYS:
            out["账期"] = v
        elif k in _SHIPPER_KEYS:
            out["货主"] = v
    if "账期" not in out:
        m = re.search(r"(\d{1,2})\s*月", t)
        if m:
            out["账期"] = f"{m.group(1)}月"
    return out


def parse_bill_close_answer(text: str) -> dict:
    """封账询问的回答 -> truthy dict。

    - "不封 / 暂不 / 算了" -> {"不封账": True}
    - "改成9月 / 归属账期=2026-09" -> {"归属账期": "..."}
    - "确认 / 封" -> {"确认": True}
    - 空回复 -> {"不封账": True}（**安全默认**：封账 = 定标准，宁可少做不可错做）
    """
    t = (text or "").strip()
    if not t:
        return {"不封账": True}
    if any(w in t for w in _NEGATIVE):
        return {"不封账": True}
    edits = parse_bill_period_edit(t)
    if "账期" in edits:
        return {"归属账期": edits["账期"]}
    return {"确认": True}


def fmt_bill_view(view: dict) -> str:
    """账单参数确认提示（键值对 -> 列表行）。"""
    if not isinstance(view, dict) or not view:
        return "（无账单参数）"
    return "\n".join(f"- {k}：{v}" for k, v in view.items())


# ---- 账单域旁路（批3.2）解析器 ----

_RE_RUN_ID = re.compile(r"\d{6}[_-][A-Za-z0-9]+[_-]\d{8}[_-]\d{6}")
_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

# 「先别急着选，我想先了解某一份」——选单回答里命中这些词时按「追问」处理，
# **不能**按序号解析：否则「第 1 份的详情」会被当成「选第 1 份」直接进入封账。
_DETAIL_WORDS = ("详情", "明细", "详细", "具体", "区别", "差异", "对比", "不一样",
                 "差在哪", "差多少", "为什么", "看看", "看一下", "介绍", "包含", "哪些", "怎么算")


def looks_like_detail_question(text: str) -> bool:
    """选单阶段的文本像不像「我在问情况」而不是「我选好了」。"""
    t = (text or "").strip()
    return bool(t) and any(w in t for w in _DETAIL_WORDS)


def parse_bill_run_pick(text: str) -> dict:
    """选单回答 -> truthy dict（**绝不默认取最新**）。

    - "第2份 / 2 / 第二份"  -> {"序号": 2}
    - "最新那份"            -> {"选最新": True}
    - "最早那份"            -> {"选最旧": True}
    - 直接给出出账编号      -> {"出账编号": "..."}
    - 空回复 / "确认"       -> {"确认": True}（**安全默认**：只有一份候选时才算数；
      多份候选时节点不执行封账——封错一次会一路影响后续账期的扣减基线）
    """
    t = (text or "").strip()
    if not t:
        return {"确认": True}
    m = _RE_RUN_ID.search(t)
    if m:
        return {"出账编号": m.group(0)}
    if "最新" in t or "最近" in t:
        return {"选最新": True}
    if "最早" in t or "最初" in t:
        return {"选最旧": True}
    m = re.search(r"(\d+)\s*(?:个|份|条|版|次)?", t)
    if m:
        return {"序号": int(m.group(1))}
    m = re.search(r"第([一二三四五六七八九十])\s*(?:个|份|条|版)?", t)
    if m:
        return {"序号": _CN_NUM.get(m.group(1), 0)}
    return {"确认": True}


def parse_bill_shipper_pick(text: str) -> dict:
    """选货主 -> {"货主": "..."}（原样回传，节点侧再匹配登记表）。

    空回复 -> {"货主": ""}（非空 dict = truthy；节点匹配不到就不执行）。
    """
    return {"货主": (text or "").strip()}


# ---------- 对账单域（bill_wt）----------

def fmt_wt_view(view: dict) -> str:
    """对账单确认卡/账期卡的键值对 -> 列表行。"""
    if not isinstance(view, dict):
        return ""
    return "\n".join(f"- {k}：{v}" for k, v in view.items())


def parse_wt_period_edit(text: str) -> dict:
    """账期修改回传 -> {"账期": "..."}（节点用 parse_period_hint 再解析）。

    空回复 -> {"确认": True}（不能返回 {}：空 dict 会被当成没恢复）。
    """
    t = (text or "").strip()
    return {"账期": t} if t else {"确认": True}


_WT_SKIP = ("跳过", "先跳", "稍后", "不确认", "待补", "skip", "later")


def parse_wt_confirm_answer(text: str) -> dict:
    """确认卡 -> {"action": "skip"} 或 {"action": "ok"}。

    两个出口都照常出账（跳过 = 先出红行版，事后补）；所以这里**永远返回 truthy**。
    """
    t = (text or "").strip().lower()
    if any(w in t for w in _WT_SKIP):
        return {"action": "skip"}
    return {"action": "ok"}


# ---------- 发票主数据（invoice_confirm）----------

_INV_CANCEL = ("放弃", "取消", "算了", "不改", "不作废", "cancel", "no")


def parse_invoice_confirm(text: str) -> dict:
    """写发票库前的确认 -> {"放弃": True} 或 {"确认": True}（均 truthy）。"""
    t = (text or "").strip().lower()
    if any(w in t for w in _INV_CANCEL):
        return {"放弃": True}
    return {"确认": True}


# 封账/改封账确认与“写发票库前确认”是同一件事：确认 → {"确认": True}，否则 {"放弃": True}。
# 共用一份实现，避免两个函数各自漂移（曾经两份代码逐字相同）。
parse_wt_close_answer = parse_invoice_confirm


_WT_NO_CLOSE = ("不封", "暂不", "先不", "不用封", "不封账", "later")
_WT_YES_CLOSE = ("封账", "封吧", "确认封", "封", "好", "可以", "确认", "ok", "yes")


def parse_wt_close_prompt(text: str) -> dict:
    """出账后的“封不封” -> {"封账": True} 或 {"不封": True}（均 truthy）。

    与账单域一致（写操作保守）：
    - 空回复 / 看不出来 -> **不封**，但必须返回非空 dict。
    """
    t = (text or "").strip().lower()
    if any(w in t for w in _WT_NO_CLOSE):
        return {"不封": True}
    if any(w in t for w in _WT_YES_CLOSE):
        return {"封账": True}
    return {"不封": True}

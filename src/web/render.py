"""Render the synthesis result to Markdown for the Chainlit UI (P7 batch-2, S4.4).

Pure functions (no Chainlit import) -> unit-testable. Emits localized, user-facing terms
(never internal codes) and no decorative emoji (project rule). Business logic stays in
src/graph; this is presentation glue (generic) -> committed.
"""
from __future__ import annotations


def _money(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, (int, float)):
        return f"{v:g}"
    return str(v)


def _cell(v) -> str:
    if v is None:
        return ""
    return str(v).replace("|", "\\|").replace("\n", " ")


def _agreement_md(val) -> str:
    """Render the agreement-price value: an exact price, or a range + signed-shipper count."""
    if not val:
        return "无匹配"
    out = []
    for r in val:
        d = r.get("方向", "?")
        if "协议价" in r:
            out.append(f"{d}：{_money(r.get('协议价'))} 元")
        else:
            lo, hi = (r.get("区间") or (None, None))[:2]
            out.append(f"{d}：{_money(lo)}~{_money(hi)} 元（{r.get('签约货主数', '?')} 家签约货主）")
    return "；".join(out)


def _history_md(val) -> str:
    """Render the historical deal-price band (min / median / max)."""
    if not val or val[0] is None:
        return "无匹配"
    lo, med, hi = val[0], val[1] if len(val) > 1 else None, val[2] if len(val) > 2 else None
    return f"{_money(lo)}~{_money(hi)} 元（中位 {_money(med)}）"


def _theoretical_md(val) -> str:
    """Render the theoretical quote, one entry per route."""
    if not val:
        return "无匹配"
    return "；".join(f"{r.get('路线', '?')}：{_money(r.get('理论价'))} 元" for r in val)


_VALUE_MD = {"协议价": _agreement_md, "历史成交价": _history_md, "理论报价": _theoretical_md}


def _suggestion_md(qu) -> str:
    """Render the suggested range: a (lo, hi) tuple, an agreement-view list, or None."""
    if qu is None:
        return "无（三价均无匹配）"
    if isinstance(qu, (tuple, list)):
        if qu and isinstance(qu[0], dict):
            return "以签约协议价为准（见上表）"
        vals = [v for v in qu if v is not None]
        if len(vals) >= 2:
            return f"{_money(vals[0])}~{_money(vals[-1])} 元"
        return f"{_money(vals[0])} 元" if vals else "—"
    return f"{_money(qu)} 元"


def _detail_table(rows) -> str:
    """Generic list[dict] -> Markdown table (preserves the SQL column order)."""
    if not rows:
        return "_（无历史明细）_"
    cols = list(rows[0].keys())
    head = "| " + " | ".join(str(c) for c in cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = "\n".join("| " + " | ".join(_cell(r.get(c)) for c in cols) + " |" for r in rows)
    return "\n".join([head, sep, body])


def render_synthesis(syn: dict) -> str:
    """Full Markdown block: three-price comparison + suggestion + history detail."""
    if not syn:
        return "_（暂无报价结果）_"
    parts = ["## 三价对比", "", "| 价格来源 | 结果 |", "| --- | --- |"]
    for row in syn.get("comparison", []):
        src = row.get("来源", "?")
        text = _VALUE_MD.get(src, str)(row.get("值")) if row.get("有") else "无匹配"
        parts.append(f"| {src} | {text} |")

    sug = syn.get("suggestion", {}) or {}
    parts += ["", f"**建议报价区间**：{_suggestion_md(sug.get('区间'))}", f"> 依据：{sug.get('依据', '')}"]
    if syn.get("review"):
        parts += ["", "> 【复核提示】三价差异较大，建议人工复核。"]

    detail = syn.get("history_detail", [])
    head = f"## 历史成交明细（Top {len(detail)}）" if detail else "## 历史成交明细"
    parts += ["", head, "", _detail_table(detail)]
    # One conversation = one job. Point to a new chat at the exact moment the quote is done:
    # mixing a billing request into this thread scatters the history (and, mid-flow, silently drops
    # the unfinished inquiry).
    parts += ["", "---",
              "> 这条线路的报价就到这里。如果接下来要办**账单**（出账 / 查账 / 封账），"
              "建议点左上角「新对话」再问——报价和账单分开记，历史列表里更好找。"]
    return "\n".join(parts)

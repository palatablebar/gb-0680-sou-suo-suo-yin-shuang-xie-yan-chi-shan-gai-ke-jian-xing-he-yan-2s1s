from decimal import Decimal

from app import orphans
from app.values import number, instant
from app.errors import DomainError


def evaluate(rows, context):
    context = context or {}
    params = context.get("parameters", {})
    threshold = number(params.get("max_delay_seconds", "120"))
    as_of = instant(params.get("as_of", orphans.DEFAULT_AS_OF))
    if threshold < 0:
        raise DomainError("BAD_THRESHOLD", "延迟阈值不能为负")
    # 孤存文档的结论由 M4 产生；这里直接读取，不按延迟重新定责。
    rows = [orphans.ensure(row, params, rows) for row in rows]
    out = []
    for row in rows:
        # 先校验读到的结论与门禁没有分叉，再据此定责，避免静默改掉上游结论。
        orphans.verify(row)
        source = instant(row["source_at"])
        visible = instant(row["visible_at"]) if row.get("visible_at") else None
        delay = max(Decimal(0), Decimal(str(((visible or as_of) - source).total_seconds())))
        state = row["visibility_state"]
        if state == "ORPHAN_INDEX":
            gate = orphans.gate(row.get("orphan_state"))
            reported = orphans.reported(row.get("orphan_state"))
        else:
            if state in {"MISSING_INDEX", "DELETE_NOT_CONFIRMED"}:
                gate = "缺失" if delay > threshold else "等待"
            elif state != "CONSISTENT":
                gate = "内容差异"
            elif delay > threshold:
                gate = "延迟"
            else:
                gate = "通过"
            reported = "1" if gate == "通过" else "0"
        updated = {
            **row,
            "delay_seconds": str(delay),
            "gate": gate,
            "threshold_seconds": str(threshold),
            "expected": "1",
            "reported": reported,
        }
        orphans.verify(updated)
        out.append(updated)
    summary = orphans.summary(out)
    return {
        "rows": out,
        "quarantine": [],
        "metrics": {
            "pass": sum(r["gate"] == "通过" for r in out),
            "delayed": sum(r["gate"] == "延迟" for r in out),
            "missing": sum(r["gate"] == "缺失" for r in out),
            "waiting": sum(r["gate"] == "等待" for r in out),
            "mismatch": sum(r["gate"] == "内容差异" for r in out),
            "retirable": sum(r["gate"] == "可下架" for r in out),
            "orphan_waiting": summary["orphan_waiting"],
            "orphan_settled": summary["orphan_settled"],
            "conclusion_digest": summary["conclusion_digest"],
        },
        "orphan_conclusions": orphans.conclusions(out),
        "conclusion_digest": summary["conclusion_digest"],
    }

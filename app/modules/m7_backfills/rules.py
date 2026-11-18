import hashlib
import json

from app import orphans
from app.errors import DomainError
from app.reasons import sources


def evaluate(rows, context):
    context = context or {}
    params = context.get("parameters", {})
    stages = [v for v in context.values() if isinstance(v, dict) and "metrics" in v]
    balances = [s["metrics"]["balanced"] for s in stages if "balanced" in s["metrics"]]
    if balances and not all(balances):
        raise DomainError("UNBALANCED", "来源行未对平，不能归档")
    rows = [orphans.ensure(row, params, rows) for row in rows]
    ids = [str(r.get("doc_id", r.get("_line", ""))) for r in rows]
    if len(ids) != len(set(ids)):
        raise DomainError("ARCHIVE_DUPLICATE", "归档业务键重复")

    # 封存只承认落在门禁/对账里的同一份孤存结论；结论分叉时拒绝归档。
    upstream = {}
    for stage in stages:
        for item in stage.get("orphan_conclusions", []) or []:
            upstream[str(item.get("doc_id"))] = item
    for row in rows:
        orphans.verify(row)
        if row.get("visibility_state") != "ORPHAN_INDEX":
            continue
        seen = upstream.get(str(row.get("doc_id")))
        if seen and row.get("conclusion_id") and seen.get("conclusion_id") != row["conclusion_id"]:
            raise DomainError("CONCLUSION_DRIFT", "封存结论与上游阶段不一致")

    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode()
    summary = orphans.summary(rows)
    conclusions = orphans.conclusions(rows)
    return {
        "rows": rows,
        "quarantine": [],
        "metrics": {
            "records": len(rows),
            "source_lines": len(set(x for row in rows for x in sources(row))),
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "ready": True,
            "differences": sum(r.get("status") == "差异" for r in rows),
            "retirable": summary["retirable"],
            "orphan_waiting": summary["orphan_waiting"],
            "orphan_settled": summary["orphan_settled"],
            "conclusions": summary["conclusions"],
            "retirable_ids": summary["retirable_ids"],
            "conclusion_digest": summary["conclusion_digest"],
        },
        "orphan_conclusions": conclusions,
        "conclusion_digest": summary["conclusion_digest"],
    }

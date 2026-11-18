from collections import defaultdict
import json

from app import orphans
from app.reasons import sources
from app.values import number


def same_document(a, b):
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def rejected_documents(quarantine):
    """源端存在但未被采纳的文档：不能把「隔离」当成「源端没有」。"""
    docs = set()
    for row in quarantine or []:
        if row.get("side") == "source" and row.get("doc_id"):
            docs.add(str(row["doc_id"]))
    return docs


def evaluate(rows, context):
    context = context or {}
    params = context.get("parameters") or {}
    values = orphans.policy(params, rows)
    rejected = rejected_documents(context.get("quarantine"))
    grouped = defaultdict(dict)
    for row in rows:
        grouped[row["doc_id"]][row["side"]] = row
    out = []
    for doc, sides in sorted(grouped.items()):
        source = sides.get("source")
        target = sides.get("index")
        lines = [n for r in sides.values() for n in sources(r)]
        if source is None:
            state = "ORPHAN_INDEX"
        elif target is None:
            state = "MISSING_INDEX" if source["op"] == "upsert" else "DELETE_NOT_CONFIRMED"
        elif number(target["version"]) > number(source["version"]):
            state = "INDEX_AHEAD"
        elif number(target["version"]) < number(source["version"]):
            state = "STALE_INDEX"
        elif source["op"] != target["op"]:
            state = "OP_MISMATCH"
        elif source["op"] == "upsert" and not same_document(source.get("document"), target.get("document")):
            state = "CONTENT_MISMATCH"
        else:
            state = "CONSISTENT"
        anchor = source or target
        row = {
            **anchor,
            "_sources": lines,
            "source_version": source["version"] if source else "—",
            "index_version": target["version"] if target else "—",
            "source_op": source["op"] if source else "—",
            "index_op": target["op"] if target else "—",
            "visible_at": target.get("visible_at") if target else None,
            "visibility_state": state,
            "expected": "1",
            "reported": "1" if state == "CONSISTENT" else "0",
            "document_equal": state == "CONSISTENT",
        }
        if state == "ORPHAN_INDEX":
            # 孤存文档的唯一裁决在这里产生，后续阶段只读取、不再重算。
            conclusion = orphans.annotate(
                target, values, source_rejected=str(doc) in rejected
            )
            row.update(conclusion)
            row["reported"] = orphans.reported(conclusion["orphan_state"])
        out.append(row)
    summary = orphans.summary(out)
    return {
        "rows": out,
        "quarantine": [],
        "metrics": {
            "documents": len(out),
            "consistent": sum(r["visibility_state"] == "CONSISTENT" for r in out),
            "missing": sum(r["visibility_state"] in {"MISSING_INDEX", "DELETE_NOT_CONFIRMED"} for r in out),
            "orphans": summary["conclusions"],
            "orphan_waiting": summary["orphan_waiting"],
            "retirable": summary["retirable"],
            "orphan_settled": summary["orphan_settled"],
            "source_watermark": values["watermark"].isoformat() if values["watermark"] else None,
            "watermark_coverage": values["coverage"],
            "conclusion_digest": summary["conclusion_digest"],
        },
        "orphan_conclusions": orphans.conclusions(out),
        "orphan_policy": {
            "as_of": values["as_of"].isoformat(),
            "grace_seconds": str(values["grace"]),
            "source_watermark": values["watermark"].isoformat() if values["watermark"] else None,
            "coverage": values["coverage"],
        },
    }

from decimal import Decimal

from app import orphans
from app.errors import DomainError
from app.reasons import sources


def evaluate(rows, context):
    context = context or {}
    params = context.get("parameters", {})
    intake = context.get("intake")
    bad = context.get("quarantine", [])
    # 对账读取门禁已经落地的结论，不再按缺失去向重新判一遍。
    rows = [orphans.ensure(row, params, rows) for row in rows]
    accepted = set(x for row in rows for x in sources(row))
    rejected = set(x for row in bad for x in sources(row))
    if accepted & rejected:
        raise DomainError("LINEAGE_OVERLAP", "同一来源行被重复裁决")
    m = {
        "accepted_inputs": len(accepted),
        "quarantine_inputs": len(rejected),
        "output_records": len(rows),
    }
    if intake:
        excluded = intake["duplicate"] + intake["blank"] + intake["repeated_headers"]
        m.update(
            input=intake["input"],
            excluded=excluded,
            balanced=intake["input"] == len(accepted) + len(rejected) + excluded,
        )
    else:
        m.update(input=len(accepted) + len(rejected), excluded=0, balanced=True)
    output = []
    for row in rows:
        expected = Decimal(str(row.get("expected", "0")))
        reported = Decimal(str(row.get("reported", "0")))
        item = {
            **row,
            "difference": str(expected - reported),
            "status": "通过" if expected == reported else "差异",
        }
        orphans.verify(item)
        output.append(item)
    summary = orphans.summary(output)
    m.update(
        expected_total=str(sum((Decimal(str(r.get("expected", "0"))) for r in rows), Decimal(0))),
        reported_total=str(sum((Decimal(str(r.get("reported", "0"))) for r in rows), Decimal(0))),
        differences=sum(r["status"] == "差异" for r in output),
        retirable=summary["retirable"],
        orphan_waiting=summary["orphan_waiting"],
        orphan_settled=summary["orphan_settled"],
        conclusions=summary["conclusions"],
        retirable_ids=summary["retirable_ids"],
        conclusion_digest=summary["conclusion_digest"],
    )
    conclusions = orphans.conclusions(output)
    return {
        "rows": output,
        "quarantine": [],
        "metrics": m,
        "orphan_conclusions": conclusions,
        "conclusion_digest": summary["conclusion_digest"],
    }

import csv
import io
import json


def build_archive(run):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "run_id",
            "input_sha256",
            "rule_version",
            "kind",
            "source_lines",
            "reason",
            "conclusion_id",
            "evidence",
        ]
    )
    meta = run["manifest"]
    for row in run["stages"][-1]["rows"]:
        writer.writerow(
            [
                run["run_id"],
                meta["input_sha256"],
                meta["rule_version"],
                "accepted",
                json.dumps(row.get("_sources", [])),
                row.get("status", ""),
                row.get("conclusion_id", ""),
                json.dumps(row, ensure_ascii=False),
            ]
        )
    for row in run["stages"][-1].get("cumulative_quarantine", []):
        writer.writerow(
            [
                run["run_id"],
                meta["input_sha256"],
                meta["rule_version"],
                "quarantine",
                json.dumps(row.get("_sources", [])),
                row.get("reason", ""),
                "",
                json.dumps(row, ensure_ascii=False),
            ]
        )
    # 封存读取与门禁、对账同一份孤存结论，不再按阶段各自重算。
    for item in run.get("orphan_conclusions") or []:
        writer.writerow(
            [
                run["run_id"],
                meta["input_sha256"],
                meta["rule_version"],
                "conclusion",
                json.dumps(item.get("source_lines", [])),
                item.get("orphan_state", ""),
                item.get("conclusion_id", ""),
                json.dumps(item, ensure_ascii=False),
            ]
        )
    return "﻿" + stream.getvalue()

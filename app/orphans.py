"""孤存文档（索引侧有、源端批次无）的唯一裁决。

可见性重建、延迟门禁、双写对账与最终封存都读取这里的结论，不各自再算一套。

核心规则：
- 缺失记录本身不是撤稿证据，不能直接当作删除。
- 只有源端批次已经补齐越过该索引事件、并且观察期已满，孤存才能转为「可下架」。
- 源端水位来自本批源端记录（或显式参数），绝不能用索引时间自我证明。
- 索引侧本来就是删除墓碑的孤存文档已经不可见，记为「通过」，不再等待。
- 源端记录存在但被隔离时，不能把「隔离」当成「没有」：结论仍为等待。
- 任一条件未满足时只能「等待」；等待既不是延迟，也不是内容差异。
"""
from datetime import timedelta
from decimal import Decimal
import hashlib
import json

from app.errors import DomainError
from app.values import instant, number

ORPHAN_WAITING = "ORPHAN_WAITING"
ORPHAN_RETIRABLE = "ORPHAN_RETIRABLE"
ORPHAN_SETTLED = "ORPHAN_SETTLED"

DEFAULT_AS_OF = "2026-11-16T10:10:00Z"
DEFAULT_GRACE = "600"

BLOCK_SOURCE = "SOURCE_BACKFILL_PENDING"
BLOCK_GRACE = "GRACE_PENDING"
BLOCK_TIME = "INDEX_TIME_MISSING"
BLOCK_REJECTED = "SOURCE_RECORD_REJECTED"

GATES = {
    ORPHAN_WAITING: "等待",
    ORPHAN_RETIRABLE: "可下架",
    ORPHAN_SETTLED: "通过",
}
# 只有「通过」算双写已经对齐；「可下架」是待执行的下架结论，仍计对账差异。
PASS_GATES = {"通过"}
CONCLUSIONS = {
    ORPHAN_WAITING: "等待补齐",
    ORPHAN_RETIRABLE: "可下架",
    ORPHAN_SETTLED: "索引侧已删除",
}
BLOCK_DETAILS = {
    BLOCK_SOURCE: "源端补齐水位未覆盖该文档",
    BLOCK_GRACE: "观察期未满",
    BLOCK_TIME: "索引事件缺少源时刻",
    BLOCK_REJECTED: "源端存在未采纳记录，不能按缺失裁决",
}

_WATERMARK_KEYS = (
    "source_watermark",
    "source_complete_through",
    "source_complete_at",
    "backfill_watermark",
    "source_coverage_end",
)
_GRACE_KEYS = ("orphan_grace_seconds", "orphan_grace", "grace_seconds")


def _first(params, keys):
    for key in keys:
        if params.get(key) not in (None, ""):
            return params[key]
    return None


def _time_of(row):
    """取一条事件的时间基准：优先 source_at，其次 source_time。"""
    raw = row.get("source_at")
    if raw:
        return instant(raw)
    raw = row.get("source_time")
    if raw and str(raw) not in {"", "-", "NULL"}:
        try:
            return instant(raw)
        except DomainError:
            return None
    return None


def _anchor(index_row):
    """孤存文档的观察起点：索引事件自身的源时刻，缺失时退回可见时刻。"""
    return _time_of(index_row) or (
        instant(index_row["visible_at"]) if index_row.get("visible_at") else None
    )


def batch_watermark(rows):
    """源端批次已经补齐到的最晚源时刻；没有源端记录时返回 None。

    水位只从源端记录取，避免用索引侧时间证明源端已经补齐。
    """
    times = [
        _time_of(row)
        for row in rows or []
        if row.get("side") == "source"
    ]
    times = [value for value in times if value is not None]
    return max(times) if times else None


def policy(parameters=None, rows=None):
    """归一孤存裁决使用的同一组时间基准。

    - 显式传入源端水位时以参数为准；
    - 否则取本批源端记录的最晚源时刻；
    - 本批没有任何源端记录时，来源覆盖不明，必须继续等待，不能当作全量撤稿。
    """
    params = parameters or {}
    as_of = instant(params.get("as_of", DEFAULT_AS_OF))
    grace = number(_first(params, _GRACE_KEYS) or DEFAULT_GRACE)
    if grace < 0:
        raise DomainError("BAD_ORPHAN_GRACE", "孤存观察期不能为负")

    explicit = _first(params, _WATERMARK_KEYS)
    batch = batch_watermark(rows)
    if explicit is not None:
        watermark, observe_from, coverage = instant(explicit), instant(explicit), "explicit"
    elif batch is not None:
        watermark, observe_from, coverage = batch, batch, "batch"
    else:
        watermark, observe_from, coverage = None, None, "unavailable"
    return {
        "as_of": as_of,
        "grace": grace,
        "watermark": watermark,
        "observe_from": observe_from,
        "coverage": coverage,
    }


def decide(index_row, values, source_rejected=False):
    """裁决一条孤存索引记录，返回结论、门禁、阻塞项与最早可下架时刻。"""
    as_of, grace, watermark = values["as_of"], values["grace"], values["watermark"]
    anchor = _anchor(index_row)
    age = (
        Decimal(0)
        if anchor is None
        else max(Decimal(0), Decimal(str((as_of - anchor).total_seconds())))
    )

    if index_row.get("op") == "delete":
        state, blockers = ORPHAN_SETTLED, []
        source_covered = time_ready = True
        retirable = False
        eligible_at = None
        detail = "索引侧已是删除墓碑，已撤内容不再可见"
    elif source_rejected:
        state, blockers = ORPHAN_WAITING, [BLOCK_REJECTED]
        source_covered = time_ready = retirable = False
        eligible_at = None
        detail = BLOCK_DETAILS[BLOCK_REJECTED]
    elif anchor is None:
        state, blockers = ORPHAN_WAITING, [BLOCK_TIME]
        source_covered = time_ready = retirable = False
        eligible_at = None
        detail = BLOCK_DETAILS[BLOCK_TIME]
    elif watermark is None:
        state, blockers = ORPHAN_WAITING, [BLOCK_SOURCE]
        source_covered = time_ready = retirable = False
        eligible_at = None
        detail = "本批没有源端记录，无法确认补齐水位"
    else:
        source_covered = anchor <= watermark
        observe_from = values["observe_from"] or anchor
        eligible_at = observe_from + timedelta(seconds=float(grace))
        time_ready = as_of >= eligible_at
        blockers = []
        if not source_covered:
            blockers.append(BLOCK_SOURCE)
        if not time_ready:
            blockers.append(BLOCK_GRACE)
        retirable = not blockers
        state = ORPHAN_RETIRABLE if retirable else ORPHAN_WAITING
        detail = BLOCK_DETAILS[blockers[0]] if blockers else "源端补齐已覆盖且观察期已满"

    return {
        "orphan_state": state,
        "conclusion_code": state,
        "conclusion": CONCLUSIONS[state],
        "gate": GATES[state],
        "retirable": retirable,
        "orphan_since": anchor.isoformat() if anchor else None,
        "orphan_age_seconds": str(age) if anchor else None,
        "orphan_grace_seconds": str(grace),
        "orphan_eligible_at": eligible_at.isoformat() if eligible_at else None,
        "orphan_blockers": blockers,
        "orphan_wait_reason": blockers[0] if blockers else None,
        "orphan_wait_detail": detail,
        "source_covered": source_covered,
        "source_rejected": bool(source_rejected),
        "time_ready": time_ready,
        "coverage": values["coverage"],
        "source_watermark": watermark.isoformat() if watermark else None,
        "evidence_at": as_of.isoformat(),
    }


def evidence(index_row, values, source_rejected=False):
    """结论证据：只保留会改变裁决的事实；重复观察本身不算新证据。"""
    return {
        "doc_id": index_row.get("doc_id"),
        "index": {
            "event_id": index_row.get("event_id"),
            "version": index_row.get("version"),
            "op": index_row.get("op"),
            "source_at": index_row.get("source_at") or index_row.get("source_time"),
            "visible_at": index_row.get("visible_at"),
            "source_lines": sorted(index_row.get("_sources") or []),
        },
        "coverage": values["coverage"],
        "source_watermark": (
            values["watermark"].isoformat()
            if values["coverage"] != "as_of" and values["watermark"]
            else None
        ),
        "grace_seconds": str(values["grace"]),
        "source_rejected": bool(source_rejected),
    }


def conclusion_id(ev, state):
    encoded = json.dumps(
        {"evidence": ev, "state": state},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:20]


def annotate(index_row, values, source_rejected=False):
    """给孤存索引行附加唯一结论，供 M4 之后的各阶段直接读取。"""
    decision = decide(index_row, values, source_rejected)
    ev = evidence(index_row, values, source_rejected)
    return {
        **decision,
        "conclusion_id": conclusion_id(ev, decision["orphan_state"]),
        "conclusion_state": decision["orphan_state"],
        "conclusion_evidence": ev,
    }


def ensure(row, parameters=None, rows=None):
    """下游阶段读取结论；行内没有结论时用同一规则补算。"""
    if row.get("visibility_state") != "ORPHAN_INDEX" or row.get("orphan_state"):
        return row
    return {**row, **annotate(row, policy(parameters, rows))}


def gate(orphan_state):
    """孤存结论到门禁的唯一映射；缺失或未知结论一律按等待处理。"""
    return GATES.get(orphan_state, GATES[ORPHAN_WAITING])


def passed(value):
    """门禁或结论是否已经落地：可下架与已删除都算已有结论。"""
    return value in PASS_GATES


def reported(orphan_state):
    return "1" if passed(GATES.get(orphan_state)) else "0"


def verify(row):
    """校验下游读取的结论、门禁与可下架标记没有分叉。"""
    if row.get("visibility_state") == "ORPHAN_INDEX" and not row.get("orphan_state"):
        raise DomainError("CONCLUSION_MISSING", "孤存文档缺少裁决结论")
    state = row.get("orphan_state")
    if not state:
        return
    if state not in GATES:
        raise DomainError("CONCLUSION_UNKNOWN", "孤存结论不在已知取值内")
    if row.get("gate") is not None and row["gate"] != gate(state):
        raise DomainError("CONCLUSION_DRIFT", "孤存结论与门禁判定不一致")
    if bool(row.get("retirable")) != (state == ORPHAN_RETIRABLE):
        raise DomainError("CONCLUSION_CONFLICT", "孤存结论与可下架标记不一致")
    if row.get("reported") not in (None, reported(state)):
        raise DomainError("CONCLUSION_REPORT_DRIFT", "孤存结论与对账取值不一致")
    if state == ORPHAN_RETIRABLE and not row.get("conclusion_id"):
        raise DomainError("CONCLUSION_MISSING", "可下架结论缺少证据编号")


def conclusions(rows):
    """提取一组阶段的孤存结论，供批次载荷、对账与封存共用。"""
    output = []
    seen = set()
    for row in rows:
        state = row.get("orphan_state")
        if not state:
            continue
        identity = row.get("conclusion_id") or (
            str(row.get("doc_id")) + "|" + state
        )
        if identity in seen:
            continue
        seen.add(identity)
        output.append(
            {
                "doc_id": row.get("doc_id"),
                "conclusion_id": row.get("conclusion_id"),
                "orphan_state": state,
                "conclusion": row.get("conclusion"),
                "gate": gate(state),
                "retirable": bool(row.get("retirable")),
                "expected": "1",
                "reported": reported(state),
                "index_version": row.get("index_version"),
                "index_op": row.get("index_op"),
                "orphan_since": row.get("orphan_since"),
                "orphan_eligible_at": row.get("orphan_eligible_at"),
                "orphan_blockers": list(row.get("orphan_blockers") or []),
                "orphan_wait_reason": row.get("orphan_wait_reason"),
                "orphan_wait_detail": row.get("orphan_wait_detail"),
                "source_lines": sorted(row.get("_sources") or []),
                "evidence": row.get("conclusion_evidence"),
            }
        )
    return sorted(output, key=lambda item: str(item["doc_id"]))


def evidence_digest(rows):
    """对一组已裁决记录生成稳定摘要，对账与封存共用。"""
    pairs = sorted(
        [
            str(row.get("doc_id", "")),
            str(row.get("conclusion_id") or ""),
            str(row.get("orphan_state") or ""),
            str(row.get("gate") or ""),
            "1" if row.get("retirable") else "0",
        ]
        for row in rows
        if row.get("orphan_state")
    )
    encoded = json.dumps(pairs, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def summary(rows):
    orphans = [row for row in rows if row.get("orphan_state")]
    return {
        "conclusions": len(orphans),
        "retirable": sum(row.get("orphan_state") == ORPHAN_RETIRABLE for row in orphans),
        "orphan_waiting": sum(row.get("orphan_state") == ORPHAN_WAITING for row in orphans),
        "orphan_settled": sum(row.get("orphan_state") == ORPHAN_SETTLED for row in orphans),
        "retirable_ids": sorted(
            str(row.get("doc_id"))
            for row in orphans
            if row.get("orphan_state") == ORPHAN_RETIRABLE
        ),
        "conclusion_digest": evidence_digest(rows),
    }

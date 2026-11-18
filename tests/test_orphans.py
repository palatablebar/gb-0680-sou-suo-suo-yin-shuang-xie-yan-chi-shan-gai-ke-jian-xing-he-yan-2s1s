import csv
import io
import json

import pytest

from app import orphans, store
from app.errors import DomainError
from app.modules.m4_visibility.rules import evaluate as visibility
from app.modules.m5_latency.rules import evaluate as latency
from app.modules.m6_parity.rules import evaluate as parity
from app.modules.m7_backfills.rules import evaluate as backfills
from app.pipeline import calculate, execute, load_sample


def index_row(doc="0100", version="3", op="upsert", source_at="2026-11-16T09:50:00Z"):
    return {
        "event_id": "e-" + doc,
        "doc_id": doc,
        "side": "index",
        "version": version,
        "op": op,
        "source_at": source_at,
        "visible_at": source_at,
        "_sources": [1],
        "document": {"name": "x"},
    }


def source_row(doc="0105", source_at="2026-11-16T10:00:00Z"):
    return {
        "event_id": "s-" + doc,
        "doc_id": doc,
        "side": "source",
        "version": "1",
        "op": "upsert",
        "source_at": source_at,
        "visible_at": None,
        "_sources": [2],
        "document": {"name": "y"},
    }


def context(**parameters):
    return {"parameters": parameters, "quarantine": []}


# ---- 裁决条件 -------------------------------------------------------------


def test_orphan_covered_and_grace_elapsed_is_retirable():
    result = visibility(
        [index_row(), source_row()],
        context(as_of="2026-11-16T10:10:00Z"),
    )["rows"][0]
    assert result["visibility_state"] == "ORPHAN_INDEX"
    assert result["orphan_state"] == orphans.ORPHAN_RETIRABLE
    assert result["retirable"] is True
    assert result["gate"] == "可下架"
    assert result["orphan_blockers"] == []


def test_orphan_not_covered_by_source_watermark_keeps_waiting():
    result = visibility(
        [index_row(source_at="2026-11-16T10:05:00Z"), source_row()],
        context(as_of="2026-11-16T11:00:00Z"),
    )["rows"][0]
    assert result["orphan_state"] == orphans.ORPHAN_WAITING
    assert result["retirable"] is False
    assert result["gate"] == "等待"
    assert result["orphan_wait_reason"] == orphans.BLOCK_SOURCE
    # 源端仍在补齐时，缺记录不得当成撤稿。
    assert result["reported"] == "0"


def test_orphan_grace_boundary_equal_passes():
    result = visibility(
        [index_row(), source_row()],
        context(as_of="2026-11-16T10:10:00Z", orphan_grace_seconds="600"),
    )["rows"][0]
    assert result["orphan_state"] == orphans.ORPHAN_RETIRABLE
    early = visibility(
        [index_row(), source_row()],
        context(as_of="2026-11-16T10:09:59Z", orphan_grace_seconds="600"),
    )["rows"][0]
    assert early["orphan_state"] == orphans.ORPHAN_WAITING
    assert early["orphan_wait_reason"] == orphans.BLOCK_GRACE


def test_orphan_delete_tombstone_is_settled():
    result = visibility([index_row(op="delete"), source_row()], context())["rows"][0]
    assert result["orphan_state"] == orphans.ORPHAN_SETTLED
    assert result["gate"] == "通过"
    assert result["retirable"] is False
    assert result["reported"] == "1"


def test_orphan_without_source_rows_stays_waiting():
    result = visibility([index_row()], context(as_of="2027-01-01T00:00:00Z"))["rows"][0]
    assert result["orphan_state"] == orphans.ORPHAN_WAITING
    assert result["coverage"] == "unavailable"
    assert result["retirable"] is False


def test_explicit_watermark_can_release_orphan():
    result = visibility(
        [index_row()],
        context(
            as_of="2026-11-16T10:10:00Z",
            source_watermark="2026-11-16T10:00:00Z",
        ),
    )["rows"][0]
    assert result["orphan_state"] == orphans.ORPHAN_RETIRABLE
    assert result["coverage"] == "explicit"


def test_rejected_source_record_blocks_orphan():
    rejected = {**source_row(doc="0100"), "reason": "SAME_VERSION_CONFLICT"}
    result = visibility([index_row()], {"parameters": {}, "quarantine": [rejected]})["rows"][0]
    assert result["orphan_state"] == orphans.ORPHAN_WAITING
    assert result["orphan_wait_reason"] == orphans.BLOCK_REJECTED
    assert result["retirable"] is False


def test_negative_orphan_grace_rejected():
    with pytest.raises(DomainError):
        visibility([index_row()], context(orphan_grace_seconds="-1"))


# ---- 同一结论贯穿门禁、对账与封存 ------------------------------------------


def test_same_conclusion_feeds_latency_parity_and_archive():
    stage4 = visibility([index_row(), source_row()], context(as_of="2026-11-16T10:10:00Z"))
    stage5 = latency(stage4["rows"], context(as_of="2026-11-16T10:10:00Z"))
    stage6 = parity(stage5["rows"], context(as_of="2026-11-16T10:10:00Z"))
    stage7 = backfills(stage6["rows"], {"parameters": {}})
    chain = [stage4, stage5, stage6, stage7]
    orphans_rows = [stage["rows"][0] for stage in chain]
    assert len({row["conclusion_id"] for row in orphans_rows}) == 1
    assert len({row["orphan_state"] for row in orphans_rows}) == 1
    assert all(row["orphan_state"] == orphans.ORPHAN_RETIRABLE for row in orphans_rows)
    assert stage7["metrics"]["retirable"] == 1
    assert stage7["orphan_conclusions"][0]["conclusion_id"] == stage6["rows"][0]["conclusion_id"]


def test_stage_metrics_share_conclusion_digest():
    run = calculate(load_sample("orphans.csv"))
    digests = {run["stages"][i]["metrics"]["conclusion_digest"] for i in (3, 4, 5, 6)}
    assert len(digests) == 1
    assert run["conclusion_digest"] == digests.pop()


def test_tampered_conclusion_is_rejected():
    stage4 = visibility([index_row(), source_row()], context(as_of="2026-11-16T10:10:00Z"))
    tampered = {**stage4["rows"][0], "gate": "等待"}
    with pytest.raises(DomainError) as error:
        latency([tampered], context(as_of="2026-11-16T10:10:00Z"))
    assert error.value.code == "CONCLUSION_DRIFT"


# ---- 样例与批次 ----------------------------------------------------------


def test_real_orphan_sample_states():
    run = calculate(load_sample("orphans.csv"))
    states = {row["doc_id"]: row for row in run["stages"][3]["rows"] if row.get("orphan_state")}
    assert states["0100"]["orphan_state"] == orphans.ORPHAN_RETIRABLE
    assert states["0101"]["orphan_state"] == orphans.ORPHAN_RETIRABLE
    assert states["0102"]["orphan_state"] == orphans.ORPHAN_SETTLED
    assert states["0103"]["orphan_state"] == orphans.ORPHAN_WAITING
    assert run["stages"][3]["metrics"]["retirable"] == 2


def test_real_archive_contains_same_conclusion(client):
    run = client.post("/api/runs", json={"sample": "orphans.csv"}).get_json()
    response = client.get("/api/runs/" + run["run_id"] + "/archive")
    rows = list(csv.reader(io.StringIO(response.data.decode("utf-8-sig"))))
    conclusions = [row for row in rows[1:] if row[3] == "conclusion"]
    assert len(conclusions) == len(run["orphan_conclusions"])
    assert {row[6] for row in conclusions} == {
        item["conclusion_id"] for item in run["orphan_conclusions"]
    }


def test_retry_does_not_duplicate_old_conclusions(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    sample = load_sample("orphans.csv")
    first = execute(sample, {"batch_id": "B1"})
    execute(sample, {"batch_id": "B1"})
    assert store.conclusions(batch="B1") == store.conclusions(first["run_id"])
    # 同一批同一输入重试只落一份结论。
    assert len(store.conclusion_history()) == len(first["orphan_conclusions"])


def test_new_evidence_supersedes_and_old_conclusion_recoverable(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    raw = load_sample("orphans.csv").decode("utf-8-sig")
    first = execute(raw.encode(), {"batch_id": "B1"})
    assert any(item["doc_id"] == "0100" for item in store.conclusions())

    # 补发带上 0100 的源端记录：该文档不再是孤存，旧「可下架」结论不得继续出现。
    backfilled = raw.replace(
        "1012,0105,1,upsert,2026-11-16T10:00:00Z,-,source,",
        '1013,0100,3,upsert,2026-11-16T09:50:00Z,-,source,"{""name"":""已撤文档""}",1,补发\n'
        "1012,0105,1,upsert,2026-11-16T10:00:00Z,-,source,",
    )
    second = execute(backfilled.encode(), {"batch_id": "B1"})
    current = [item for item in store.conclusions() if item["doc_id"] == "0100"]
    assert current == []
    history = [item for item in store.conclusion_history(doc_id="0100")]
    assert history and all(item["conclusion_id"] for item in history)
    # 原始记录仍可从批次载荷追回。
    records = store.raw_records(second["run_id"], "0100")
    assert records["input_sha256"] and records["rows"]
    assert records["rows"][0]["_sources"]


def test_unchanged_conclusion_survives_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    raw = load_sample("orphans.csv").decode("utf-8-sig")
    execute(raw.encode(), {"batch_id": "B1"})
    # 只补一条无关源端记录，观察期和证据不变：结论编号保持稳定，不重复。
    retried = raw + ('1014,0199,1,upsert,2026-11-16T10:00:00Z,-,source,'
                     '"{""name"":""其他""}",1,补发\n')
    execute(retried.encode(), {"batch_id": "B1"})
    current = store.conclusions(batch="B1")
    ids = [item["conclusion_id"] for item in current]
    assert len(ids) == len(set(ids))
    assert {item["doc_id"] for item in current} == {"0100", "0101", "0102", "0103"}


def test_new_evidence_changes_state_without_duplicate_rows(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "DATA_DIR", tmp_path)
    raw = load_sample("orphans.csv").decode("utf-8-sig")
    execute(raw.encode(), {"batch_id": "B2"})
    # 0100 在首批里已可下架；把观察期参数收紧到 1 小时，同一文档退回等待。
    execute(raw.encode(), {"batch_id": "B2", "orphan_grace_seconds": "3600"})
    current = {item["doc_id"]: item for item in store.conclusions(batch="B2")}
    assert current["0100"]["orphan_state"] == orphans.ORPHAN_WAITING
    assert current["0100"]["gate"] == "等待"
    # 旧的可下架结论只出现在历史里，当前视图不重复。
    history = store.conclusion_history(doc_id="0100", batch="B2")
    assert {item["orphan_state"] for item in history} == {"ORPHAN_RETIRABLE", "ORPHAN_WAITING"}
    assert current["0100"]["conclusion_id"] in {item["conclusion_id"] for item in history}


def test_conclusions_endpoints(client):
    run = client.post("/api/runs", json={"sample": "orphans.csv"}).get_json()
    current = client.get("/api/runs/" + run["run_id"] + "/conclusions").get_json()
    assert current["conclusion_digest"] == run["conclusion_digest"]
    assert {item["doc_id"] for item in current["conclusions"]} == {"0100", "0101", "0102", "0103"}
    history = client.get("/api/conclusions/history?doc_id=0100").get_json()["conclusions"]
    assert history
    records = client.get("/api/runs/" + run["run_id"] + "/records?doc_id=0100").get_json()
    assert records["rows"][0]["visibility_state"] == "ORPHAN_INDEX"

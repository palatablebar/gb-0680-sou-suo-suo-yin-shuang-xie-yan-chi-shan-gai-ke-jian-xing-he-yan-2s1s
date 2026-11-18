import json
import sqlite3

from app.config import DATA_DIR
from app.errors import DomainError

RUNS_TABLE = (
    "CREATE TABLE IF NOT EXISTS runs ("
    "id TEXT PRIMARY KEY,"
    "payload TEXT NOT NULL,"
    "version INTEGER NOT NULL DEFAULT 1,"
    "decision TEXT NOT NULL DEFAULT '待复核'"
    ")"
)
# 每次落库都保留结论；同批补发重试按 (批次, 文档, 证据编号) 去重。
# 旧证据算出的结论仍留在库里可追回，但同一批次只展示最新一次补发确认的结论。
CONCLUSIONS_TABLE = (
    "CREATE TABLE IF NOT EXISTS conclusions ("
    "batch_key TEXT NOT NULL,"
    "run_id TEXT NOT NULL,"
    "doc_id TEXT NOT NULL,"
    "conclusion_id TEXT NOT NULL,"
    "orphan_state TEXT NOT NULL,"
    "gate TEXT NOT NULL,"
    "retirable INTEGER NOT NULL DEFAULT 0,"
    "payload TEXT NOT NULL,"
    "PRIMARY KEY (batch_key, doc_id, conclusion_id)"
    ")"
)
# 批次游标：即使补发后孤存文档已经消失（没有结论可写），也要把该批推进到新运行，
# 否则旧结论会被误当成当前结论继续展示。
BATCHES_TABLE = (
    "CREATE TABLE IF NOT EXISTS batches ("
    "batch_key TEXT PRIMARY KEY,"
    "run_id TEXT NOT NULL,"
    "updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
    ")"
)


def connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "records.db", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(RUNS_TABLE)
    db.execute(CONCLUSIONS_TABLE)
    db.execute(BATCHES_TABLE)
    return db


def batch_key(payload, run_id):
    """同一批补发共享的批次键：优先显式批次标识，其次本次运行号。"""
    parameters = payload.get("parameters") or {}
    explicit = (
        parameters.get("batch_id")
        or parameters.get("backfill_batch_id")
        or parameters.get("batch")
    )
    return str(explicit or run_id)


def _record_conclusions(db, run_id, payload):
    key = batch_key(payload, run_id)
    db.execute(
        "INSERT INTO batches(batch_key,run_id) VALUES (?,?) "
        "ON CONFLICT(batch_key) DO UPDATE SET run_id=excluded.run_id,updated_at=CURRENT_TIMESTAMP",
        (key, run_id),
    )
    for item in payload.get("orphan_conclusions") or []:
        # 同一批重试不新增行：同一条证据得出的结论幂等；重新确认时把游标推进到本次运行。
        db.execute(
            "INSERT INTO conclusions"
            "(batch_key,run_id,doc_id,conclusion_id,orphan_state,gate,retirable,payload)"
            " VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT(batch_key,doc_id,conclusion_id) DO UPDATE SET"
            " run_id=excluded.run_id,payload=excluded.payload",
            (
                key,
                run_id,
                str(item.get("doc_id", "")),
                str(item.get("conclusion_id", "")),
                str(item.get("orphan_state", "")),
                str(item.get("gate", "")),
                1 if item.get("retirable") else 0,
                json.dumps(item, ensure_ascii=False),
            ),
        )


def save(run_id, payload):
    with connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO runs(id,payload) VALUES (?,?)",
            (run_id, json.dumps(payload, ensure_ascii=False)),
        )
        _record_conclusions(db, run_id, payload)
    return get(run_id)


def get(run_id):
    with connect() as db:
        row = db.execute("SELECT payload,version,decision FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "批次不存在", 404)
    return {**json.loads(row[0]), "version": row[1], "decision": row[2]}


def latest():
    with connect() as db:
        row = db.execute("SELECT id FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return get(row[0]) if row else None


def history():
    with connect() as db:
        return [
            {"run_id": i, "version": v, "decision": d, "conclusions": c}
            for i, v, d, c in db.execute(
                "SELECT r.id,r.version,r.decision,"
                "(SELECT COUNT(*) FROM conclusions c WHERE c.run_id=r.id) "
                "FROM runs r ORDER BY r.rowid DESC"
            )
        ]


def conclusions(run_id=None, batch=None):
    """孤存结论视图。

    - 指定 run_id：返回该运行载荷里的结论快照（封存证据以它为准，永不随重试变化）。
    - 指定 batch：返回该补发批当前有效的结论。
    - 都不指定：每个批次键只取最新一次补发。
    旧结论留在库里，可用 conclusion_history 追回，不会在批次当前视图中重复出现。
    """
    if run_id is not None:
        return [
            {"run_id": run_id, **item}
            for item in get(run_id).get("orphan_conclusions") or []
        ]
    with connect() as db:
        active = dict(db.execute("SELECT batch_key,run_id FROM batches").fetchall())
        if batch is not None:
            rows = db.execute(
                "SELECT batch_key,run_id,payload FROM conclusions "
                "WHERE batch_key=? ORDER BY rowid",
                (str(batch),),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT batch_key,run_id,payload FROM conclusions ORDER BY rowid"
            ).fetchall()
        rows = [r for r in rows if active.get(r[0]) == r[1]]
    output, seen = [], set()
    for batch, run, payload in rows:
        item = json.loads(payload)
        identity = (run, str(item.get("doc_id")), str(item.get("conclusion_id")))
        if identity in seen:
            continue
        seen.add(identity)
        output.append({"run_id": run, **item})
    return output


def conclusion_history(doc_id=None, batch=None):
    """全部历史结论，含已被新证据取代的旧结论与原始证据。"""
    query = "SELECT batch_key,run_id,payload FROM conclusions"
    clauses, args = [], []
    if batch is not None:
        clauses.append("batch_key=?")
        args.append(batch)
    if doc_id is not None:
        clauses.append("doc_id=?")
        args.append(str(doc_id))
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY rowid"
    with connect() as db:
        rows = db.execute(query, args).fetchall()
    return [{"batch_key": key, "run_id": run_id, **json.loads(payload)} for key, run_id, payload in rows]


def raw_records(run_id, doc_id=None):
    """原始记录追回：直接回到批次载荷与源行号，不依赖派生结论。"""
    run = get(run_id)
    rows = run["stages"][-1]["rows"]
    if doc_id is not None:
        rows = [row for row in rows if str(row.get("doc_id")) == str(doc_id)]
    return {
        "run_id": run_id,
        "input_sha256": run["manifest"]["input_sha256"],
        "rule_version": run["manifest"]["rule_version"],
        "rows": rows,
    }


def review(run_id, version, decision):
    if decision not in {"已复核", "退回复核"}:
        raise DomainError("BAD_DECISION", "复核状态无效")
    with connect() as db:
        cursor = db.execute(
            "UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?",
            (decision, run_id, version),
        )
        if cursor.rowcount != 1:
            if not db.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone():
                raise DomainError("NOT_FOUND", "批次不存在", 404)
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
    return get(run_id)

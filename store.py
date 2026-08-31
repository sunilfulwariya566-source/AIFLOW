"""SQLite persistence: workflows (versioned), runs, schedules, api keys, vector docs."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
DB_PATH = os.path.join(DATA, "aiflow.db")
_LOCK = threading.RLock()
_local = threading.local()


def conn() -> sqlite3.Connection:
    c = getattr(_local, "c", None)
    if c is None:
        os.makedirs(DATA, exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA busy_timeout=5000")
        _local.c = c
    return c


SCHEMA = """
CREATE TABLE IF NOT EXISTS workflows(
  name TEXT PRIMARY KEY, description TEXT, nodes TEXT NOT NULL,
  version INTEGER DEFAULT 1, updated_at REAL, on_error TEXT DEFAULT 'stop',
  layout TEXT DEFAULT '{}', budget TEXT DEFAULT '{}', parallel INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS versions(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, version INTEGER,
  doc TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY, workflow TEXT, status TEXT, provider TEXT,
  trigger TEXT, duration_ms INTEGER, tokens_in INTEGER DEFAULT 0,
  tokens_out INTEGER DEFAULT 0, cost_usd REAL DEFAULT 0,
  created_at REAL, doc TEXT);
CREATE TABLE IF NOT EXISTS schedules(
  id TEXT PRIMARY KEY, workflow TEXT, every_seconds INTEGER, payload TEXT,
  provider TEXT, enabled INTEGER DEFAULT 1, runs INTEGER DEFAULT 0,
  next_run REAL, created_at REAL, cron TEXT);
CREATE TABLE IF NOT EXISTS apikeys(
  key TEXT PRIMARY KEY, label TEXT, scopes TEXT, created_at REAL,
  last_used REAL, calls INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS approvals(
  id TEXT PRIMARY KEY, run_id TEXT, workflow TEXT, node TEXT, prompt TEXT,
  payload TEXT, status TEXT DEFAULT 'pending', decided_by TEXT,
  comment TEXT, created_at REAL, decided_at REAL);
CREATE TABLE IF NOT EXISTS docs(
  id TEXT PRIMARY KEY, collection TEXT, text TEXT, meta TEXT,
  vec TEXT, created_at REAL);
CREATE TABLE IF NOT EXISTS audit(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL, actor TEXT, scopes TEXT,
  action TEXT, target TEXT, detail TEXT, ip TEXT);
CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit(ts DESC);
CREATE INDEX IF NOT EXISTS ix_audit_target ON audit(target, ts DESC);
CREATE TABLE IF NOT EXISTS cache(
  key TEXT PRIMARY KEY, workflow TEXT, node TEXT, value TEXT,
  usage TEXT, hits INTEGER DEFAULT 0, created_at REAL, last_hit REAL);
CREATE INDEX IF NOT EXISTS ix_cache_wf ON cache(workflow, node);
CREATE TABLE IF NOT EXISTS alerts(
  id TEXT PRIMARY KEY, name TEXT, workflow TEXT, metric TEXT, op TEXT,
  threshold REAL, window_runs INTEGER DEFAULT 20, channel TEXT, target TEXT,
  enabled INTEGER DEFAULT 1, cooldown_s INTEGER DEFAULT 300,
  last_fired REAL, fires INTEGER DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS alert_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, alert_id TEXT, name TEXT, workflow TEXT,
  metric TEXT, value REAL, threshold REAL, message TEXT,
  delivered INTEGER DEFAULT 0, detail TEXT, created_at REAL);
CREATE INDEX IF NOT EXISTS ix_alert_ev ON alert_events(created_at DESC);
CREATE INDEX IF NOT EXISTS ix_runs_wf ON runs(workflow, created_at DESC);
CREATE INDEX IF NOT EXISTS ix_docs_col ON docs(collection);
CREATE INDEX IF NOT EXISTS ix_appr ON approvals(status, created_at DESC);
"""

SEED: List[Dict[str, Any]] = [
    {
        "name": "support-triage",
        "description": "Classify a ticket, score priority, gate refunds on human approval, draft a reply.",
        "on_error": "stop",
        "nodes": [
            {"id": "ticket", "type": "input", "params": {"key": "ticket", "required": True}},
            {"id": "customer", "type": "input", "params": {"key": "customer", "default": "Unknown"}},
            {"id": "analysis", "type": "llm", "params": {
                "system": "You are a support triage engine. Return strict JSON.",
                "prompt": "Classify this ticket. Fields: category, priority, sentiment, summary.\n\nTICKET:\n{{ticket}}",
                "json_mode": True}, "retries": 2, "on_error": "stop"},
            {"id": "parsed", "type": "python", "params": {"expr": "json.loads(analysis)"}},
            {"id": "urgent", "type": "branch", "params": {
                "condition": "parsed['priority'] == 'high' or 'urgent' in str(ticket).lower()",
                "if_true": "PAGE_ONCALL", "if_false": "NORMAL_QUEUE"}},
            {"id": "reply", "type": "llm", "params": {
                "system": "You are a warm, concise support agent.",
                "prompt": "Draft a reply to {{customer}} about: {{parsed.summary}} (category {{parsed.category}})."},
                "on_error": "fallback", "fallback": "Thanks for reaching out — we're looking into this."},
            {"id": "result", "type": "output", "params": {"value": {
                "customer": "{{customer}}", "routing": "{{urgent}}",
                "category": "{{parsed.category}}", "priority": "{{parsed.priority}}",
                "sentiment": "{{parsed.sentiment}}", "draft_reply": "{{reply}}"}}},
        ],
    },
    {
        "name": "doc-summarize-chain",
        "description": "Summarize a document, extract action items, count words, emit a digest.",
        "nodes": [
            {"id": "doc", "type": "input", "params": {"key": "document", "required": True}},
            {"id": "summary", "type": "llm", "params": {
                "system": "Summarize faithfully.",
                "prompt": "Summarize the document below in 3 sentences.\n\n{{doc}}"}},
            {"id": "actions", "type": "llm", "params": {
                "system": "Extract action items. Return strict JSON.",
                "prompt": "Extract action items from:\n{{doc}}", "json_mode": True}},
            {"id": "wordcount", "type": "python", "params": {"expr": "len(str(doc).split())"}},
            {"id": "digest", "type": "output", "params": {"value": {
                "words_in": "{{wordcount}}", "summary": "{{summary}}", "extracted": "{{actions}}"}}},
        ],
    },
    {
        "name": "batch-review-miner",
        "description": "Fan out over reviews: classify in parallel, filter negatives, validate, aggregate.",
        "nodes": [
            {"id": "reviews", "type": "input", "params": {"key": "reviews", "required": True}},
            {"id": "scored", "type": "map", "params": {
                "over": "{{reviews}}", "as": "item", "workers": 4,
                "step": {"type": "llm", "params": {
                    "system": "Return strict JSON with category, priority, sentiment, summary.",
                    "prompt": "Classify this customer review:\n{{item}}", "json_mode": True}}}},
            {"id": "objs", "type": "python", "params": {"expr": "[json.loads(s) for s in scored]"}},
            {"id": "negative", "type": "filter", "params": {
                "over": "{{objs}}", "as": "item", "condition": "item['sentiment'] == 'negative'"}},
            {"id": "check", "type": "validate", "params": {
                "value": "{{objs.0}}", "soft": True,
                "schema": {"required": ["category", "priority", "sentiment"],
                            "properties": {"priority": {"type": "string",
                                                        "enum": ["low", "medium", "high"]}}}}},
            {"id": "counts", "type": "python", "params": {
                "expr": "{'total': len(objs), 'negative': len(negative), "
                        "'high_priority': len([o for o in objs if o['priority']=='high'])}"}},
            {"id": "report", "type": "output", "params": {"value": {
                "counts": "{{counts}}", "schema_ok": "{{check.valid}}",
                "negative_reviews": "{{negative}}"}}},
        ],
    },
    {
        "name": "content-pipeline",
        "description": "Outline -> draft -> teaser, with a human approval gate before publish.",
        "nodes": [
            {"id": "topic", "type": "input", "params": {"key": "topic", "required": True}},
            {"id": "tone", "type": "input", "params": {"key": "tone", "default": "practical"}},
            {"id": "outline", "type": "llm", "params": {
                "prompt": "Write an outline for a post about {{topic}} in a {{tone}} tone."}},
            {"id": "post", "type": "llm", "params": {
                "prompt": "Write the post following this outline:\n{{outline}}"}},
            {"id": "teaser", "type": "llm", "params": {
                "prompt": "Summarize this post as a 1-line teaser:\n{{post}}"}},
            {"id": "signoff", "type": "approval", "params": {
                "prompt": "Approve publishing the post about {{topic}}?",
                "value": "{{teaser}}", "auto_approve_after": 0}},
            {"id": "bundle", "type": "output", "params": {"value": {
                "topic": "{{topic}}", "outline": "{{outline}}", "post": "{{post}}",
                "teaser": "{{teaser}}", "approved": "{{signoff.approved}}"}}},
        ],
    },
    {
        "name": "lib-classify",
        "description": "Reusable building block: classify any text into JSON. Call it from other workflows.",
        "nodes": [
            {"id": "text", "type": "input", "params": {"key": "text", "required": True}},
            {"id": "raw", "type": "llm", "params": {
                "system": "Classify the text. Return strict JSON with category, priority, sentiment, summary.",
                "prompt": "{{text}}", "json_mode": True}, "retries": 2},
            {"id": "parsed", "type": "python", "params": {"expr": "json.loads(raw)"}},
            {"id": "out", "type": "output", "params": {"value": "{{parsed}}"}},
        ],
    },
    {
        "name": "smart-router",
        "description": "Sub-workflow + conditional edges: classify once, then only the matching branch runs.",
        "nodes": [
            {"id": "message", "type": "input", "params": {"key": "message", "required": True}},
            {"id": "triage", "type": "workflow", "params": {
                "workflow": "lib-classify", "payload": {"text": "{{message}}"}}},
            {"id": "urgent", "type": "llm",
             "when": "triage.priority == 'high'",
             "params": {"system": "You are an on-call engineer.",
                        "prompt": "Write a 1-line incident summary for: {{message}}"}},
            {"id": "page", "type": "template",
             "params": {"text": "PAGED on-call: {{urgent}}"}},
            {"id": "normal", "type": "llm",
             "when": "triage.priority != 'high'",
             "params": {"system": "You are a support agent.",
                        "prompt": "Draft a friendly reply to: {{message}}"}},
            {"id": "result", "type": "output", "params": {"value": {
                "category": "{{triage.category}}", "priority": "{{triage.priority}}",
                "escalation": "{{page}}", "reply": "{{normal}}"}}},
        ],
    },
    {
        "name": "kb-rag-answer",
        "description": "Chunk + index a knowledge base, retrieve relevant passages, answer grounded.",
        "nodes": [
            {"id": "kb", "type": "input", "params": {"key": "knowledge", "required": True}},
            {"id": "question", "type": "input", "params": {"key": "question", "required": True}},
            {"id": "chunks", "type": "chunk", "params": {"text": "{{kb}}", "size": 240, "overlap": 40}},
            {"id": "indexed", "type": "embed", "params": {
                "collection": "kb", "texts": "{{chunks}}", "reset": True}},
            {"id": "hits", "type": "retrieve", "params": {
                "collection": "kb", "query": "{{question}}", "k": 3},
                "depends_on": ["indexed"]},
            {"id": "answer", "type": "llm", "params": {
                "system": "Answer ONLY from the context. If unknown, say so.",
                "prompt": "CONTEXT:\n{{hits.context}}\n\nQUESTION: {{question}}"}},
            {"id": "out", "type": "output", "params": {"value": {
                "question": "{{question}}", "chunks_indexed": "{{indexed.count}}",
                "sources": "{{hits.matches}}", "answer": "{{answer}}"}}},
        ],
    },
    {
        "name": "agent-research",
        "description": "Autonomous agent loop: the model picks tools (search/calc/kb) until it can answer.",
        "nodes": [
            {"id": "goal", "type": "input", "params": {"key": "goal", "required": True}},
            {"id": "run", "type": "agent", "params": {
                "goal": "{{goal}}", "max_steps": 5,
                "tools": ["search", "calculator", "kb_lookup", "finish"]}},
            {"id": "out", "type": "output", "params": {"value": {
                "goal": "{{goal}}", "answer": "{{run.answer}}",
                "steps": "{{run.steps}}", "trace": "{{run.trace}}"}}},
        ],
    },
]


# --------------------------------------------------------------------------- #
def _migrate(c):
    """Additive migrations so existing databases keep working."""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(workflows)").fetchall()}
    if "layout" not in cols:
        c.execute("ALTER TABLE workflows ADD COLUMN layout TEXT DEFAULT '{}'")
        c.commit()
    if "budget" not in cols:
        c.execute("ALTER TABLE workflows ADD COLUMN budget TEXT DEFAULT '{}'")
        c.commit()
    if "parallel" not in cols:
        c.execute("ALTER TABLE workflows ADD COLUMN parallel INTEGER DEFAULT 0")
        c.commit()
    scols = {r["name"] for r in c.execute("PRAGMA table_info(schedules)").fetchall()}
    if scols and "cron" not in scols:
        c.execute("ALTER TABLE schedules ADD COLUMN cron TEXT")
        c.commit()


def init():
    with _LOCK:
        c = conn()
        c.executescript(SCHEMA)
        c.commit()
        _migrate(c)
        if not c.execute("SELECT 1 FROM workflows LIMIT 1").fetchone():
            for w in SEED:
                save_workflow(w, seed=True)
        if not c.execute("SELECT 1 FROM apikeys LIMIT 1").fetchone():
            # Dev convenience: a predictable key so the bundled UI works offline.
            # In production set AIFLOW_ADMIN_KEY (or AIFLOW_AUTH=0 for a private box)
            # so the admin key is never the published default.
            seeded = os.environ.get("AIFLOW_ADMIN_KEY") or "aiflow-dev-key"
            create_key("default-admin", ["admin"], key=seeded)


# ------------------------------ workflows ---------------------------------- #
def _row_to_wf(r) -> Dict[str, Any]:
    return {"name": r["name"], "description": r["description"] or "",
            "nodes": json.loads(r["nodes"]), "version": r["version"],
            "on_error": r["on_error"] or "stop", "updated_at": r["updated_at"],
            "layout": json.loads((r["layout"] if "layout" in r.keys() else None) or "{}"),
            "budget": json.loads((r["budget"] if "budget" in r.keys() else None) or "{}"),
            "parallel": (r["parallel"] if "parallel" in r.keys() else 0) or 0}


def list_workflows() -> List[Dict[str, Any]]:
    return [_row_to_wf(r) for r in conn().execute(
        "SELECT * FROM workflows ORDER BY name").fetchall()]


def get_workflow(name: str) -> Optional[Dict[str, Any]]:
    r = conn().execute("SELECT * FROM workflows WHERE name=?", (name,)).fetchone()
    return _row_to_wf(r) if r else None


def save_workflow(wf: Dict[str, Any], seed: bool = False) -> Dict[str, Any]:
    with _LOCK:
        c = conn()
        prev = c.execute("SELECT version FROM workflows WHERE name=?", (wf["name"],)).fetchone()
        version = (prev["version"] + 1) if prev else 1
        now = time.time()
        c.execute(
            "INSERT INTO workflows(name,description,nodes,version,updated_at,on_error,"
            "layout,budget,parallel) VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET "
            "description=excluded.description,nodes=excluded.nodes,"
            "version=excluded.version,updated_at=excluded.updated_at,"
            "on_error=excluded.on_error,layout=excluded.layout,budget=excluded.budget,"
            "parallel=excluded.parallel",
            (wf["name"], wf.get("description", ""), json.dumps(wf.get("nodes", [])),
             version, now, wf.get("on_error", "stop"),
             json.dumps(wf.get("layout", {})), json.dumps(wf.get("budget", {})),
             int(wf.get("parallel") or 0)))
        c.execute("INSERT INTO versions(name,version,doc,created_at) VALUES(?,?,?,?)",
                  (wf["name"], version, json.dumps(wf), now))
        c.commit()
    return get_workflow(wf["name"])


def save_layout(name: str, layout: Dict[str, Any]):
    """Persist canvas node positions without creating a new version."""
    with _LOCK:
        conn().execute("UPDATE workflows SET layout=? WHERE name=?",
                       (json.dumps(layout or {}), name))
        conn().commit()
    return get_workflow(name)


def delete_workflow(name: str):
    with _LOCK:
        conn().execute("DELETE FROM workflows WHERE name=?", (name,))
        conn().execute("DELETE FROM versions WHERE name=?", (name,))
        conn().commit()


def list_versions(name: str) -> List[Dict[str, Any]]:
    return [{"version": r["version"], "created_at": r["created_at"],
             "doc": json.loads(r["doc"])}
            for r in conn().execute(
                "SELECT * FROM versions WHERE name=? ORDER BY version DESC LIMIT 30",
                (name,)).fetchall()]


def get_version(name: str, version: int):
    r = conn().execute("SELECT doc FROM versions WHERE name=? AND version=?",
                       (name, version)).fetchone()
    return json.loads(r["doc"]) if r else None


def rollback(name: str, version: int):
    r = conn().execute("SELECT doc FROM versions WHERE name=? AND version=?",
                       (name, version)).fetchone()
    if not r:
        return None
    return save_workflow(json.loads(r["doc"]))


# ------------------------------ runs --------------------------------------- #
def add_run(run: Dict[str, Any]):
    u = run.get("usage") or {}
    with _LOCK:
        conn().execute(
            "INSERT OR REPLACE INTO runs(run_id,workflow,status,provider,trigger,"
            "duration_ms,tokens_in,tokens_out,cost_usd,created_at,doc) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run["run_id"], run.get("workflow"), run.get("status"), run.get("provider"),
             run.get("trigger", "manual"), run.get("duration_ms", 0),
             u.get("tokens_in", 0), u.get("tokens_out", 0), u.get("cost_usd", 0.0),
             time.time(), json.dumps(run)))
        conn().commit()
    return run


def update_run(run: Dict[str, Any]):
    return add_run(run)


def get_run(run_id: str):
    r = conn().execute("SELECT doc FROM runs WHERE run_id=?", (run_id,)).fetchone()
    return json.loads(r["doc"]) if r else None


def list_runs(limit: int = 30, workflow: str = None, status: str = None,
              offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT doc FROM runs WHERE 1=1"
    a: List[Any] = []
    if workflow:
        q += " AND workflow=?"; a.append(workflow)
    if status:
        q += " AND status=?"; a.append(status)
    q += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
    a += [limit, offset]
    return [json.loads(r["doc"]) for r in conn().execute(q, a).fetchall()]


def stats() -> Dict[str, Any]:
    c = conn()
    tot = c.execute("SELECT COUNT(*) n, COALESCE(SUM(cost_usd),0) c, "
                    "COALESCE(SUM(tokens_in+tokens_out),0) t FROM runs").fetchone()
    ok = c.execute("SELECT COUNT(*) n FROM runs WHERE status='success'").fetchone()["n"]
    per = [dict(r) for r in c.execute(
        "SELECT workflow, COUNT(*) runs, "
        "SUM(status='success') ok, ROUND(AVG(duration_ms)) avg_ms, "
        "ROUND(SUM(cost_usd),4) cost FROM runs GROUP BY workflow "
        "ORDER BY runs DESC").fetchall()]
    durs = [r["duration_ms"] for r in c.execute(
        "SELECT duration_ms FROM runs ORDER BY duration_ms").fetchall()]
    p95 = durs[int(len(durs) * 0.95)] if durs else 0
    recent = [dict(r) for r in c.execute(
        "SELECT status, duration_ms, created_at FROM runs "
        "ORDER BY created_at DESC LIMIT 40").fetchall()]
    return {"total_runs": tot["n"], "success": ok,
            "success_rate": round(100.0 * ok / tot["n"], 1) if tot["n"] else 0.0,
            "total_cost_usd": round(tot["c"], 4), "total_tokens": tot["t"],
            "p95_ms": p95, "per_workflow": per, "recent": recent}


# ------------------------------ schedules ---------------------------------- #
def list_schedules() -> List[Dict[str, Any]]:
    return [{"id": r["id"], "workflow": r["workflow"], "every_seconds": r["every_seconds"],
             "payload": json.loads(r["payload"] or "{}"), "provider": r["provider"],
             "enabled": bool(r["enabled"]), "runs": r["runs"], "next_run": r["next_run"],
             "cron": (r["cron"] if "cron" in r.keys() else None)}
            for r in conn().execute("SELECT * FROM schedules ORDER BY created_at").fetchall()]


def add_schedule(s: Dict[str, Any]):
    with _LOCK:
        conn().execute(
            "INSERT OR REPLACE INTO schedules(id,workflow,every_seconds,payload,provider,"
            "enabled,runs,next_run,created_at,cron) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (s["id"], s["workflow"], s.get("every_seconds") or 0,
             json.dumps(s.get("payload", {})),
             s.get("provider", "mock"), int(s.get("enabled", True)), s.get("runs", 0),
             s["next_run"], time.time(), s.get("cron")))
        conn().commit()
    return s


def bump_schedule(sid: str, next_run: float):
    with _LOCK:
        conn().execute("UPDATE schedules SET next_run=?, runs=runs+1 WHERE id=?",
                       (next_run, sid))
        conn().commit()


def del_schedule(sid: str):
    with _LOCK:
        conn().execute("DELETE FROM schedules WHERE id=?", (sid,))
        conn().commit()


# ------------------------------ api keys ----------------------------------- #
def create_key(label: str, scopes: List[str], key: str = None) -> Dict[str, Any]:
    key = key or "aiflow-" + uuid.uuid4().hex[:24]
    with _LOCK:
        conn().execute("INSERT OR REPLACE INTO apikeys(key,label,scopes,created_at) "
                       "VALUES(?,?,?,?)", (key, label, json.dumps(scopes), time.time()))
        conn().commit()
    return {"key": key, "label": label, "scopes": scopes}


def get_key(key: str):
    r = conn().execute("SELECT * FROM apikeys WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    with _LOCK:
        conn().execute("UPDATE apikeys SET last_used=?, calls=calls+1 WHERE key=?",
                       (time.time(), key))
        conn().commit()
    return {"key": r["key"], "label": r["label"], "scopes": json.loads(r["scopes"])}


def list_keys():
    return [{"key": r["key"][:14] + "…", "label": r["label"],
             "scopes": json.loads(r["scopes"]), "calls": r["calls"]}
            for r in conn().execute("SELECT * FROM apikeys").fetchall()]


def revoke_key(key: str):
    with _LOCK:
        conn().execute("DELETE FROM apikeys WHERE key LIKE ?", (key.rstrip("…") + "%",))
        conn().commit()


# ------------------------------ approvals ---------------------------------- #
def create_approval(run_id, workflow, node, prompt, payload):
    aid = uuid.uuid4().hex[:10]
    with _LOCK:
        conn().execute("INSERT INTO approvals(id,run_id,workflow,node,prompt,payload,"
                       "status,created_at) VALUES(?,?,?,?,?,?, 'pending', ?)",
                       (aid, run_id, workflow, node, prompt,
                        json.dumps(payload, default=str), time.time()))
        conn().commit()
    return aid


def get_approval(aid: str):
    r = conn().execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


def list_approvals(status: str = None):
    q = "SELECT * FROM approvals" + (" WHERE status=?" if status else "")
    q += " ORDER BY created_at DESC LIMIT 50"
    return [dict(r) for r in conn().execute(q, ([status] if status else [])).fetchall()]


def decide_approval(aid: str, approved: bool, by: str = "ui", comment: str = ""):
    with _LOCK:
        conn().execute("UPDATE approvals SET status=?, decided_by=?, comment=?, "
                       "decided_at=? WHERE id=?",
                       ("approved" if approved else "rejected", by, comment, time.time(), aid))
        conn().commit()
    return get_approval(aid)


# ------------------------------ audit log ---------------------------------- #
def _clip(detail: Any, limit: int = 2000) -> Optional[str]:
    """Serialise a detail payload, shrinking it so the result stays valid JSON."""
    if detail is None:
        return None
    try:
        blob = json.dumps(detail, default=str)
    except Exception:
        blob = json.dumps(str(detail))
    if len(blob) <= limit:
        return blob
    # too big: keep a readable marker rather than a truncated, unparseable blob
    if isinstance(detail, dict):
        small = {k: (v if len(json.dumps(v, default=str)) < 200 else "…truncated")
                 for k, v in list(detail.items())[:20]}
        blob = json.dumps(small, default=str)
        if len(blob) <= limit:
            return blob
    return json.dumps({"truncated": True, "preview": blob[:limit - 60]})


def audit(actor: str, action: str, target: str = "", detail: Any = None,
          scopes: List[str] = None, ip: str = "") -> None:
    """Record a state-changing action. Never raises — logging must not break a request."""
    try:
        with _LOCK:
            conn().execute(
                "INSERT INTO audit(ts,actor,scopes,action,target,detail,ip) "
                "VALUES(?,?,?,?,?,?,?)",
                (time.time(), actor or "unknown", json.dumps(scopes or []),
                 action, str(target)[:200], _clip(detail), ip))
            conn().commit()
    except Exception:
        pass


def _safe_json(blob):
    if not blob:
        return None
    try:
        return json.loads(blob)
    except Exception:
        return {"unparseable": str(blob)[:200]}


def list_audit(limit: int = 100, action: str = None, target: str = None,
               actor: str = None, offset: int = 0) -> List[Dict[str, Any]]:
    q = "SELECT * FROM audit WHERE 1=1"
    a: List[Any] = []
    if action:
        q += " AND action=?"; a.append(action)
    if target:
        q += " AND target=?"; a.append(target)
    if actor:
        q += " AND actor=?"; a.append(actor)
    q += " ORDER BY ts DESC LIMIT ? OFFSET ?"
    a += [limit, offset]
    return [{"id": r["id"], "ts": r["ts"], "actor": r["actor"],
             "scopes": json.loads(r["scopes"] or "[]"), "action": r["action"],
             "target": r["target"], "ip": r["ip"],
             "detail": _safe_json(r["detail"]),
             "when": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r["ts"]))}
            for r in conn().execute(q, a).fetchall()]


def audit_summary() -> Dict[str, Any]:
    c = conn()
    total = c.execute("SELECT COUNT(*) n FROM audit").fetchone()["n"]
    by_action = [dict(r) for r in c.execute(
        "SELECT action, COUNT(*) n FROM audit GROUP BY action ORDER BY n DESC LIMIT 12")]
    by_actor = [dict(r) for r in c.execute(
        "SELECT actor, COUNT(*) n FROM audit GROUP BY actor ORDER BY n DESC LIMIT 8")]
    return {"total": total, "by_action": by_action, "by_actor": by_actor}


# ------------------------------ node cache --------------------------------- #
def cache_get(key: str, ttl: float = 0):
    r = conn().execute("SELECT * FROM cache WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    if ttl and (time.time() - (r["created_at"] or 0)) > ttl:
        with _LOCK:
            conn().execute("DELETE FROM cache WHERE key=?", (key,))
            conn().commit()
        return None
    with _LOCK:
        conn().execute("UPDATE cache SET hits=hits+1, last_hit=? WHERE key=?",
                       (time.time(), key))
        conn().commit()
    return {"value": json.loads(r["value"]),
            "usage": json.loads(r["usage"] or "{}"),
            "hits": (r["hits"] or 0) + 1, "created_at": r["created_at"]}


def cache_put(key: str, workflow: str, node: str, value: Any, usage: Dict[str, Any] = None):
    with _LOCK:
        conn().execute(
            "INSERT OR REPLACE INTO cache(key,workflow,node,value,usage,hits,created_at,last_hit) "
            "VALUES(?,?,?,?,?,COALESCE((SELECT hits FROM cache WHERE key=?),0),?,?)",
            (key, workflow, node, json.dumps(value, default=str),
             json.dumps(usage or {}), key, time.time(), time.time()))
        conn().commit()


def cache_stats() -> Dict[str, Any]:
    r = conn().execute("SELECT COUNT(*) n, COALESCE(SUM(hits),0) h FROM cache").fetchone()
    per = [dict(x) for x in conn().execute(
        "SELECT workflow, node, hits, created_at FROM cache "
        "ORDER BY hits DESC LIMIT 20").fetchall()]
    return {"entries": r["n"], "total_hits": r["h"], "top": per}


def cache_clear(workflow: str = None) -> int:
    with _LOCK:
        if workflow:
            cur = conn().execute("DELETE FROM cache WHERE workflow=?", (workflow,))
        else:
            cur = conn().execute("DELETE FROM cache")
        conn().commit()
        return cur.rowcount


# ------------------------------ alerts ------------------------------------- #
METRICS = ("failure_rate", "p95_ms", "avg_ms", "cost_usd", "error_count", "budget_exceeded")


def create_alert(a: Dict[str, Any]) -> Dict[str, Any]:
    aid = a.get("id") or "al-" + uuid.uuid4().hex[:8]
    with _LOCK:
        conn().execute(
            "INSERT OR REPLACE INTO alerts(id,name,workflow,metric,op,threshold,"
            "window_runs,channel,target,enabled,cooldown_s,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, a.get("name", aid), a.get("workflow") or None, a["metric"],
             a.get("op", ">"), float(a["threshold"]), int(a.get("window_runs", 20)),
             a.get("channel", "log"), a.get("target", ""), int(a.get("enabled", True)),
             int(a.get("cooldown_s", 300)), time.time()))
        conn().commit()
    return get_alert(aid)


def get_alert(aid: str):
    r = conn().execute("SELECT * FROM alerts WHERE id=?", (aid,)).fetchone()
    return dict(r) if r else None


def list_alerts():
    return [dict(r) for r in conn().execute(
        "SELECT * FROM alerts ORDER BY created_at DESC").fetchall()]


def del_alert(aid: str):
    with _LOCK:
        conn().execute("DELETE FROM alerts WHERE id=?", (aid,))
        conn().commit()


def alert_metrics(workflow: str = None, window: int = 20) -> Dict[str, float]:
    """Compute the metric window an alert rule is evaluated against."""
    q = "SELECT status, duration_ms, cost_usd FROM runs"
    a: List[Any] = []
    if workflow:
        q += " WHERE workflow=?"
        a.append(workflow)
    q += " ORDER BY created_at DESC LIMIT ?"
    a.append(max(1, window))
    rows = [dict(r) for r in conn().execute(q, a).fetchall()]
    if not rows:
        return {m: 0.0 for m in METRICS} | {"runs": 0}
    n = len(rows)
    bad = [r for r in rows if r["status"] in ("error", "budget_exceeded")]
    durs = sorted(r["duration_ms"] or 0 for r in rows)
    return {
        "runs": n,
        "failure_rate": round(100.0 * len(bad) / n, 2),
        "error_count": float(len(bad)),
        "budget_exceeded": float(sum(1 for r in rows if r["status"] == "budget_exceeded")),
        "p95_ms": float(durs[min(n - 1, int(n * 0.95))]),
        "avg_ms": round(sum(durs) / n, 1),
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in rows), 6),
    }


def record_alert_event(al: Dict[str, Any], value: float, message: str,
                       delivered: bool, detail: str = ""):
    now = time.time()
    with _LOCK:
        conn().execute(
            "INSERT INTO alert_events(alert_id,name,workflow,metric,value,threshold,"
            "message,delivered,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (al["id"], al["name"], al["workflow"], al["metric"], value,
             al["threshold"], message, int(delivered), detail, now))
        conn().execute("UPDATE alerts SET last_fired=?, fires=fires+1 WHERE id=?",
                       (now, al["id"]))
        conn().commit()


def list_alert_events(limit: int = 30):
    return [dict(r) for r in conn().execute(
        "SELECT * FROM alert_events ORDER BY created_at DESC LIMIT ?",
        (limit,)).fetchall()]


# ------------------------------ vector docs -------------------------------- #
def reset_collection(collection: str):
    with _LOCK:
        conn().execute("DELETE FROM docs WHERE collection=?", (collection,))
        conn().commit()


def add_docs(collection: str, rows: List[Dict[str, Any]]):
    with _LOCK:
        conn().executemany(
            "INSERT OR REPLACE INTO docs(id,collection,text,meta,vec,created_at) "
            "VALUES(?,?,?,?,?,?)",
            [(r["id"], collection, r["text"], json.dumps(r.get("meta", {})),
              json.dumps(r["vec"]), time.time()) for r in rows])
        conn().commit()
    return len(rows)


def all_docs(collection: str):
    return [{"id": r["id"], "text": r["text"], "meta": json.loads(r["meta"] or "{}"),
             "vec": json.loads(r["vec"])}
            for r in conn().execute("SELECT * FROM docs WHERE collection=?",
                                    (collection,)).fetchall()]

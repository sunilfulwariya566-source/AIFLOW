from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import queue
import threading
import time
import uuid
from collections import defaultdict, deque
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

import alerts
import bundle
import cron
import providers
import store
import templates
from engine import HANDLERS, Workflow, refs_in, run_workflow, topo_order

BASE = os.path.dirname(os.path.abspath(__file__))
store.init()

WEBHOOK_SECRET = os.environ.get("AIFLOW_WEBHOOK_SECRET", "dev-webhook-secret")
AUTH_ENABLED = os.environ.get("AIFLOW_AUTH", "1") != "0"
RATE_LIMIT = int(os.environ.get("AIFLOW_RATE_LIMIT", "120"))  # req/min per key+ip


def _startup_warnings() -> List[str]:
    """Insecure defaults must be loud, not silent."""
    w = []
    if WEBHOOK_SECRET == "dev-webhook-secret":
        w.append("AIFLOW_WEBHOOK_SECRET is the published default — "
                 "anyone can forge webhook calls. Set it before exposing this.")
    if not os.environ.get("AIFLOW_ADMIN_KEY") and AUTH_ENABLED:
        w.append("Admin key is the published default 'aiflow-dev-key' — "
                 "set AIFLOW_ADMIN_KEY, or rotate it from the Admin tab.")
    if not AUTH_ENABLED:
        w.append("AIFLOW_AUTH=0 — the API is completely open. Local use only.")
    return w


INSECURE_DEFAULTS = _startup_warnings()
if INSECURE_DEFAULTS:
    print("\n" + "=" * 68)
    print(" AIFlow — INSECURE DEFAULTS (fine for local, not for the internet)")
    for _w in INSECURE_DEFAULTS:
        print("   ! " + _w)
    print("=" * 68 + "\n", flush=True)

app = FastAPI(title="AIFlow — AI Automation System", version="2.0")


# ------------------------------ schemas ----------------------------------- #
class RunReq(BaseModel):
    payload: Dict[str, Any] = {}
    provider: str = "mock"
    async_mode: bool = False
    budget: Optional[Dict[str, Any]] = None
    no_cache: bool = False
    cache_all: Optional[Dict[str, Any]] = None
    parallel: int = 0


class WorkflowReq(BaseModel):
    name: str
    description: str = ""
    nodes: List[Dict[str, Any]]
    on_error: str = "stop"
    layout: Dict[str, Any] = {}
    budget: Dict[str, Any] = {}
    parallel: int = 0


class ScheduleReq(BaseModel):
    workflow: str
    every_seconds: int = 0
    cron: Optional[str] = None
    payload: Dict[str, Any] = {}
    provider: str = "mock"


class LayoutReq(BaseModel):
    layout: Dict[str, Any] = {}


class AlertReq(BaseModel):
    name: str
    metric: str
    threshold: float
    op: str = ">"
    workflow: Optional[str] = None
    window_runs: int = 20
    channel: str = "log"
    target: str = ""
    cooldown_s: int = 300
    enabled: bool = True


class RetryReq(BaseModel):
    from_node: Optional[str] = None     # default: the node that failed
    payload: Optional[Dict[str, Any]] = None
    provider: Optional[str] = None
    no_cache: bool = False


class BatchReq(BaseModel):
    payloads: List[Dict[str, Any]]
    provider: str = "mock"
    concurrency: int = 4
    budget: Optional[Dict[str, Any]] = None
    parallel: int = 0
    no_cache: bool = False
    stop_on_error: bool = False


class TemplateReq(BaseModel):
    template: str
    name: Optional[str] = None


class ImportReq(BaseModel):
    bundle: Dict[str, Any]
    mode: str = "rename"
    dry_run: bool = False


class KeyReq(BaseModel):
    label: str
    scopes: List[str] = ["run"]


class DecisionReq(BaseModel):
    approved: bool
    comment: str = ""
    by: str = "ui"


# ------------------------------ auth + limits ------------------------------ #
_hits: Dict[str, deque] = defaultdict(deque)
_rl_lock = threading.Lock()


def rate_limit(ident: str):
    now = time.time()
    with _rl_lock:
        q = _hits[ident]
        while q and now - q[0] > 60:
            q.popleft()
        if len(q) >= RATE_LIMIT:
            raise HTTPException(429, f"rate limit exceeded ({RATE_LIMIT}/min)")
        q.append(now)


PUBLIC_PATHS = ("/", "/api/health", "/static")


def auth(request: Request, x_api_key: Optional[str] = Header(None)) -> Dict[str, Any]:
    ip = request.client.host if request.client else "?"
    rate_limit(f"{x_api_key or ip}")
    if not AUTH_ENABLED:
        return {"label": "anonymous", "scopes": ["admin"]}
    # the bundled UI is served same-origin and passes the dev key automatically
    if not x_api_key:
        raise HTTPException(401, "missing X-API-Key header")
    rec = store.get_key(x_api_key)
    if not rec:
        raise HTTPException(403, "invalid API key")
    return rec


def need(scope: str, who: Dict[str, Any]):
    if "admin" not in who["scopes"] and scope not in who["scopes"]:
        raise HTTPException(403, f"scope '{scope}' required")


def audit(request: Request, who: Dict[str, Any], action: str,
          target: str = "", detail: Any = None) -> None:
    """Record a state change against the calling key."""
    store.audit(actor=(who or {}).get("label", "unknown"), action=action,
                target=target, detail=detail, scopes=(who or {}).get("scopes", []),
                ip=(request.client.host if request and request.client else ""))


# ------------------------------ event bus (SSE) ---------------------------- #
_subs: List[queue.Queue] = []
_subs_lock = threading.Lock()


def publish(evt: Dict[str, Any]):
    dead = []
    with _subs_lock:
        for q in _subs:
            try:
                q.put_nowait(evt)
            except Exception:
                dead.append(q)
        for q in dead:
            _subs.remove(q)


def slim(evt: Dict[str, Any]) -> Dict[str, Any]:
    e = dict(evt)
    o = e.get("output")
    if o is not None and not isinstance(o, (int, float, bool)):
        s = o if isinstance(o, str) else json.dumps(o, default=str)
        e["output"] = s[:1500]
    return e


# ------------------------------ job queue ---------------------------------- #
JOBS: Dict[str, Dict[str, Any]] = {}
_jobq: "queue.Queue[tuple]" = queue.Queue()


def execute(wf_doc, payload, provider, trigger, resume=None, job_id=None, budget=None,
            no_cache=False, cache_all=None, parallel=0):
    wf = Workflow.from_dict(wf_doc)

    def cb(evt):
        if evt.get("event") == "token":
            publish({**evt, "workflow": wf.name, "job_id": job_id})
        else:
            publish(slim({**evt, "workflow": wf.name, "job_id": job_id}))

    res = run_workflow(wf, payload, provider, on_event=cb, resume=resume, budget=budget,
                       no_cache=no_cache, cache_all=cache_all,
                       parallel=parallel or int(wf_doc.get("parallel") or 0))
    res["trigger"] = trigger
    store.add_run(res)
    try:
        res["alerts_fired"] = alerts.evaluate(wf.name)
    except Exception:
        res["alerts_fired"] = []
    if job_id:
        JOBS[job_id] = {"job_id": job_id, "state": "done", "run_id": res["run_id"],
                        "status": res["status"]}
    return res


def _worker():
    while True:
        job_id, wf_doc, payload, provider, trigger, resume, budget = _jobq.get()
        try:
            JOBS[job_id] = {"job_id": job_id, "state": "running"}
            execute(wf_doc, payload, provider, trigger, resume, job_id, budget)
        except Exception as e:  # noqa: BLE001
            JOBS[job_id] = {"job_id": job_id, "state": "failed", "error": str(e)}
        finally:
            _jobq.task_done()


for _ in range(3):
    threading.Thread(target=_worker, daemon=True).start()


# ------------------------------ scheduler ---------------------------------- #
def _scheduler_loop():
    while True:
        try:
            now = time.time()
            for s in store.list_schedules():
                if s["enabled"] and now >= (s["next_run"] or 0):
                    wf = store.get_workflow(s["workflow"])
                    if s.get("cron"):
                        try:
                            nxt = cron.next_run(s["cron"], now)
                        except cron.CronError:
                            nxt = now + 3600      # bad expression: back off, keep the row
                    else:
                        nxt = now + max(5, s["every_seconds"] or 60)
                    store.bump_schedule(s["id"], nxt)
                    if wf:
                        _jobq.put((f"sched-{uuid.uuid4().hex[:8]}", wf, s["payload"],
                                   s["provider"], f"schedule:{s['id']}", None, None))
        except Exception:
            pass
        time.sleep(1)


threading.Thread(target=_scheduler_loop, daemon=True).start()
alerts.subscribe(publish)


# ------------------------------ routes ------------------------------------- #
@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"))


@app.get("/api/health")
def health():
    import tools as _t
    return {"ok": True, "version": "2.0", "auth": AUTH_ENABLED,
            "search_backend": _t.search_backend(),
            "insecure_defaults": INSECURE_DEFAULTS,
            "workflows": len(store.list_workflows()),
            "node_types": sorted(HANDLERS), "providers": providers.available(),
            "rate_limit_per_min": RATE_LIMIT}


@app.get("/api/workflows")
def api_workflows(who=Depends(auth)):
    return store.list_workflows()


@app.get("/api/workflows/{name}")
def api_workflow(name: str, who=Depends(auth)):
    wf = store.get_workflow(name)
    if not wf:
        raise HTTPException(404, "workflow not found")
    return wf


@app.put("/api/workflows/{name}")
def api_save(name: str, body: WorkflowReq, request: Request, who=Depends(auth)):
    need("write", who)
    d = body.model_dump()
    d["name"] = name
    saved = store.save_workflow(d)
    audit(request, who, "workflow.save", name,
          {"version": saved.get("version"), "nodes": len(d.get("nodes") or [])})
    return saved


@app.delete("/api/workflows/{name}")
def api_delete(name: str, request: Request, who=Depends(auth)):
    need("write", who)
    store.delete_workflow(name)
    audit(request, who, "workflow.delete", name)
    return {"deleted": name}


@app.put("/api/workflows/{name}/layout")
def api_layout(name: str, body: LayoutReq, who=Depends(auth)):
    """Save canvas node positions. Does not create a new version."""
    need("write", who)
    if not store.get_workflow(name):
        raise HTTPException(404, "workflow not found")
    return store.save_layout(name, body.layout)


@app.get("/api/workflows/{name}/versions")
def api_versions(name: str, who=Depends(auth)):
    return store.list_versions(name)


@app.post("/api/workflows/{name}/rollback/{version}")
def api_rollback(name: str, version: int, request: Request, who=Depends(auth)):
    need("write", who)
    wf = store.rollback(name, version)
    if not wf:
        raise HTTPException(404, "version not found")
    audit(request, who, "workflow.rollback", name,
          {"to_version": version, "new_version": wf.get("version")})
    return wf


@app.post("/api/workflows/{name}/run")
def api_run(name: str, body: RunReq, who=Depends(auth)):
    need("run", who)
    wf = store.get_workflow(name)
    if not wf:
        raise HTTPException(404, "workflow not found")
    if body.async_mode:
        job_id = uuid.uuid4().hex[:10]
        JOBS[job_id] = {"job_id": job_id, "state": "queued"}
        _jobq.put((job_id, wf, body.payload, body.provider, "manual-async", None,
                   body.budget))
        return {"job_id": job_id, "state": "queued"}
    return JSONResponse(execute(wf, body.payload, body.provider, "manual",
                                budget=body.budget, no_cache=body.no_cache,
                                cache_all=body.cache_all, parallel=body.parallel))


@app.post("/api/workflows/{name}/batch")
def api_batch(name: str, body: BatchReq, who=Depends(auth)):
    """Run one workflow over many payloads, concurrently, and summarise the results."""
    need("run", who)
    wf = store.get_workflow(name)
    if not wf:
        raise HTTPException(404, "workflow not found")
    if not body.payloads:
        raise HTTPException(400, "payloads must be a non-empty list")
    if len(body.payloads) > 200:
        raise HTTPException(400, "batch is limited to 200 payloads")

    width = max(1, min(int(body.concurrency or 1), 8))
    started = time.time()
    stop = threading.Event()

    def one(idx_payload):
        idx, payload = idx_payload
        if stop.is_set():
            return {"index": idx, "status": "cancelled", "run_id": None}
        try:
            res = execute(wf, payload, body.provider, f"batch:{name}",
                          budget=body.budget, no_cache=body.no_cache,
                          parallel=body.parallel)
        except Exception as e:  # noqa: BLE001
            return {"index": idx, "status": "error", "error": str(e), "run_id": None}
        if body.stop_on_error and res["status"] in ("error", "budget_exceeded"):
            stop.set()
        u = res.get("usage") or {}
        return {"index": idx, "run_id": res["run_id"], "status": res["status"],
                "duration_ms": res.get("duration_ms"), "outputs": res.get("outputs"),
                "error": res.get("error"), "cost_usd": u.get("cost_usd", 0.0),
                "tokens": u.get("tokens_in", 0) + u.get("tokens_out", 0)}

    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=width) as ex:
        results = list(ex.map(one, list(enumerate(body.payloads))))
    results.sort(key=lambda r: r["index"])

    by_status: Dict[str, int] = {}
    for r in results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1
    return {
        "workflow": name, "total": len(results),
        "succeeded": by_status.get("success", 0),
        "by_status": by_status,
        "cost_usd": round(sum(r.get("cost_usd") or 0 for r in results), 6),
        "tokens": sum(r.get("tokens") or 0 for r in results),
        "duration_ms": int((time.time() - started) * 1000),
        "concurrency": width, "results": results,
    }


@app.get("/api/workflows/{name}/versions/diff")
def api_version_diff(name: str, a: int, b: int, who=Depends(auth)):
    """Structural diff between two saved versions."""
    try:
        return bundle.diff_versions(name, a, b)
    except bundle.BundleError as e:
        raise HTTPException(404, str(e))


@app.get("/api/jobs/{job_id}")
def api_job(job_id: str, who=Depends(auth)):
    j = JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "job not found")
    if j.get("run_id"):
        j = {**j, "run": store.get_run(j["run_id"])}
    return j


@app.post("/api/validate")
def api_validate(body: WorkflowReq, who=Depends(auth)):
    d = body.model_dump()
    errors, warnings = [], []
    ids = [n.get("id") for n in d["nodes"]]
    if len(ids) != len(set(ids)):
        errors.append("duplicate node ids")
    for n in d["nodes"]:
        if not n.get("id"):
            errors.append("a node is missing an id")
        if n.get("type") not in HANDLERS:
            errors.append(f"node '{n.get('id')}': unknown type '{n.get('type')}'")
        if n.get("on_error") not in (None, "stop", "continue", "fallback", "dead_letter"):
            errors.append(f"node '{n.get('id')}': bad on_error '{n.get('on_error')}'")
        if n.get("type") == "workflow":
            sub = (n.get("params") or {}).get("workflow")
            if not sub:
                errors.append(f"node '{n.get('id')}': workflow node needs params.workflow")
            elif sub == d["name"]:
                errors.append(f"node '{n.get('id')}': workflow cannot call itself")
            elif not store.get_workflow(sub):
                errors.append(f"node '{n.get('id')}': sub-workflow '{sub}' not found")
        if n.get("when") is not None:
            if not isinstance(n["when"], str) or not n["when"].strip():
                errors.append(f"node '{n.get('id')}': when must be a non-empty expression")
            else:
                try:
                    import ast as _ast
                    _ast.parse(n["when"], mode="eval")
                except SyntaxError as e:
                    errors.append(f"node '{n.get('id')}': bad when expression ({e.msg})")
            for r in set(refs_in(n.get("when", ""))):
                if r not in ids and r != "payload":
                    warnings.append(f"node '{n.get('id')}' when references unknown '{r}'")
        for r in set(refs_in(n.get("params", {}))):
            if r not in ids and r != "payload":
                warnings.append(f"node '{n.get('id')}' references unknown '{r}'")
    for n in d["nodes"]:
        t = n.get("timeout")
        if t is not None and (not isinstance(t, (int, float)) or t < 0):
            errors.append(f"node '{n.get('id')}': timeout must be a positive number")
    if d.get("parallel") and not (1 <= int(d["parallel"]) <= 16):
        errors.append("parallel must be between 1 and 16")
    for k in (d.get("budget") or {}):
        if k not in ("max_cost_usd", "max_tokens", "max_llm_calls"):
            warnings.append(f"unknown budget key '{k}'")
    if not errors:
        try:
            order = topo_order(Workflow.from_dict(d).nodes)
            return {"valid": True, "errors": [], "warnings": warnings,
                    "execution_order": [n.id for n in order]}
        except Exception as e:  # noqa: BLE001
            errors.append(str(e))
    return {"valid": False, "errors": errors, "warnings": warnings, "execution_order": []}


# ------------------------------ templates ---------------------------------- #
@app.get("/api/templates")
def api_templates(who=Depends(auth)):
    return templates.listing()


@app.post("/api/templates")
def api_use_template(body: TemplateReq, request: Request, who=Depends(auth)):
    """Create a new workflow from a starter template."""
    need("write", who)
    try:
        made = templates.instantiate(body.template, body.name)
    except KeyError as e:
        raise HTTPException(404, str(e))
    audit(request, who, "template.use", made["workflow"]["name"],
          {"template": body.template})
    return made


# ------------------------------ import / export ---------------------------- #
@app.get("/api/export")
def api_export(names: str = None, include_deps: bool = True, who=Depends(auth)):
    """Export workflows as a portable bundle. `names` is comma separated; all if omitted."""
    want = [n.strip() for n in (names or "").split(",") if n.strip()]
    doc = bundle.export_bundle(want or None, include_deps)
    fname = (want[0] if len(want) == 1 else "aiflow-workflows") + ".bundle.json"
    return JSONResponse(doc, headers={
        "Content-Disposition": f'attachment; filename="{fname}"'})


@app.post("/api/import")
def api_import(body: ImportReq, request: Request, who=Depends(auth)):
    need("write", who)
    try:
        res = bundle.import_bundle(body.bundle, body.mode, body.dry_run)
    except bundle.BundleError as e:
        raise HTTPException(400, str(e))
    if not body.dry_run:
        audit(request, who, "bundle.import", str(res.get("imported", 0)),
              {"mode": body.mode, "results": res.get("results")})
    return res


@app.post("/api/runs/{run_id}/retry")
def api_retry(run_id: str, body: RetryReq, who=Depends(auth)):
    """Re-run a failed run from the failing node, reusing everything before it."""
    need("run", who)
    prev = store.get_run(run_id)
    if not prev:
        raise HTTPException(404, "run not found")
    wf_doc = store.get_workflow(prev["workflow"])
    if not wf_doc:
        raise HTTPException(404, f"workflow '{prev['workflow']}' no longer exists")

    logs = prev.get("logs") or []
    bad = {"error", "budget_exceeded", "dead_letter"}
    target = body.from_node
    if target is None:
        target = next((l["node"] for l in logs if l.get("status") in bad), None)
    if target is None:
        raise HTTPException(400, "nothing to retry — no failed node in that run")

    node_ids = [n.get("id") for n in wf_doc["nodes"]]
    if target not in node_ids:
        raise HTTPException(400, f"node '{target}' is not in workflow '{prev['workflow']}'")

    # keep only the work that happened strictly before the target node
    order = [l["node"] for l in logs]
    cut = order.index(target) if target in order else len(order)
    keep_logs = [l for l in logs[:cut] if l.get("status") in
                 ("success", "skipped", "cached", "recovered")]
    keep_ids = {l["node"] for l in keep_logs}
    ctx = {k: v for k, v in (prev.get("context") or {}).items() if k in keep_ids}

    resume = {"context": ctx, "logs": keep_logs,
              "skipped": [n for n in (prev.get("skipped") or []) if n in keep_ids],
              "dead_letter": []}
    res = execute(wf_doc,
                  body.payload if body.payload is not None else prev.get("payload", {}),
                  body.provider or prev.get("provider", "mock"),
                  f"retry:{run_id}", resume=resume)
    res["retried_from"] = target
    res["reused_nodes"] = sorted(keep_ids)
    return JSONResponse(res)


@app.get("/api/runs/compare")
def api_compare(a: str, b: str, who=Depends(auth)):
    """Side-by-side diff of two runs."""
    try:
        return bundle.compare_runs(a, b)
    except bundle.BundleError as e:
        raise HTTPException(404, str(e))


# ------------------------------ webhooks ----------------------------------- #
@app.post("/hooks/{name}")
async def api_webhook(name: str, request: Request,
                      x_signature: Optional[str] = Header(None)):
    rate_limit(request.client.host if request.client else "hook")
    raw = await request.body()
    if AUTH_ENABLED:
        if not x_signature:
            raise HTTPException(401, "missing X-Signature (HMAC-SHA256 of the body)")
        expected = hmac.new(WEBHOOK_SECRET.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, x_signature.replace("sha256=", "")):
            raise HTTPException(403, "bad signature")
    wf = store.get_workflow(name)
    if not wf:
        raise HTTPException(404, "workflow not found")
    payload = json.loads(raw or b"{}")
    job_id = uuid.uuid4().hex[:10]
    JOBS[job_id] = {"job_id": job_id, "state": "queued"}
    _jobq.put((job_id, wf, payload, "mock", "webhook", None, None))
    return {"accepted": True, "job_id": job_id}


@app.get("/api/webhook-signature")
def api_sig(name: str, payload: str = "{}", who=Depends(auth)):
    """Helper so the UI can show a ready-to-paste curl command."""
    sig = hmac.new(WEBHOOK_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return {"signature": sig, "header": f"X-Signature: {sig}"}


# ------------------------------ approvals ---------------------------------- #
@app.get("/api/approvals")
def api_approvals(status: str = "pending", who=Depends(auth)):
    return store.list_approvals(status or None)


@app.post("/api/approvals/{aid}")
def api_decide(aid: str, body: DecisionReq, request: Request, who=Depends(auth)):
    need("approve", who)
    rec = store.get_approval(aid)
    if not rec:
        raise HTTPException(404, "approval not found")
    store.decide_approval(aid, body.approved, body.by, body.comment)
    audit(request, who, "approval.decide", aid,
          {"approved": body.approved, "workflow": rec["workflow"],
           "node": rec["node"], "comment": body.comment})
    prev = store.get_run(rec["run_id"])
    if not prev:
        return {"decided": True, "resumed": False}
    wf = store.get_workflow(rec["workflow"])
    resume = {"run_id": prev["run_id"], "context": prev["context"], "logs":
              [l for l in prev["logs"] if l["status"] != "paused"],
              "approvals": {rec["node"]: aid},
              "duration_ms": prev.get("duration_ms", 0),
              "dead_letter": prev.get("dead_letter", [])}
    res = execute(wf, prev.get("payload", {}), prev.get("provider", "mock"),
                  "resume", resume=resume)
    return {"decided": True, "resumed": True, "run": res}


# ------------------------------ runs / stats ------------------------------- #
@app.get("/api/runs")
def api_runs(limit: int = 30, offset: int = 0, workflow: str = None,
             status: str = None, who=Depends(auth)):
    return store.list_runs(limit, workflow, status, offset)


@app.get("/api/runs/{run_id}")
def api_run_get(run_id: str, who=Depends(auth)):
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404, "run not found")
    return r


@app.get("/api/stats")
def api_stats(who=Depends(auth)):
    return store.stats()


@app.get("/api/events")
async def api_events(request: Request):
    """SSE stream of live execution events."""
    q: queue.Queue = queue.Queue(maxsize=500)
    with _subs_lock:
        _subs.append(q)

    async def gen():
        try:
            yield "retry: 2000\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = q.get_nowait()
                    yield f"data: {json.dumps(evt, default=str)}\n\n"
                except queue.Empty:
                    await asyncio.sleep(0.25)
                    yield ": ping\n\n"
        finally:
            with _subs_lock:
                if q in _subs:
                    _subs.remove(q)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


# ------------------------------ schedules ---------------------------------- #
@app.get("/api/schedules")
def api_schedules(who=Depends(auth)):
    now = time.time()
    return [{**s, "in_seconds": max(0, round((s["next_run"] or now) - now))}
            for s in store.list_schedules()]


@app.post("/api/schedules")
def api_add_schedule(body: ScheduleReq, request: Request, who=Depends(auth)):
    need("write", who)
    if not store.get_workflow(body.workflow):
        raise HTTPException(404, "workflow not found")

    if body.cron:
        try:
            first = cron.next_run(body.cron)
            human = cron.describe(body.cron)
        except cron.CronError as e:
            raise HTTPException(400, f"bad cron expression: {e}")
        s = {"id": f"{body.workflow}-{uuid.uuid4().hex[:5]}", "workflow": body.workflow,
             "every_seconds": 0, "cron": body.cron, "payload": body.payload,
             "provider": body.provider, "enabled": True, "runs": 0, "next_run": first}
        out = store.add_schedule(s)
        out["describes"] = human
    else:
        every = max(5, body.every_seconds or 60)
        s = {"id": f"{body.workflow}-{uuid.uuid4().hex[:5]}", "workflow": body.workflow,
             "every_seconds": every, "cron": None, "payload": body.payload,
             "provider": body.provider, "enabled": True, "runs": 0,
             "next_run": time.time() + every}
        out = store.add_schedule(s)

    audit(request, who, "schedule.create", out["id"],
          {"workflow": body.workflow, "cron": body.cron,
           "every_seconds": body.every_seconds})
    return out


@app.get("/api/cron/check")
def api_cron_check(expr: str, who=Depends(auth)):
    """Validate a cron expression and preview when it would fire."""
    try:
        return {"valid": True, "expression": expr, "describes": cron.describe(expr),
                "next": cron.preview(expr, 5)}
    except cron.CronError as e:
        return {"valid": False, "expression": expr, "error": str(e)}


@app.delete("/api/schedules/{sid}")
def api_del_schedule(sid: str, request: Request, who=Depends(auth)):
    need("write", who)
    store.del_schedule(sid)
    audit(request, who, "schedule.delete", sid)
    return {"deleted": sid}


# ------------------------------ cache -------------------------------------- #
@app.get("/api/cache")
def api_cache(who=Depends(auth)):
    return store.cache_stats()


@app.delete("/api/cache")
def api_cache_clear(request: Request, workflow: str = None, who=Depends(auth)):
    need("write", who)
    n = store.cache_clear(workflow)
    audit(request, who, "cache.clear", workflow or "*", {"cleared": n})
    return {"cleared": n}


# ------------------------------ alerts ------------------------------------- #
@app.get("/api/alerts")
def api_alerts(who=Depends(auth)):
    return store.list_alerts()


@app.post("/api/alerts")
def api_add_alert(body: AlertReq, request: Request, who=Depends(auth)):
    need("write", who)
    if body.metric not in store.METRICS:
        raise HTTPException(400, f"metric must be one of {list(store.METRICS)}")
    if body.op not in alerts.OPS:
        raise HTTPException(400, f"op must be one of {list(alerts.OPS)}")
    if body.workflow and not store.get_workflow(body.workflow):
        raise HTTPException(404, "workflow not found")
    if body.channel == "webhook" and not body.target:
        raise HTTPException(400, "webhook channel needs a target URL")
    created = store.create_alert(body.model_dump())
    audit(request, who, "alert.create", created["id"],
          {"metric": body.metric, "op": body.op, "threshold": body.threshold})
    return created


@app.delete("/api/alerts/{aid}")
def api_del_alert(aid: str, request: Request, who=Depends(auth)):
    need("write", who)
    store.del_alert(aid)
    audit(request, who, "alert.delete", aid)
    return {"deleted": aid}


@app.post("/api/alerts/evaluate")
def api_eval_alerts(who=Depends(auth)):
    """Force an evaluation pass (normally runs automatically after each run)."""
    return {"fired": alerts.evaluate()}


@app.get("/api/alerts/events")
def api_alert_events(limit: int = 30, who=Depends(auth)):
    return store.list_alert_events(limit)


@app.get("/api/alerts/metrics")
def api_alert_metrics(workflow: str = None, window: int = 20, who=Depends(auth)):
    return store.alert_metrics(workflow, window)


# ------------------------------ audit log ---------------------------------- #
@app.get("/api/audit")
def api_audit(limit: int = 100, offset: int = 0, action: str = None,
              target: str = None, actor: str = None, who=Depends(auth)):
    need("admin", who)
    return store.list_audit(limit, action, target, actor, offset)


@app.get("/api/audit/summary")
def api_audit_summary(who=Depends(auth)):
    need("admin", who)
    return store.audit_summary()


# ------------------------------ api keys ----------------------------------- #
@app.get("/api/keys")
def api_keys(who=Depends(auth)):
    need("admin", who)
    return store.list_keys()


@app.post("/api/keys")
def api_new_key(body: KeyReq, request: Request, who=Depends(auth)):
    need("admin", who)
    key = store.create_key(body.label, body.scopes)
    audit(request, who, "key.create", body.label, {"scopes": body.scopes})
    return key


@app.delete("/api/keys/{key}")
def api_revoke(key: str, request: Request, who=Depends(auth)):
    need("admin", who)
    store.revoke_key(key)
    audit(request, who, "key.revoke", key[:14])
    return {"revoked": key}

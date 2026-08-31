"""AI automation / workflow engine.

A workflow is a DAG of nodes executed in dependency order. Features:
  - {{templating}} with implicit dependency inference
  - AST-sandboxed expressions (see sandbox.py)
  - per-node retries with exponential backoff + jitter
  - error policies: stop | continue | fallback | dead_letter
  - token/cost accounting per node and per run
  - human-in-the-loop approval gates (runs pause and resume)
  - RAG nodes (chunk / embed / retrieve) and an autonomous agent loop
  - streaming callbacks so the UI can render a live trace
"""
from __future__ import annotations

import hashlib
import json
import random
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

import tools
from providers import get_provider
from sandbox import SandboxError, safe_eval

REF = re.compile(r"\{\{\s*([a-zA-Z0-9_.\[\]-]+)\s*\}\}")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


class WorkflowError(Exception):
    pass


class NodeTimeout(Exception):
    """A node exceeded its `timeout` budget."""


class BudgetExceeded(Exception):
    """Raised when a run would exceed its cost / token / call ceiling."""


class PausedForApproval(Exception):
    def __init__(self, approval_id: str, node: str):
        self.approval_id = approval_id
        self.node = node
        super().__init__(f"paused for approval {approval_id} at node {node}")


# --------------------------------------------------------------------------- #
# templating
# --------------------------------------------------------------------------- #
def _lookup(path: str, ctx: Dict[str, Any]) -> Any:
    cur: Any = ctx
    for part in path.replace("[", ".").replace("]", "").split("."):
        if part == "":
            continue
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            i = int(part)
            cur = cur[i] if -len(cur) <= i < len(cur) else None
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    return cur


# keys whose values are rendered later, in an inner scope (loop vars etc.)
LAZY_KEYS = {"step"}


def render(value: Any, ctx: Dict[str, Any], lazy: bool = True) -> Any:
    if isinstance(value, str):
        whole = REF.fullmatch(value.strip())
        if whole:
            return _lookup(whole.group(1), ctx)

        def sub(m: re.Match) -> str:
            v = _lookup(m.group(1), ctx)
            return "" if v is None else (v if isinstance(v, str)
                                         else json.dumps(v, default=str))
        return REF.sub(sub, value)
    if isinstance(value, dict):
        return {k: (v if (lazy and k in LAZY_KEYS) else render(v, ctx, lazy))
                for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx, lazy) for v in value]
    return value


def refs_in(value: Any) -> List[str]:
    out: List[str] = []
    if isinstance(value, str):
        out += [m.split(".")[0].split("[")[0] for m in REF.findall(value)]
    elif isinstance(value, dict):
        for v in value.values():
            out += refs_in(v)   # includes lazy subtrees: deps must still resolve
    elif isinstance(value, list):
        for v in value:
            out += refs_in(v)
    return out


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
@dataclass
class Node:
    id: str
    type: str
    params: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    retries: int = 0
    on_error: Optional[str] = None      # stop | continue | fallback | dead_letter
    fallback: Any = None
    timeout: float = 0.0
    when: Optional[str] = None          # conditional edge: skip node if falsy
    cache: Any = None                   # True or {"ttl": seconds} — reuse prior output

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Node":
        return Node(id=d["id"], type=d["type"], params=d.get("params", {}) or {},
                    depends_on=list(d.get("depends_on", []) or []),
                    retries=int(d.get("retries", 0)), on_error=d.get("on_error"),
                    fallback=d.get("fallback"), timeout=float(d.get("timeout", 0)),
                    when=d.get("when"), cache=d.get("cache"))


@dataclass
class Workflow:
    name: str
    nodes: List[Node]
    description: str = ""
    on_error: str = "stop"
    budget: Dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def from_dict(d: Dict[str, Any]) -> "Workflow":
        return Workflow(name=d.get("name", "untitled"),
                        description=d.get("description", ""),
                        on_error=d.get("on_error", "stop"),
                        budget=d.get("budget") or {},
                        nodes=[Node.from_dict(n) for n in d.get("nodes", [])])


# --------------------------------------------------------------------------- #
# node handlers
# --------------------------------------------------------------------------- #
CACHEABLE = {"llm", "http", "agent", "embed", "retrieve", "chunk", "python",
             "template", "map", "filter", "validate", "workflow"}


def cache_key(wf_name: str, node: "Node", params: Dict[str, Any], provider: str) -> str:
    """Stable hash of everything that can change a node's output."""
    payload = json.dumps({"w": wf_name, "n": node.id, "t": node.type,
                          "p": params, "prov": provider},
                         sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def call_with_timeout(fn, seconds: float):
    """Run `fn()` with a wall-clock limit.

    The worker thread is a daemon: on timeout we stop waiting and let it die with
    the process. Python cannot safely kill a thread mid-call, so an abandoned
    handler may still finish in the background — its result is simply discarded.
    """
    if not seconds or seconds <= 0:
        return fn()
    import concurrent.futures as _cf
    ex = _cf.ThreadPoolExecutor(max_workers=1)
    fut = ex.submit(fn)
    try:
        return fut.result(timeout=seconds)
    except _cf.TimeoutError:
        raise NodeTimeout(f"node exceeded its {seconds}s timeout")
    finally:
        ex.shutdown(wait=False)


def spend(run: Dict[str, Any], usage: Dict[str, Any]) -> None:
    """Record usage and enforce the run budget. Called after every provider call."""
    run["_usage"].append(usage)
    b = run.get("budget") or {}
    if not b:
        return
    tot_cost = sum(x.get("cost_usd", 0.0) for x in run["_usage"])
    tot_tok = sum(x.get("tokens_in", 0) + x.get("tokens_out", 0) for x in run["_usage"])
    calls = len(run["_usage"])
    if b.get("max_cost_usd") is not None and tot_cost > float(b["max_cost_usd"]):
        raise BudgetExceeded(
            f"cost ${tot_cost:.6f} exceeds budget ${float(b['max_cost_usd']):.6f}")
    if b.get("max_tokens") is not None and tot_tok > int(b["max_tokens"]):
        raise BudgetExceeded(f"{tot_tok} tokens exceeds budget {int(b['max_tokens'])}")
    if b.get("max_llm_calls") is not None and calls > int(b["max_llm_calls"]):
        raise BudgetExceeded(f"{calls} LLM calls exceeds budget {int(b['max_llm_calls'])}")


def _scope(ctx, run, extra=None):
    s = {"payload": run["payload"], **{k: v for k, v in ctx.items() if str(k).isidentifier()}}
    s.update(extra or {})
    return s


def h_input(p, ctx, run, node):
    key = p.get("key")
    val = run["payload"].get(key, p.get("default"))
    if val is None and p.get("required"):
        raise WorkflowError(f"missing required input '{key}'")
    return val


def h_template(p, ctx, run, node):
    return p.get("text", "")


def h_python(p, ctx, run, node):
    return safe_eval(p.get("expr", "None"), _scope(ctx, run))


def h_llm(p, ctx, run, node):
    provider = get_provider(run["provider"])
    kw = dict(prompt=p.get("prompt", ""), system=p.get("system", ""),
              model=p.get("model", "auto"),
              temperature=float(p.get("temperature", 0.2)),
              json_mode=bool(p.get("json_mode", False)))

    emit = run.get("_emit")
    # stream unless the node opts out; only worth it when someone is listening
    want = p.get("stream", True) and emit is not None and run.get("stream", True)
    if want and getattr(provider, "supports_streaming", False):
        nid = getattr(node, "id", "llm")
        idx = [0]

        def on_token(tok):
            idx[0] += 1
            emit({"event": "token", "node": nid, "seq": idx[0], "text": tok})

        out, usage = provider.stream(on_token=on_token, **kw)
    else:
        out, usage = provider.complete_with_usage(**kw)
    spend(run, usage)
    return out


def h_http(p, ctx, run, node):
    if run["provider"] == "mock" or p.get("mock"):
        return {"status": 200, "mock": True, "url": p.get("url"),
                "body": {"ok": True, "note": "stubbed HTTP response (mock mode)"}}
    import urllib.request
    req = urllib.request.Request(
        p["url"], method=p.get("method", "GET").upper(),
        data=json.dumps(p.get("body")).encode() if p.get("body") else None,
        headers={"Content-Type": "application/json", **(p.get("headers") or {})})
    with urllib.request.urlopen(req, timeout=p.get("timeout", 20)) as r:  # noqa: S310
        raw = r.read().decode()
    try:
        return {"status": 200, "body": json.loads(raw)}
    except Exception:
        return {"status": 200, "body": raw}


def h_branch(p, ctx, run, node):
    cond = safe_eval(p.get("condition", "False"), _scope(ctx, run))
    return p.get("if_true") if cond else p.get("if_false")


def h_output(p, ctx, run, node):
    return p.get("value")


def h_map(p, ctx, run, node):
    items = p.get("over") or []
    if isinstance(items, str):
        try:
            items = json.loads(items)
        except Exception:
            items = [s for s in items.splitlines() if s.strip()]
    if not isinstance(items, list):
        items = [items]
    items = items[:int(p.get("limit", 50))]

    step = p.get("step") or {"type": "llm", "params": {"prompt": "{{item}}"}}
    var = p.get("as", "item")
    handler = HANDLERS.get(step.get("type"))
    if handler is None:
        raise WorkflowError(f"map: unknown inner step type '{step.get('type')}'")
    inner = Node(id=f"{node.id}[]", type=step["type"], params=step.get("params", {}))

    def one(pair):
        idx, item = pair
        scoped_ctx = {**ctx, var: item, "index": idx}
        params = render(step.get("params", {}), {**scoped_ctx, "payload": run["payload"]})
        return handler(params, scoped_ctx, run, inner)

    workers = max(1, min(int(p.get("workers", 4)), 8))
    if workers == 1 or len(items) <= 1:
        return [one((i, it)) for i, it in enumerate(items)]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=workers) as ex:
        return list(ex.map(one, list(enumerate(items))))


def h_filter(p, ctx, run, node):
    items = p.get("over") or []
    var = p.get("as", "item")
    cond = p.get("condition", "True")
    return [it for i, it in enumerate(items)
            if safe_eval(cond, _scope(ctx, run, {var: it, "index": i}))]


def h_validate(p, ctx, run, node):
    value = p.get("value")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except Exception:
            pass
    schema = p.get("schema") or {}
    errors: List[str] = []
    required = schema.get("required") or []
    if required and not isinstance(value, dict):
        errors.append("value is not an object")
    else:
        for k in required:
            if not isinstance(value, dict) or value.get(k) in (None, ""):
                errors.append(f"missing required field '{k}'")
    types = {"string": str, "number": (int, float), "integer": int,
             "boolean": bool, "array": list, "object": dict}
    for k, spec in (schema.get("properties") or {}).items():
        if not isinstance(value, dict) or k not in value:
            continue
        v, want = value[k], spec.get("type")
        if want in types and not isinstance(v, types[want]):
            errors.append(f"field '{k}' should be {want}, got {type(v).__name__}")
        if spec.get("enum") and v not in spec["enum"]:
            errors.append(f"field '{k}'={v!r} not in {spec['enum']}")
    if errors and not p.get("soft"):
        raise WorkflowError("validation failed: " + "; ".join(errors))
    return {"valid": not errors, "errors": errors, "value": value}


# ------------------------------ RAG nodes ---------------------------------- #
def h_chunk(p, ctx, run, node):
    return tools.chunk_text(p.get("text", ""), int(p.get("size", 240)),
                            int(p.get("overlap", 40)))


def h_embed(p, ctx, run, node):
    texts = p.get("texts") or []
    if isinstance(texts, str):
        texts = [texts]
    n = tools.index_texts(p.get("collection", "default"), texts,
                          reset=bool(p.get("reset")), meta=p.get("meta"))
    return {"count": n, "collection": p.get("collection", "default")}


def h_retrieve(p, ctx, run, node):
    hits = tools.search(p.get("collection", "default"), p.get("query", ""),
                        int(p.get("k", 3)))
    return {"matches": hits, "count": len(hits),
            "context": "\n---\n".join(h["text"] for h in hits)}


# ------------------------------ approval ----------------------------------- #
def h_approval(p, ctx, run, node):
    import store
    resume = run.get("approvals") or {}
    aid = resume.get(node.id)
    if aid:
        rec = store.get_approval(aid)
        if rec and rec["status"] != "pending":
            return {"approved": rec["status"] == "approved", "approval_id": aid,
                    "decided_by": rec["decided_by"], "comment": rec["comment"],
                    "value": p.get("value")}
    auto = float(p.get("auto_approve_after", 0) or 0)
    if auto and auto < 0:
        return {"approved": True, "auto": True, "value": p.get("value")}
    aid = store.create_approval(run["id"], run["workflow"], node.id,
                                p.get("prompt", "Approve this step?"), p.get("value"))
    raise PausedForApproval(aid, node.id)


# ------------------------------ agent loop --------------------------------- #
def h_agent(p, ctx, run, node):
    """ReAct-style loop: model picks a tool each step until it calls finish."""
    goal = p.get("goal", "")
    max_steps = int(p.get("max_steps", 5))
    allowed = p.get("tools") or list(tools.TOOLS)
    provider = get_provider(run["provider"])
    trace, scratch = [], ""

    tool_desc = "\n".join(f"- {t}: {tools.TOOLS[t]['desc']}"
                          for t in allowed if t in tools.TOOLS)
    for step in range(max_steps):
        prompt = (f"GOAL: {goal}\n\nAVAILABLE TOOLS:\n{tool_desc}\n\n"
                  f"NOTES SO FAR:\n{scratch or '(none)'}\n\n"
                  "Choose the next tool. Reply as JSON: "
                  '{"tool": "...", "input": "...", "reason": "..."}')
        raw, usage = provider.complete_with_usage(
            prompt=prompt, system="You are a tool-using research agent. Reply strict JSON.",
            model=p.get("model", "auto"), temperature=0.1, json_mode=True,
            agent_step=step, agent_goal=goal, agent_tools=allowed)
        spend(run, usage)
        try:
            decision = json.loads(raw)
        except Exception:
            decision = {"tool": "finish", "input": raw, "reason": "unparseable"}

        tool = decision.get("tool", "finish")
        arg = str(decision.get("input", goal))
        if tool not in allowed or tool not in tools.TOOLS:
            tool = "finish"
        result = tools.TOOLS[tool]["fn"](arg, run)
        trace.append({"step": step + 1, "tool": tool, "input": arg[:200],
                      "reason": decision.get("reason", "")[:200],
                      "observation": str(result)[:400]})
        scratch += f"\n[{step+1}] {tool}({arg[:80]}) -> {str(result)[:200]}"
        if tool == "finish":
            return {"answer": str(result), "steps": step + 1, "trace": trace}

    final, usage = provider.complete_with_usage(
        prompt=f"GOAL: {goal}\nNOTES:{scratch}\n\nGive the final answer.",
        system="Summarize findings into a direct answer.", model="auto", temperature=0.2)
    spend(run, usage)
    return {"answer": final, "steps": max_steps, "trace": trace, "truncated": True}


MAX_SUBWORKFLOW_DEPTH = 5


def h_workflow(p, ctx, run, node):
    """Call another workflow as a single node (sub-workflow / reuse)."""
    import store as _store

    name = p.get("workflow") or p.get("name")
    if not name:
        raise WorkflowError("workflow node needs a 'workflow' name")

    stack = list(run.get("_stack") or [])
    if name in stack or name == run["workflow"]:
        raise WorkflowError(
            f"recursive sub-workflow detected: {' -> '.join(stack + [name])}")
    if len(stack) >= MAX_SUBWORKFLOW_DEPTH:
        raise WorkflowError(f"sub-workflow nesting deeper than {MAX_SUBWORKFLOW_DEPTH}")

    doc = _store.get_workflow(name)
    if not doc:
        raise WorkflowError(f"sub-workflow '{name}' not found")

    payload = p.get("payload")
    if payload is None:
        payload = run["payload"]          # inherit parent payload by default
    if not isinstance(payload, dict):
        raise WorkflowError("sub-workflow payload must be an object")

    child_budget = None
    b = run.get("budget") or {}
    if b:
        used_cost = sum(x.get("cost_usd", 0.0) for x in run["_usage"])
        used_tok = sum(x.get("tokens_in", 0) + x.get("tokens_out", 0) for x in run["_usage"])
        child_budget = {}
        if b.get("max_cost_usd") is not None:
            child_budget["max_cost_usd"] = max(0.0, float(b["max_cost_usd"]) - used_cost)
        if b.get("max_tokens") is not None:
            child_budget["max_tokens"] = max(0, int(b["max_tokens"]) - used_tok)

    sub = run_workflow(Workflow.from_dict(doc), payload, run["provider"],
                       on_event=run.get("_emit_sub"),
                       stack=stack + [run["workflow"]], budget=child_budget,
                       stream=run.get("stream", True))

    # roll the child's spend into the parent's meter
    u = sub.get("usage") or {}
    if u.get("llm_calls"):
        spend(run, {"provider": run["provider"], "model": "sub:" + name,
                    "tokens_in": u.get("tokens_in", 0),
                    "tokens_out": u.get("tokens_out", 0),
                    "cost_usd": u.get("cost_usd", 0.0)})

    if sub["status"] == "paused":
        raise WorkflowError(
            f"sub-workflow '{name}' paused for approval — not supported inside a parent run")
    if sub["status"] == "error" and not p.get("ignore_errors"):
        raise WorkflowError(f"sub-workflow '{name}' failed: {sub.get('error')}")

    outputs = sub.get("outputs") or {}
    result = {"status": sub["status"], "run_id": sub["run_id"], "outputs": outputs,
              "usage": u}
    # a single output node unwraps for ergonomic {{node.field}} access
    if len(outputs) == 1:
        only = next(iter(outputs.values()))
        result["value"] = only
        if isinstance(only, dict):
            for k, v in only.items():
                result.setdefault(k, v)
    return result


HANDLERS: Dict[str, Callable] = {
    "workflow": h_workflow,
    "input": h_input, "template": h_template, "python": h_python, "llm": h_llm,
    "http": h_http, "branch": h_branch, "output": h_output, "map": h_map,
    "filter": h_filter, "validate": h_validate, "chunk": h_chunk, "embed": h_embed,
    "retrieve": h_retrieve, "approval": h_approval, "agent": h_agent,
}


# --------------------------------------------------------------------------- #
# scheduling
# --------------------------------------------------------------------------- #
def topo_order(nodes: List[Node]) -> List[Node]:
    by_id = {n.id: n for n in nodes}
    order_hint = [n.id for n in nodes]
    deps: Dict[str, set] = {}
    for n in nodes:
        d = set(n.depends_on) | {r for r in refs_in(n.params) if r in by_id and r != n.id}
        for key in ("expr", "condition"):
            code = n.params.get(key)
            if isinstance(code, str):
                d |= {t for t in IDENT.findall(code) if t in by_id and t != n.id}
        if isinstance(n.when, str):
            d |= {r for r in refs_in(n.when) if r in by_id and r != n.id}
            d |= {t for t in IDENT.findall(n.when) if t in by_id and t != n.id}
        deps[n.id] = d
    # an approval gate is a barrier: nodes declared after it wait for the decision
    gates: List[str] = []
    for n in nodes:
        if gates:
            deps[n.id] |= set(gates)
        if n.type == "approval":
            gates.append(n.id)

    order, done = [], set()
    while len(order) < len(nodes):
        ready = [n for n in nodes if n.id not in done and deps[n.id] <= done]
        if not ready:
            raise WorkflowError(
                f"cycle or missing dependency among: {[n.id for n in nodes if n.id not in done]}")
        ready.sort(key=lambda n: order_hint.index(n.id))
        for n in ready:
            order.append(n)
            done.add(n.id)
    return order


def dep_map(nodes: List[Node]) -> Dict[str, set]:
    """Same dependency inference topo_order uses, exposed for skip propagation."""
    by_id = {n.id: n for n in nodes}
    deps: Dict[str, set] = {}
    for n in nodes:
        d = set(n.depends_on) | {r for r in refs_in(n.params) if r in by_id and r != n.id}
        for key in ("expr", "condition"):
            code = n.params.get(key)
            if isinstance(code, str):
                d |= {t for t in IDENT.findall(code) if t in by_id and t != n.id}
        if isinstance(n.when, str):
            d |= {r for r in refs_in(n.when) if r in by_id and r != n.id}
            d |= {t for t in IDENT.findall(n.when) if t in by_id and t != n.id}
        deps[n.id] = d
    return deps


# --------------------------------------------------------------------------- #
# executor
# --------------------------------------------------------------------------- #
def run_workflow(wf: Workflow, payload: Dict[str, Any], provider: str = "mock",
                 on_event: Callable[[Dict[str, Any]], None] = None,
                 resume: Dict[str, Any] = None,
                 stack: List[str] = None,
                 budget: Dict[str, Any] = None,
                 stream: bool = True,
                 cache_all: Any = None,
                 no_cache: bool = False,
                 parallel: int = 0) -> Dict[str, Any]:
    resume = resume or {}
    run = {"id": resume.get("run_id") or uuid.uuid4().hex[:12],
           "payload": payload or {}, "provider": provider, "workflow": wf.name,
           "approvals": resume.get("approvals", {}), "_usage": [],
           "_stack": list(stack or []), "_emit_sub": on_event, "_emit": None,
           "stream": stream,
           "budget": dict(budget or wf.budget or {}),
           "cache_all": cache_all, "no_cache": no_cache,
           "parallel": parallel}
    ctx: Dict[str, Any] = dict(resume.get("context") or {})
    logs: List[Dict[str, Any]] = list(resume.get("logs") or [])
    dead_letter: List[Dict[str, Any]] = list(resume.get("dead_letter") or [])
    started = time.time()
    status = "success"
    error = None
    emit = on_event or (lambda e: None)
    run["_emit"] = on_event
    _skipped_ref = [set(resume.get("skipped") or [])]

    def finish(st, err=None, paused=None):
        u = run["_usage"]
        usage = {"tokens_in": sum(x.get("tokens_in", 0) for x in u),
                 "tokens_out": sum(x.get("tokens_out", 0) for x in u),
                 "cost_usd": round(sum(x.get("cost_usd", 0.0) for x in u), 6),
                 "llm_calls": len(u)}
        outs = {n.id: ctx.get(n.id) for n in wf.nodes
                if n.type == "output" and n.id in ctx and n.id not in _skipped_ref[0]}
        if not outs and ctx:
            last = [n.id for n in wf.nodes if n.id in ctx]
            outs = {last[-1]: ctx[last[-1]]} if last else {}
        res = {"run_id": run["id"], "workflow": wf.name, "status": st, "provider": provider,
               "outputs": outs, "context": ctx, "logs": logs, "usage": usage,
               "dead_letter": dead_letter, "error": err, "payload": run["payload"],
               "skipped": sorted(_skipped_ref[0]), "budget": run.get("budget") or {},
               "duration_ms": int((time.time() - started) * 1000)
               + int(resume.get("duration_ms", 0)),
               "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        if paused:
            res["paused"] = paused
        emit({"event": "run_end", **{k: res[k] for k in ("run_id", "status", "usage")}})
        return res

    try:
        order = topo_order(wf.nodes)
    except WorkflowError as e:
        return finish("error", str(e))

    completed = {l["node"] for l in logs
                 if l.get("status") in ("success", "skipped", "cached", "recovered")}
    emit({"event": "run_start", "run_id": run["id"], "workflow": wf.name,
          "nodes": [n.id for n in order]})

    deps_of = dep_map(wf.nodes)
    skipped: set = _skipped_ref[0]

    # ---- per-node execution, shared by the serial and parallel schedulers ----
    # Returns (kind, payload):
    #   ("done",   entry)             node finished (success / skipped / recovered / ...)
    #   ("halt",   (st, err, paused)) the whole run must stop
    _lock = threading.Lock()

    def eval_skip(node):
        """Decide whether a node is skipped. Returns (skip_reason, error_or_None)."""
        node_deps = deps_of.get(node.id, set())
        upstream_skipped = sorted(node_deps & skipped)
        if node_deps and upstream_skipped and set(node_deps) <= skipped:
            return f"upstream skipped: {', '.join(upstream_skipped)}", None
        if node.when is not None:
            try:
                cond = render(node.when, {**ctx, "payload": run["payload"]})
                keep = safe_eval(cond, _scope(ctx, run)) if isinstance(cond, str) else bool(cond)
            except Exception as e:  # noqa: BLE001
                return None, f"when-condition failed: {type(e).__name__}: {e}"
            if not keep:
                return f"when: {node.when}", None
        return None, None

    def run_one(node):
        skip_reason, when_err = eval_skip(node)
        if when_err:
            entry = {"node": node.id, "type": node.type, "status": "error",
                     "attempts": 1, "output": None, "policy": "stop",
                     "error": when_err, "duration_ms": 0}
            return "halt", (entry, "error", f"node '{node.id}': {when_err}", None)

        if skip_reason:
            entry = {"node": node.id, "type": node.type, "status": "skipped",
                     "attempts": 0, "output": None, "error": None,
                     "policy": "when", "reason": skip_reason, "duration_ms": 0,
                     "usage": {"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0}}
            return "done", entry

        policy = node.on_error or wf.on_error or "stop"
        t0 = time.time()
        emit({"event": "node_start", "node": node.id, "type": node.type})
        entry = {"node": node.id, "type": node.type, "status": "success",
                 "attempts": 0, "output": None, "error": None, "policy": policy}

        handler = HANDLERS.get(node.type)
        if handler is None:
            entry.update(status="error", error=f"unknown node type '{node.type}'",
                         duration_ms=0)
            return "halt", (entry, "error", entry["error"], None)

        # --- per-node cache ------------------------------------------------ #
        cache_cfg = node.cache if node.cache is not None else run.get("cache_all")
        ckey = None
        if cache_cfg and node.type in CACHEABLE and not run.get("no_cache"):
            import store as _store
            try:
                rendered = render(node.params, {**ctx, "payload": run["payload"]})
                ckey = cache_key(wf.name, node, rendered, provider)
                ttl = float(cache_cfg.get("ttl", 0)) if isinstance(cache_cfg, dict) else 0
                hit = _store.cache_get(ckey, ttl)
            except Exception:
                hit = None
            if hit is not None:
                entry.update(status="cached", output=hit["value"], attempts=0,
                             duration_ms=int((time.time() - t0) * 1000),
                             cache_hits=hit["hits"],
                             usage={"tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                                    "saved_usd": (hit.get("usage") or {}).get("cost_usd", 0.0)})
                return "done", entry

        last_err = None
        with _lock:
            usage_before = len(run["_usage"])
        for attempt in range(node.retries + 1):
            entry["attempts"] = attempt + 1
            try:
                params = render(node.params, {**ctx, "payload": run["payload"]})
                entry["output"] = call_with_timeout(
                    lambda: handler(params, ctx, run, node), node.timeout)
                last_err = None
                break
            except PausedForApproval as pause:
                pentry = {**entry, "status": "paused", "error": None,
                          "approval_id": pause.approval_id,
                          "duration_ms": int((time.time() - t0) * 1000)}
                emit({"event": "paused", "node": node.id,
                      "approval_id": pause.approval_id})
                return "halt", (pentry, "paused", None,
                                {"node": node.id, "approval_id": pause.approval_id})
            except BudgetExceeded as e:
                entry.update(status="budget_exceeded", error=str(e),
                             duration_ms=int((time.time() - t0) * 1000))
                return "halt", (entry, "budget_exceeded", f"node '{node.id}': {e}", None)
            except NodeTimeout as e:
                last_err = f"NodeTimeout: {e}"
                entry["timed_out"] = True
                if attempt < node.retries:
                    time.sleep(min(0.5, 0.05 * (2 ** attempt)))
            except (SandboxError, WorkflowError, NameError, SyntaxError) as e:
                last_err = f"{type(e).__name__}: {e}"
                break  # deterministic failures: no point retrying
            except Exception as e:  # noqa: BLE001
                last_err = f"{type(e).__name__}: {e}"
                if attempt < node.retries:
                    backoff = min(2.0, 0.05 * (2 ** attempt)) * (1 + random.random() * 0.3)
                    time.sleep(backoff)

        entry["duration_ms"] = int((time.time() - t0) * 1000)
        with _lock:
            step_usage = run["_usage"][usage_before:]
        entry["usage"] = {"tokens_in": sum(u.get("tokens_in", 0) for u in step_usage),
                          "tokens_out": sum(u.get("tokens_out", 0) for u in step_usage),
                          "cost_usd": round(sum(u.get("cost_usd", 0.0) for u in step_usage), 6)}

        if last_err:
            entry["error"] = last_err
            if policy == "continue":
                entry["status"] = "skipped"
            elif policy == "fallback":
                entry["status"] = "recovered"
                entry["output"] = render(node.fallback, {**ctx, "payload": run["payload"]})
            elif policy == "dead_letter":
                entry["status"] = "dead_letter"
            else:
                entry["status"] = "error"
                return "halt", (entry, "error", last_err, None)
        elif ckey:
            entry["_ckey"] = ckey
        return "done", entry

    def commit(entry):
        """Apply a finished node's result to the shared run state."""
        nonlocal status
        nid = entry["node"]
        st = entry["status"]
        if st == "skipped" and entry.get("policy") == "when":
            skipped.add(nid)
            ctx[nid] = None
        elif st in ("skipped", "dead_letter"):
            ctx[nid] = None
            if st == "dead_letter":
                dead_letter.append({"node": nid, "error": entry.get("error"),
                                    "params": by_id[nid].params})
            status = "partial"
        elif st == "recovered":
            ctx[nid] = entry["output"]
            status = "partial"
        else:
            ctx[nid] = entry["output"]
            ck = entry.pop("_ckey", None)
            if ck:
                try:
                    import store as _store
                    _store.cache_put(ck, wf.name, nid, entry["output"], entry.get("usage"))
                except Exception:
                    pass
        logs.append(entry)
        emit({"event": "node_end", **entry})

    by_id = {n.id: n for n in wf.nodes}
    pending = [n for n in order if not (n.id in completed and n.id in ctx)]
    done_ids = set(completed) | set(ctx)

    if parallel and parallel > 1:
        # ---- wave scheduler: run every ready node concurrently -------------- #
        from concurrent.futures import ThreadPoolExecutor
        remaining = list(pending)
        while remaining:
            ready = [n for n in remaining if deps_of.get(n.id, set()) <= done_ids]
            if not ready:
                return finish("error",
                              f"deadlock among: {[n.id for n in remaining]}")
            # a node that pauses or is order-sensitive runs alone
            batch = ready if len(ready) > 1 else ready[:1]
            width = min(len(batch), parallel)
            if width > 1:
                with ThreadPoolExecutor(max_workers=width) as ex:
                    results = list(ex.map(run_one, batch))
            else:
                results = [run_one(batch[0])]

            for node, (kind, payload) in zip(batch, results):
                if kind == "halt":
                    entry, st, err, paused = payload
                    logs.append(entry)
                    emit({"event": "node_end", **entry})
                    return finish(st, err, paused)
                commit(payload)
                done_ids.add(node.id)
            remaining = [n for n in remaining if n.id not in done_ids]
    else:
        # ---- serial scheduler (default): strict declaration order ----------- #
        for node in pending:
            kind, payload = run_one(node)
            if kind == "halt":
                entry, st, err, paused = payload
                logs.append(entry)
                emit({"event": "node_end", **entry})
                return finish(st, err, paused)
            commit(payload)
            done_ids.add(node.id)

    return finish(status, error)

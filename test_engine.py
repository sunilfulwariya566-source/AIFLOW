"""Full test suite: python3 test_engine.py  (no pytest needed)."""
import json
import os
import sys
import time

os.environ.setdefault("AIFLOW_AUTH", "1")

import store
from engine import HANDLERS, Node, Workflow, render, run_workflow, topo_order
from sandbox import SandboxError, safe_eval

PASS = FAIL = 0
SECTION = ""


def check(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}  {extra}")


def sec(s):
    global SECTION
    SECTION = s
    print(s)


def wf(nodes, name="t", **kw):
    return Workflow.from_dict({"name": name, "nodes": nodes, **kw})


store.init()

# --------------------------------------------------------------------------- #
sec("templating")
ctx = {"a": {"b": [1, 2, 3]}, "s": "hi"}
check("nested ref", render("{{a.b.1}}", ctx) == 2)
check("interpolation", render("x={{s}}!", ctx) == "x=hi!")
check("type preserved", render("{{a.b}}", ctx) == [1, 2, 3])
check("missing -> empty", render("[{{nope}}]", ctx) == "[]")
check("negative index", render("{{a.b.-1}}", ctx) == 3)
check("deep structure", render({"k": ["{{s}}"]}, ctx) == {"k": ["hi"]})

sec("sandbox — allowed")
check("comprehension", safe_eval("[x*2 for x in [1,2,3]]", {}) == [2, 4, 6])
check("json.loads", safe_eval("json.loads(s)", {"s": '{"a":1}'}) == {"a": 1})
check("dict methods", safe_eval("d.get('k','z')", {"d": {}}) == "z")
check("f-string", safe_eval("f'{n} items'", {"n": 3}) == "3 items")

sec("sandbox — blocked")
for expr, label in [("__import__('os')", "import"),
                    ("().__class__.__bases__", "dunder chain"),
                    ("open('/etc/passwd')", "open"),
                    ("(lambda: 1)()", "lambda"),
                    ("eval('1')", "eval"),
                    ("getattr(x, 'y')", "getattr"),
                    ("x.__globals__", "globals attr"),
                    ("[0]*10**9", "memory bomb"),
                    ("9**9**9", "cpu bomb")]:
    try:
        safe_eval(expr, {"x": 1})
        check(f"blocks {label}", False, "LEAKED")
    except SandboxError:
        check(f"blocks {label}", True)
    except Exception as e:
        check(f"blocks {label}", False, f"wrong error {type(e).__name__}")

sec("dependency resolution")
order = topo_order(wf([
    {"id": "c", "type": "python", "params": {"expr": "a + b"}},
    {"id": "a", "type": "input", "params": {"key": "a", "default": 1}},
    {"id": "b", "type": "python", "params": {"expr": "a * 2"}},
]).nodes)
check("implicit deps sorted", [n.id for n in order] == ["a", "b", "c"], [n.id for n in order])
r = run_workflow(wf([{"id": "x", "type": "python", "params": {"expr": "y"}},
                     {"id": "y", "type": "python", "params": {"expr": "x"}}]), {})
check("cycle detected", r["status"] == "error" and "cycle" in r["error"])
o = topo_order(wf([{"id": "b", "type": "template", "params": {"text": "x"},
                    "depends_on": ["a"]},
                   {"id": "a", "type": "template", "params": {"text": "y"}}]).nodes)
check("explicit depends_on", [n.id for n in o] == ["a", "b"])

sec("execution + usage")
r = run_workflow(wf([
    {"id": "n", "type": "input", "params": {"key": "n", "required": True}},
    {"id": "sq", "type": "python", "params": {"expr": "n * n"}},
    {"id": "out", "type": "output", "params": {"value": {"sq": "{{sq}}"}}}]), {"n": 7})
check("chain runs", r["status"] == "success" and r["outputs"]["out"]["sq"] == 49)
check("trace complete", len(r["logs"]) == 3)
check("usage block present", "cost_usd" in r["usage"] and "llm_calls" in r["usage"])
r = run_workflow(wf([{"id": "l", "type": "llm", "params": {"prompt": "Summarize: hello"}}]), {})
check("tokens counted", r["usage"]["tokens_in"] > 0 and r["usage"]["tokens_out"] > 0)
check("per-node usage", r["logs"][0]["usage"]["tokens_out"] > 0)
check("required input enforced",
      run_workflow(wf([{"id": "n", "type": "input",
                        "params": {"key": "n", "required": True}}]), {})["status"] == "error")
check("unknown node type",
      run_workflow(wf([{"id": "z", "type": "nope", "params": {}}]), {})["status"] == "error")

sec("retries + error policies")
r = run_workflow(wf([{"id": "b", "type": "http",
                      "params": {"url": "http://127.0.0.1:9/x", "mock": False},
                      "retries": 2}]), {}, "openai")
check("retries attempted", r["logs"][0]["attempts"] == 3, r["logs"][0]["attempts"])
check("stop halts run", r["status"] == "error")
r = run_workflow(wf([{"id": "bad", "type": "python", "params": {"expr": "1/0"},
                      "on_error": "continue"},
                     {"id": "ok", "type": "template", "params": {"text": "went on"}}]), {})
check("continue policy", r["status"] == "partial" and r["context"]["ok"] == "went on")
r = run_workflow(wf([{"id": "bad", "type": "python", "params": {"expr": "1/0"},
                      "on_error": "fallback", "fallback": "SAFE"}]), {})
check("fallback policy", r["context"]["bad"] == "SAFE" and r["logs"][0]["status"] == "recovered")
r = run_workflow(wf([{"id": "bad", "type": "python", "params": {"expr": "1/0"},
                      "on_error": "dead_letter"}]), {})
check("dead letter captured", len(r["dead_letter"]) == 1 and r["dead_letter"][0]["node"] == "bad")
r = run_workflow(wf([{"id": "bad", "type": "python", "params": {"expr": "1/0"}}],
                    on_error="continue"), {})
check("workflow-level policy", r["status"] == "partial")
t0 = time.time()
run_workflow(wf([{"id": "b", "type": "python", "params": {"expr": "nope_undefined"},
                  "retries": 3}]), {})
check("no retry on deterministic error", time.time() - t0 < 0.4)

sec("branch / filter / validate / map")
check("branch", run_workflow(wf([{"id": "v", "type": "input", "params": {"key": "v"}},
      {"id": "b", "type": "branch", "params": {"condition": "v > 10",
       "if_true": "BIG", "if_false": "SMALL"}}]), {"v": 42})["context"]["b"] == "BIG")
check("filter", run_workflow(wf([{"id": "f", "type": "filter",
      "params": {"over": [1, 2, 3, 4, 5], "condition": "item % 2 == 0"}}]),
      {})["context"]["f"] == [2, 4])
res = run_workflow(wf([{"id": "v", "type": "validate", "params": {
    "value": {"category": "billing", "priority": "urgent"}, "soft": True,
    "schema": {"required": ["category", "sentiment"],
               "properties": {"priority": {"type": "string", "enum": ["low", "high"]}}}}}]),
    {})["context"]["v"]
check("validate soft finds 2", not res["valid"] and len(res["errors"]) == 2, res["errors"])
check("validate hard fails", run_workflow(wf([{"id": "v", "type": "validate", "params": {
    "value": {}, "schema": {"required": ["x"]}}}]), {})["status"] == "error")
r = run_workflow(wf([{"id": "m", "type": "map", "params": {
    "over": ["a", "b", "c"], "step": {"type": "python",
                                      "params": {"expr": "str(item).upper()"}}}}]), {})
check("map fans out", r["context"]["m"] == ["A", "B", "C"], r["context"].get("m"))
r = run_workflow(wf([{"id": "m", "type": "map", "params": {
    "over": ["t1", "t2"], "workers": 4,
    "step": {"type": "llm", "params": {"prompt": "Classify: {{item}}"}}}}]), {})
check("map over llm", len(r["context"]["m"]) == 2)
check("map usage aggregated", r["usage"]["llm_calls"] == 2, r["usage"])
r = run_workflow(wf([{"id": "m", "type": "map", "params": {
    "over": list(range(100)), "limit": 5,
    "step": {"type": "python", "params": {"expr": "item"}}}}]), {})
check("map limit honoured", len(r["context"]["m"]) == 5)

sec("RAG nodes")
r = run_workflow(wf([{"id": "c", "type": "chunk", "params": {
    "text": " ".join(f"w{i}" for i in range(600)), "size": 100, "overlap": 20}}]), {})
check("chunking overlaps", len(r["context"]["c"]) >= 7, len(r["context"]["c"]))
r = run_workflow(wf([
    {"id": "e", "type": "embed", "params": {"collection": "test_kb", "reset": True,
     "texts": ["Retries use exponential backoff with jitter.",
               "The scheduler persists schedules to SQLite.",
               "Bananas are yellow and grow in the tropics."]}},
    {"id": "h", "type": "retrieve", "params": {"collection": "test_kb",
     "query": "how are retries handled?", "k": 2}, "depends_on": ["e"]}]), {})
check("embed indexes", r["context"]["e"]["count"] == 3)
check("retrieve ranks correctly", "backoff" in r["context"]["h"]["matches"][0]["text"],
      r["context"]["h"]["matches"][0]["text"][:40])
check("retrieve builds context", r["context"]["h"]["context"].startswith("Retries use"))
check("retrieve drops irrelevant", all("Banana" not in m["text"]
                                       for m in r["context"]["h"]["matches"]))

sec("agent loop")
r = run_workflow(wf([{"id": "a", "type": "agent", "params": {
    "goal": "assess workflow automation", "max_steps": 4,
    "tools": ["search", "calculator", "finish"]}}]), {})
a = r["context"]["a"]
check("agent terminates", r["status"] == "success" and a["steps"] <= 4, a.get("steps"))
check("agent used tools", len(a["trace"]) >= 2 and a["trace"][0]["tool"] == "search")
check("agent produced answer", len(a["answer"]) > 20)
r = run_workflow(wf([{"id": "a", "type": "agent", "params": {
    "goal": "x", "max_steps": 2, "tools": ["calculator", "finish"]}}]), {})
check("agent respects max_steps", r["context"]["a"]["steps"] <= 2)

sec("approval gates")
r = run_workflow(wf([
    {"id": "t", "type": "template", "params": {"text": "draft"}},
    {"id": "gate", "type": "approval", "params": {"prompt": "ok?", "value": "{{t}}"}},
    {"id": "after", "type": "template", "params": {"text": "published"}}], name="appr"), {})
check("run pauses", r["status"] == "paused" and r["paused"]["node"] == "gate")
check("later node not run", "after" not in r["context"])
aid = r["paused"]["approval_id"]
check("approval persisted", store.get_approval(aid)["status"] == "pending")
store.decide_approval(aid, True, "tester", "looks good")
r2 = run_workflow(wf([
    {"id": "t", "type": "template", "params": {"text": "draft"}},
    {"id": "gate", "type": "approval", "params": {"prompt": "ok?", "value": "{{t}}"}},
    {"id": "after", "type": "template", "params": {"text": "published"}}], name="appr"), {},
    resume={"run_id": r["run_id"], "context": r["context"],
            "logs": [l for l in r["logs"] if l["status"] != "paused"],
            "approvals": {"gate": aid}})
check("resume completes", r2["status"] == "success" and r2["context"]["after"] == "published")
check("approval decision carried", r2["context"]["gate"]["approved"] is True)
check("run id preserved on resume", r2["run_id"] == r["run_id"])
r3 = run_workflow(wf([{"id": "g", "type": "approval",
                       "params": {"prompt": "?", "auto_approve_after": -1}}]), {})
check("auto-approve path", r3["status"] == "success" and r3["context"]["g"]["approved"])

sec("conditional edges (when)")
cond = [{"id": "score", "type": "input", "params": {"key": "score"}},
        {"id": "hi", "type": "template", "params": {"text": "HIGH"}, "when": "score > 50"},
        {"id": "lo", "type": "template", "params": {"text": "LOW"}, "when": "score <= 50"},
        {"id": "after", "type": "template", "params": {"text": "got {{hi}}"}},
        {"id": "out", "type": "output", "params": {"value": {"h": "{{hi}}", "l": "{{lo}}"}}}]
r = run_workflow(wf(cond), {"score": 90})
check("true branch runs", r["context"]["hi"] == "HIGH")
check("false branch skipped", r["context"]["lo"] is None and "lo" in r["skipped"])
check("skip propagates to dead branch", "after" not in r["skipped"])
r = run_workflow(wf(cond), {"score": 10})
check("inverse branch runs", r["context"]["lo"] == "LOW")
check("dead branch child skipped", "after" in r["skipped"], r["skipped"])
check("join node still runs", r["context"]["out"] is not None)
check("skipped logged with reason",
      any(l["node"] == "hi" and l["status"] == "skipped" and "when" in (l.get("reason") or "")
          for l in r["logs"]))
check("skipped costs nothing",
      all(l.get("usage", {}).get("cost_usd", 0) == 0
          for l in r["logs"] if l["status"] == "skipped"))
r = run_workflow(wf([{"id": "a", "type": "template", "params": {"text": "x"},
                      "when": "this is not python"}]), {})
check("bad when errors clearly", r["status"] == "error" and "when-condition" in r["error"])
r = run_workflow(wf([{"id": "p", "type": "input", "params": {"key": "p"}},
                     {"id": "n", "type": "template", "params": {"text": "y"},
                      "when": "p.flag"}]), {"p": {"flag": True}})
check("when reads dict attr", r["context"]["n"] == "y")
r = run_workflow(wf([{"id": "gate", "type": "template", "params": {"text": "g"},
                      "when": "False"},
                     {"id": "chain", "type": "template", "params": {"text": "{{gate}}"}},
                     {"id": "tail", "type": "template", "params": {"text": "{{chain}}"}}]), {})
check("skip cascades transitively", set(r["skipped"]) == {"gate", "chain", "tail"}, r["skipped"])

sec("sub-workflows")
store.save_workflow({"name": "_sub_child", "description": "child",
                     "nodes": [{"id": "t", "type": "input", "params": {"key": "text", "required": True}},
                               {"id": "u", "type": "python", "params": {"expr": "str(t).upper()"}},
                               {"id": "o", "type": "output", "params": {"value": {"shout": "{{u}}"}}}]})
r = run_workflow(wf([{"id": "c", "type": "workflow",
                      "params": {"workflow": "_sub_child", "payload": {"text": "hello"}}}]), {})
check("sub-workflow runs", r["status"] == "success", r.get("error"))
check("sub outputs exposed", r["context"]["c"]["outputs"]["o"]["shout"] == "HELLO")
check("single output unwrapped", r["context"]["c"]["shout"] == "HELLO")
check("sub run_id recorded", len(r["context"]["c"]["run_id"]) == 12)
r = run_workflow(wf([{"id": "d", "type": "input", "params": {"key": "doc"}},
                     {"id": "c", "type": "workflow",
                      "params": {"workflow": "_sub_child", "payload": {"text": "{{d}}"}}},
                     {"id": "o", "type": "output", "params": {"value": "{{c.shout}}"}}]),
                 {"doc": "chained"})
check("parent templates into child", r["outputs"]["o"] == "CHAINED")
r = run_workflow(wf([{"id": "c", "type": "workflow", "params": {"workflow": "_sub_child"}}]),
                 {"text": "inherited"})
check("payload inherited by default", r["context"]["c"]["shout"] == "INHERITED")
r = run_workflow(wf([{"id": "c", "type": "workflow", "params": {"workflow": "_nope"}}]), {})
check("missing sub-workflow errors", r["status"] == "error" and "not found" in r["error"])
store.save_workflow({"name": "_sub_loop", "nodes": [
    {"id": "me", "type": "workflow", "params": {"workflow": "_sub_loop"}}]})
r = run_workflow(Workflow.from_dict(store.get_workflow("_sub_loop")), {})
check("self-recursion blocked", r["status"] == "error" and "recursive" in r["error"])
store.save_workflow({"name": "_sub_a", "nodes": [
    {"id": "x", "type": "workflow", "params": {"workflow": "_sub_b"}}]})
store.save_workflow({"name": "_sub_b", "nodes": [
    {"id": "y", "type": "workflow", "params": {"workflow": "_sub_a"}}]})
r = run_workflow(Workflow.from_dict(store.get_workflow("_sub_a")), {})
check("mutual recursion blocked", r["status"] == "error" and "recursive" in r["error"])
store.save_workflow({"name": "_sub_llm", "nodes": [
    {"id": "q", "type": "llm", "params": {"prompt": "Summarize: hi"}},
    {"id": "o", "type": "output", "params": {"value": "{{q}}"}}]})
r = run_workflow(wf([{"id": "c", "type": "workflow", "params": {"workflow": "_sub_llm"}}]), {})
check("child usage rolls into parent", r["usage"]["tokens_out"] > 0, r["usage"])
store.save_workflow({"name": "_sub_bad", "nodes": [
    {"id": "b", "type": "python", "params": {"expr": "1/0"}}]})
r = run_workflow(wf([{"id": "c", "type": "workflow", "params": {"workflow": "_sub_bad"}}]), {})
check("child failure surfaces", r["status"] == "error" and "_sub_bad" in r["error"])
r = run_workflow(wf([{"id": "c", "type": "workflow",
                      "params": {"workflow": "_sub_bad", "ignore_errors": True}}]), {})
check("ignore_errors tolerates failure", r["status"] == "success")
for n in ("_sub_child", "_sub_loop", "_sub_a", "_sub_b", "_sub_llm", "_sub_bad"):
    store.delete_workflow(n)

sec("budget caps")
three = [{"id": "a", "type": "llm", "params": {"prompt": "one"}},
         {"id": "b", "type": "llm", "params": {"prompt": "two"}},
         {"id": "c", "type": "llm", "params": {"prompt": "three"}}]
r = run_workflow(wf(three), {}, budget={"max_llm_calls": 2})
check("call cap stops run", r["status"] == "budget_exceeded", r["status"])
check("cap stops at the right node", r["context"].get("a") and r["context"].get("c") is None)
check("partial work is kept", len([l for l in r["logs"] if l["status"] == "success"]) == 2)
check("breach is reported", "LLM calls exceeds budget" in (r["error"] or ""))
r = run_workflow(wf(three), {}, budget={"max_tokens": 5})
check("token cap enforced", r["status"] == "budget_exceeded" and "tokens" in r["error"])
r = run_workflow(wf(three), {}, budget={"max_cost_usd": 0.0})
check("zero-cost budget survives free mock", r["status"] == "success", r["error"])
r = run_workflow(wf(three), {})
check("no budget = no limit", r["status"] == "success" and r["usage"]["llm_calls"] == 3)
r = run_workflow(wf(three, budget={"max_llm_calls": 1}), {})
check("workflow-level budget applies", r["status"] == "budget_exceeded")
r = run_workflow(wf([{"id": "x", "type": "llm", "params": {"prompt": "p"},
                      "on_error": "continue"},
                     {"id": "y", "type": "llm", "params": {"prompt": "q"},
                      "on_error": "continue"}]), {}, budget={"max_llm_calls": 1})
check("budget overrides on_error=continue", r["status"] == "budget_exceeded")
check("budget echoed in result", r["budget"]["max_llm_calls"] == 1)
store.save_workflow({"name": "_bud_child", "nodes": [
    {"id": "q", "type": "llm", "params": {"prompt": "child"}},
    {"id": "o", "type": "output", "params": {"value": "{{q}}"}}]})
r = run_workflow(wf([{"id": "s1", "type": "workflow", "params": {"workflow": "_bud_child"}},
                     {"id": "s2", "type": "workflow", "params": {"workflow": "_bud_child"}},
                     {"id": "s3", "type": "workflow", "params": {"workflow": "_bud_child"}}]),
                 {}, budget={"max_llm_calls": 2})
check("budget spans sub-workflows", r["status"] == "budget_exceeded", r["status"])
store.delete_workflow("_bud_child")

sec("alerting")
import alerts as _al
import uuid as _uuid
for a in store.list_alerts():
    store.del_alert(a["id"])
# unique scope per test run so leftover rows from earlier runs can't pollute the window
AWF = "_alerts_" + _uuid.uuid4().hex[:6]
# scope to a private workflow so unrelated runs can't pollute the window
rule = store.create_alert({"name": "t-fail", "metric": "error_count", "op": ">",
                           "threshold": 0, "window_runs": 5, "workflow": AWF,
                           "cooldown_s": 0})
check("alert persisted", store.get_alert(rule["id"])["metric"] == "error_count")
check("alert listed", any(a["id"] == rule["id"] for a in store.list_alerts()))
store.add_run({"run_id": "_ok_" + AWF, "workflow": AWF, "status": "success",
               "provider": "mock", "duration_ms": 5, "usage": {}, "trigger": "test"})
fired = _al.evaluate(AWF)
check("clean window does not fire", not any(f["alert_id"] == rule["id"] for f in fired),
      [f["name"] for f in fired])
store.add_run({"run_id": "_bad_" + AWF, "workflow": AWF, "status": "error",
               "provider": "mock", "duration_ms": 5, "usage": {}, "trigger": "test"})
fired = _al.evaluate(AWF)
check("failure fires the rule", any(f["alert_id"] == rule["id"] for f in fired))
check("event recorded", any(e["alert_id"] == rule["id"] for e in store.list_alert_events(10)))
check("delivery marked ok", store.list_alert_events(1)[0]["delivered"] == 1)
store.del_alert(rule["id"])
cd_rule = store.create_alert({"name": "t-cd", "metric": "error_count", "op": ">",
                              "threshold": 0, "window_runs": 5, "workflow": AWF,
                              "cooldown_s": 999})
_al.evaluate()
before = len(store.list_alert_events(50))
_al.evaluate()
check("cooldown suppresses repeats", len(store.list_alert_events(50)) == before)
store.del_alert(cd_rule["id"])
scoped = store.create_alert({"name": "t-scope", "metric": "error_count", "op": ">",
                             "threshold": 0, "workflow": "_other_wf", "window_runs": 5,
                             "cooldown_s": 0})
fired = _al.evaluate()
check("scoped rule ignores other workflows",
      not any(f["alert_id"] == scoped["id"] for f in fired))
store.del_alert(scoped["id"])
lo = store.create_alert({"name": "t-lt", "metric": "avg_ms", "op": "<",
                         "threshold": 999999, "window_runs": 5, "cooldown_s": 0})
check("less-than operator works", any(f["alert_id"] == lo["id"] for f in _al.evaluate()))
store.del_alert(lo["id"])
m = store.alert_metrics(window=5)
check("metrics expose all fields",
      all(k in m for k in ("failure_rate", "p95_ms", "avg_ms", "cost_usd",
                           "error_count", "budget_exceeded", "runs")))
seen = []
_al.subscribe(seen.append)
push = store.create_alert({"name": "t-push", "metric": "error_count", "op": ">=",
                           "threshold": 0, "window_runs": 5, "cooldown_s": 0})
_al.evaluate()
check("subscribers notified", any(e.get("event") == "alert" for e in seen))
store.del_alert(push["id"])

sec("bundle export / import")
import bundle as _b
for n in ("_bx_child", "_bx_parent", "_bx_child-imported", "_bx_parent-imported"):
    store.delete_workflow(n)
store.save_workflow({"name": "_bx_child", "description": "child", "nodes": [
    {"id": "t", "type": "input", "params": {"key": "text"}},
    {"id": "o", "type": "output", "params": {"value": "{{t}}"}}]})
store.save_workflow({"name": "_bx_parent", "description": "parent", "budget": {"max_llm_calls": 9},
                     "nodes": [
    {"id": "c", "type": "workflow", "params": {"workflow": "_bx_child"}},
    {"id": "o", "type": "output", "params": {"value": "{{c}}"}}]})
bd = _b.export_bundle(["_bx_parent"])
check("bundle marker present", bd["aiflow_bundle"] == 1)
check("sub-workflow pulled in", {w["name"] for w in bd["workflows"]} == {"_bx_child", "_bx_parent"})
check("dependency ordered first", bd["workflows"][0]["name"] == "_bx_child")
check("budget travels", bd["workflows"][1]["budget"]["max_llm_calls"] == 9)
check("no deps when disabled", _b.export_bundle(["_bx_parent"], False)["count"] == 1)
check("missing names reported", "_ghost" in _b.export_bundle(["_ghost"])["missing"])

dry = _b.import_bundle(bd, "rename", dry_run=True)
check("dry run imports nothing", dry["dry_run"] and store.get_workflow("_bx_child-imported") is None)
check("dry run predicts renames",
      all(r["action"] == "renamed" for r in dry["results"]), dry["results"])
res = _b.import_bundle(bd, "rename")
check("rename import writes", store.get_workflow("_bx_parent-imported") is not None)
sub = [n["params"]["workflow"] for n in store.get_workflow("_bx_parent-imported")["nodes"]
       if n["type"] == "workflow"]
check("sub-refs rewired to copies", sub == ["_bx_child-imported"], sub)
res2 = _b.import_bundle(bd, "skip")
check("skip mode leaves originals", all(r["action"] == "skipped" for r in res2["results"]))
before = store.get_workflow("_bx_parent")["version"]
_b.import_bundle(bd, "overwrite")
check("overwrite bumps version", store.get_workflow("_bx_parent")["version"] == before + 1)

for bad, label in (({"nope": 1}, "no marker"),
                   ({"aiflow_bundle": 1, "workflows": []}, "empty"),
                   ({"aiflow_bundle": 99, "workflows": [{"name": "x", "nodes": []}]}, "future version"),
                   ({"aiflow_bundle": 1, "workflows": [{"nodes": []}]}, "nameless"),
                   ({"aiflow_bundle": 1, "workflows": [{"name": "d", "nodes": [
                       {"id": "a", "type": "template"}, {"id": "a", "type": "template"}]}]}, "dup ids")):
    try:
        _b.validate_bundle(bad)
        check(f"rejects {label}", False, "accepted")
    except _b.BundleError:
        check(f"rejects {label}", True)
check("bare workflow accepted",
      len(_b.validate_bundle({"name": "solo", "nodes": []})) == 1)
check("json string accepted", len(_b.validate_bundle(json.dumps(bd))) == 2)
try:
    _b.import_bundle(bd, "nonsense")
    check("bad mode rejected", False)
except _b.BundleError:
    check("bad mode rejected", True)
for n in ("_bx_child", "_bx_parent", "_bx_child-imported", "_bx_parent-imported"):
    store.delete_workflow(n)

sec("workflow version diff")
store.delete_workflow("_vdiff")
store.save_workflow({"name": "_vdiff", "description": "first", "nodes": [
    {"id": "a", "type": "input", "params": {"key": "x"}},
    {"id": "b", "type": "llm", "params": {"prompt": "one"}}]})
store.save_workflow({"name": "_vdiff", "description": "second", "on_error": "continue",
                     "nodes": [
    {"id": "a", "type": "input", "params": {"key": "x"}},
    {"id": "b", "type": "llm", "params": {"prompt": "TWO"}, "retries": 3, "when": "a"},
    {"id": "c", "type": "output", "params": {"value": "{{b}}"}}]})
vd = _b.diff_versions("_vdiff", 1, 2)
check("diff counts nodes", vd["summary"] == {"added": 1, "removed": 0,
                                             "changed": 1, "unchanged": 1}, vd["summary"])
check("added node listed", vd["added"][0]["node"] == "c")
check("unchanged node listed", vd["unchanged"] == ["a"], vd["unchanged"])
fields = {f["field"] for c in vd["changed"] for f in c["fields"]}
check("changed fields detected", fields == {"params", "retries", "when"}, fields)
check("workflow meta diffed", {m["field"] for m in vd["meta"]} == {"description", "on_error"},
      [m["field"] for m in vd["meta"]])
check("not identical", vd["identical"] is False)
check("self diff is identical", _b.diff_versions("_vdiff", 2, 2)["identical"] is True)
store.save_workflow({"name": "_vdiff", "description": "second", "on_error": "continue",
                     "nodes": [
    {"id": "c", "type": "output", "params": {"value": "{{b}}"}},
    {"id": "a", "type": "input", "params": {"key": "x"}},
    {"id": "b", "type": "llm", "params": {"prompt": "TWO"}, "retries": 3, "when": "a"}]})
vd2 = _b.diff_versions("_vdiff", 2, 3)
check("reordering detected", vd2["reordered"] is True and not vd2["changed"], vd2["changed"])
check("reorder is not identical", vd2["identical"] is False)
store.save_workflow({"name": "_vdiff", "description": "second", "on_error": "continue",
                     "nodes": [{"id": "a", "type": "input", "params": {"key": "x"}}]})
vd3 = _b.diff_versions("_vdiff", 3, 4)
check("removals detected", {r["node"] for r in vd3["removed"]} == {"b", "c"},
      vd3["removed"])
for bad in ((99, 1), (1, 99)):
    try:
        _b.diff_versions("_vdiff", *bad)
        check(f"missing version {bad} errors", False)
    except _b.BundleError:
        check(f"missing version {bad} errors", True)
store.delete_workflow("_vdiff")

sec("run comparison")
cmp_wf = wf([{"id": "n", "type": "input", "params": {"key": "n"}},
             {"id": "double", "type": "python", "params": {"expr": "n * 2"}},
             {"id": "out", "type": "output", "params": {"value": "{{double}}"}}], name="_cmp")
ra = run_workflow(cmp_wf, {"n": 5}); store.add_run({**ra, "trigger": "cmp"})
rb = run_workflow(cmp_wf, {"n": 9}); store.add_run({**rb, "trigger": "cmp"})
d = _b.compare_runs(ra["run_id"], rb["run_id"])
check("same workflow detected", d["same_workflow"])
check("differing node found", "double" in d["changed_nodes"], d["changed_nodes"])
# a node whose output is genuinely identical must NOT be listed as changed
same_wf = wf([{"id": "fixed", "type": "template", "params": {"text": "constant"}},
              {"id": "v", "type": "input", "params": {"key": "v"}}], name="_cmp_same")
sa = run_workflow(same_wf, {"v": 1}); store.add_run({**sa, "trigger": "cmp"})
sb = run_workflow(same_wf, {"v": 2}); store.add_run({**sb, "trigger": "cmp"})
dsame = _b.compare_runs(sa["run_id"], sb["run_id"])
check("unchanged node excluded", "fixed" not in dsame["changed_nodes"], dsame["changed_nodes"])
check("changed node included", "v" in dsame["changed_nodes"], dsame["changed_nodes"])
check("outputs captured", any(n["node"] == "double" and n["output_a"] == 10
                              and n["output_b"] == 18 for n in d["nodes"]))
check("summary has deltas", any(s["field"] == "tokens_in" and s["delta"] is not None
                                for s in d["summary"]))
d2 = _b.compare_runs(ra["run_id"], ra["run_id"])
check("self-compare is identical", d2["identical"], d2["changed_nodes"])
rc = run_workflow(wf([{"id": "z", "type": "template", "params": {"text": "other"}}],
                     name="_cmp_other"), {})
store.add_run({**rc, "trigger": "cmp"})
d3 = _b.compare_runs(ra["run_id"], rc["run_id"])
check("cross-workflow flagged", not d3["same_workflow"])
check("nodes only in one side marked",
      any(n["in_a"] and not n["in_b"] for n in d3["nodes"]))
try:
    _b.compare_runs("nope", ra["run_id"])
    check("missing run errors", False)
except _b.BundleError:
    check("missing run errors", True)
big = run_workflow(wf([{"id": "big", "type": "python",
                        "params": {"expr": "'x' * 5000"}}], name="_cmp_big"), {})
store.add_run({**big, "trigger": "cmp"})
d4 = _b.compare_runs(big["run_id"], big["run_id"])
check("large outputs truncated",
      len(str(d4["nodes"][0]["output_a"])) < 600, len(str(d4["nodes"][0]["output_a"])))

sec("streaming events")
events = []
run_workflow(wf([{"id": "a", "type": "template", "params": {"text": "1"}},
                 {"id": "b", "type": "template", "params": {"text": "2"}}]), {},
             on_event=events.append)
kinds = [e["event"] for e in events]
check("emits lifecycle", kinds[0] == "run_start" and kinds[-1] == "run_end")
check("emits per node", kinds.count("node_start") == 2 and kinds.count("node_end") == 2)

sec("cron expressions")
import cron as _cron
from datetime import datetime as _dt
check("parses 5 fields", len(_cron.parse("* * * * *")) == 5)
check("step values", sorted(_cron.parse("*/15 * * * *")["minute"]) == [0, 15, 30, 45])
check("ranges", sorted(_cron.parse("1-5 * * * *")["minute"]) == [1, 2, 3, 4, 5])
check("lists", sorted(_cron.parse("1,3,5 * * * *")["minute"]) == [1, 3, 5])
check("range with step", sorted(_cron.parse("10-30/10 * * * *")["minute"]) == [10, 20, 30])
check("month names", _cron.parse("0 0 1 jan *")["month"] == {1})
check("weekday names", _cron.parse("0 0 * * mon-fri")["weekday"] == {1, 2, 3, 4, 5})
check("sunday is 0 and 7", _cron.parse("0 0 * * 7")["weekday"] == {0})
for sh in ("@hourly", "@daily", "@weekly", "@monthly", "@yearly", "@midnight"):
    check(f"shorthand {sh}", len(_cron.parse(sh)) == 5)

friday_9am = _dt(2026, 8, 28, 9, 0)
check("matches exact time", _cron.matches("0 9 * * *", friday_9am))
check("matches weekday name", _cron.matches("0 9 * * fri", friday_9am))
check("rejects wrong weekday", not _cron.matches("0 9 * * mon", friday_9am))
check("rejects wrong hour", not _cron.matches("0 10 * * *", friday_9am))
check("step matches", _cron.matches("*/15 * * * *", friday_9am))
check("dom OR dow when both set",
      _cron.matches("0 9 28 * mon", friday_9am))   # 28th matches even though Mon doesn't

for bad, why in [("", "empty"), ("* * * *", "4 fields"), ("* * * * * *", "6 fields"),
                 ("60 * * * *", "minute 60"), ("* 24 * * *", "hour 24"),
                 ("* * 32 * *", "day 32"), ("* * * 13 *", "month 13"),
                 ("* * * * 9", "weekday 9"), ("abc * * * *", "not a number"),
                 ("0 0 L * *", "L unsupported"), ("0 0 * * 1#2", "# unsupported"),
                 ("*/0 * * * *", "zero step"), ("5-1 * * * *", "backwards range"),
                 ("@weird", "unknown shorthand")]:
    try:
        _cron.parse(bad)
        check(f"rejects {why}", False, "accepted")
    except _cron.CronError:
        check(f"rejects {why}", True)

base = _dt(2026, 8, 28, 9, 30).timestamp()
n1 = _cron.next_run("0 * * * *", base)
check("next_run is in the future", n1 > base)
check("next_run lands on the hour", _dt.fromtimestamp(n1).minute == 0)
check("next_run is strictly after", _cron.next_run("*/30 * * * *", n1) > n1)
seq = _cron.preview("0 0 * * *", 3)
check("preview returns n entries", len(seq) == 3)
check("daily preview is 24h apart",
      abs((_cron.next_run("0 0 * * *", _cron.next_run("0 0 * * *", base))
           - _cron.next_run("0 0 * * *", base)) - 86400) < 2)
check("describe is human", "09:00" in _cron.describe("0 9 * * *"))
check("describe names weekdays", "Mon" in _cron.describe("0 9 * * mon"))
check("describe handles shorthand", "hour" in _cron.describe("@hourly"))

sec("audit log")
before = len(store.list_audit(500))
store.audit("tester", "workflow.save", "wf-a", {"version": 2}, ["write"], "1.2.3.4")
store.audit("tester", "workflow.delete", "wf-b", None, ["write"], "1.2.3.4")
store.audit("other", "key.create", "k1", {"scopes": ["run"]}, ["admin"], "5.6.7.8")
rows = store.list_audit(500)
check("entries appended", len(rows) == before + 3, len(rows) - before)
latest = rows[0]
check("newest first", latest["action"] == "key.create", latest["action"])
check("actor recorded", latest["actor"] == "other")
check("scopes recorded", latest["scopes"] == ["admin"])
check("ip recorded", latest["ip"] == "5.6.7.8")
check("detail round-trips", latest["detail"] == {"scopes": ["run"]})
check("human timestamp", len(latest["when"]) == 19)
check("filter by action",
      all(r["action"] == "workflow.save" for r in store.list_audit(50, action="workflow.save")))
check("filter by actor",
      all(r["actor"] == "tester" for r in store.list_audit(50, actor="tester")))
check("filter by target",
      all(r["target"] == "wf-a" for r in store.list_audit(50, target="wf-a")))
check("limit respected", len(store.list_audit(2)) == 2)
check("offset pages", store.list_audit(1, offset=1)[0]["id"] != store.list_audit(1)[0]["id"])
summ = store.audit_summary()
check("summary counts", summ["total"] >= 3)
check("summary groups by action", any(a["action"] == "workflow.save" for a in summ["by_action"]))
check("summary groups by actor", any(a["actor"] == "tester" for a in summ["by_actor"]))
store.audit("t", "big", "x", {"blob": "y" * 5000})
check("oversized detail truncated, not dropped", store.list_audit(1)[0]["action"] == "big")
try:
    store.audit(None, "weird", None, object())
    check("audit never raises", True)
except Exception:
    check("audit never raises", False)

sec("node timeout")
from engine import NodeTimeout, call_with_timeout
try:
    call_with_timeout(lambda: time.sleep(3), 0.15)
    check("timeout fires", False, "no timeout")
except NodeTimeout:
    check("timeout fires", True)
check("fast call returns", call_with_timeout(lambda: 42, 5) == 42)
check("zero means unlimited", call_with_timeout(lambda: 7, 0) == 7)
try:
    call_with_timeout(lambda: 1 / 0, 5)
    check("inner errors propagate", False)
except ZeroDivisionError:
    check("inner errors propagate", True)
_t0 = time.time()
try:
    call_with_timeout(lambda: time.sleep(10), 0.2)
except NodeTimeout:
    pass
check("returns promptly on timeout", time.time() - _t0 < 1.0, time.time() - _t0)

os.environ["AIFLOW_MOCK_STREAM_DELAY"] = "0.04"
slow_prompt = "Summarize: " + " ".join(["word"] * 40)
r = run_workflow(wf([{"id": "s", "type": "llm", "timeout": 0.1,
                      "params": {"prompt": slow_prompt}}]), {}, on_event=lambda e: None)
check("node timeout halts run", r["status"] == "error" and "NodeTimeout" in r["error"])
check("timed_out flag recorded", r["logs"][0].get("timed_out") is True)
r = run_workflow(wf([{"id": "s", "type": "llm", "timeout": 0.08, "retries": 2,
                      "params": {"prompt": slow_prompt}}]), {}, on_event=lambda e: None)
check("timeouts are retried", r["logs"][0]["attempts"] == 3, r["logs"][0]["attempts"])
r = run_workflow(wf([{"id": "s", "type": "llm", "timeout": 0.08, "on_error": "fallback",
                      "fallback": "SAFE", "params": {"prompt": slow_prompt}}]),
                 {}, on_event=lambda e: None)
check("fallback covers timeout", r["context"]["s"] == "SAFE" and r["status"] == "partial")
os.environ["AIFLOW_MOCK_STREAM_DELAY"] = "0"
r = run_workflow(wf([{"id": "s", "type": "llm", "timeout": 30,
                      "params": {"prompt": "quick"}}]), {})
check("generous timeout passes", r["status"] == "success")

sec("parallel execution")
par_nodes = [{"id": "src", "type": "input", "params": {"key": "x"}}]
for _i in range(4):
    par_nodes.append({"id": f"br{_i}", "type": "python",
                      "params": {"expr": f"str(src) + '-{_i}'"}})
par_nodes.append({"id": "join", "type": "output", "params": {
    "value": {f"br{_i}": f"{{{{br{_i}}}}}" for _i in range(4)}}})
ser = run_workflow(wf(par_nodes, name="_par"), {"x": "d"})
par = run_workflow(wf(par_nodes, name="_par"), {"x": "d"}, parallel=4)
check("parallel matches serial output", ser["outputs"] == par["outputs"])
check("parallel logs every node", len(par["logs"]) == len(ser["logs"]))
check("parallel usage matches", par["usage"]["llm_calls"] == ser["usage"]["llm_calls"])
chain = wf([{"id": "c", "type": "python", "params": {"expr": "b + 1"}},
            {"id": "a", "type": "input", "params": {"key": "a"}},
            {"id": "b", "type": "python", "params": {"expr": "a * 2"}}], name="_parchain")
check("dependencies still ordered",
      run_workflow(chain, {"a": 5}, parallel=8)["context"]["c"] == 11)
r = run_workflow(wf([{"id": "ok", "type": "template", "params": {"text": "fine"}},
                     {"id": "bad", "type": "python", "params": {"expr": "1/0"}}]),
                 {}, parallel=4)
check("error halts parallel run", r["status"] == "error")
r = run_workflow(wf([{"id": "bad", "type": "python", "params": {"expr": "1/0"},
                      "on_error": "continue"},
                     {"id": "ok", "type": "template", "params": {"text": "fine"}}]),
                 {}, parallel=4)
check("policies work in parallel", r["status"] == "partial" and r["context"]["ok"] == "fine")
r = run_workflow(wf([{"id": f"n{_i}", "type": "llm", "params": {"prompt": f"p{_i}"}}
                     for _i in range(6)]), {}, parallel=4, budget={"max_llm_calls": 2})
check("budget enforced in parallel", r["status"] == "budget_exceeded")
r = run_workflow(wf([{"id": "v", "type": "input", "params": {"key": "v"}},
                     {"id": "hi", "type": "template", "params": {"text": "H"}, "when": "v > 5"},
                     {"id": "lo", "type": "template", "params": {"text": "L"}, "when": "v <= 5"},
                     {"id": "tail", "type": "template", "params": {"text": "{{hi}}"}}]),
                 {"v": 1}, parallel=4)
check("skips propagate in parallel", set(r["skipped"]) == {"hi", "tail"}, r["skipped"])
r = run_workflow(wf([{"id": "t", "type": "template", "params": {"text": "d"}},
                     {"id": "g", "type": "approval", "params": {"prompt": "ok?"}},
                     {"id": "after", "type": "template", "params": {"text": "x"}}],
                    name="_parappr"), {}, parallel=4)
check("approval pauses in parallel", r["status"] == "paused" and "after" not in r["context"])
store.cache_clear()
cw = wf([{"id": "c1", "type": "llm", "cache": True, "params": {"prompt": "Summarize: par cache"}}],
        name="_parcache")
run_workflow(cw, {}, parallel=4)
check("cache works in parallel",
      run_workflow(cw, {}, parallel=4)["logs"][0]["status"] == "cached")
store.cache_clear()
for w_ in store.list_workflows():
    if w_["name"].startswith("_par"):
        store.delete_workflow(w_["name"])

sec("starter templates")
import templates as _tpl
check("templates defined", len(_tpl.TEMPLATES) >= 8, len(_tpl.TEMPLATES))
check("ids unique", len({t["id"] for t in _tpl.TEMPLATES}) == len(_tpl.TEMPLATES))
listing = _tpl.listing()
check("listing has metadata",
      all({"id", "title", "category", "blurb", "nodes", "types", "sample"} <= set(t)
          for t in listing))
check("listing omits node bodies", all("workflow" not in t for t in listing))
_seen_types = set()
for t in _tpl.TEMPLATES:
    _seen_types |= {n["type"] for n in t["workflow"]["nodes"]}
check("templates cover many node types", len(_seen_types) >= 12, len(_seen_types))

for t in _tpl.TEMPLATES:
    doc = {"name": "_t_" + t["id"], **t["workflow"]}
    nodes = [Node.from_dict(n) for n in doc["nodes"]]
    ids = [n.id for n in nodes]
    ok_ids = len(ids) == len(set(ids))
    try:
        topo_order(nodes)
        ok_dag = True
    except Exception:
        ok_dag = False
    bad_type = [n.type for n in nodes if n.type not in HANDLERS]
    check(f"{t['id']}: structure valid", ok_ids and ok_dag and not bad_type,
          bad_type or "dup ids/cycle")
    r = run_workflow(Workflow.from_dict(doc), t.get("sample", {}))
    check(f"{t['id']}: runs from its sample", r["status"] in ("success", "paused"),
          [l["error"] for l in r["logs"] if l.get("error")][:1])
    outs = [n for n in doc["nodes"] if n["type"] == "output"]
    check(f"{t['id']}: produces output", bool(outs) and (r["outputs"] or r["status"] == "paused"))

for n in [w["name"] for w in store.list_workflows() if w["name"].startswith("_inst")]:
    store.delete_workflow(n)
inst = _tpl.instantiate("summarize", "_inst_demo")
check("instantiate saves", store.get_workflow("_inst_demo") is not None)
check("instantiate returns sample", "document" in inst["sample"])
check("instantiated is versioned", inst["workflow"]["version"] == 1)
again = _tpl.instantiate("summarize", "_inst_demo")
check("name collision avoided", again["workflow"]["name"] == "_inst_demo-2",
      again["workflow"]["name"])
r = run_workflow(Workflow.from_dict(store.get_workflow("_inst_demo")), inst["sample"])
check("instantiated copy runs", r["status"] == "success")
try:
    _tpl.instantiate("does-not-exist")
    check("unknown template raises", False)
except KeyError:
    check("unknown template raises", True)
for n in ("_inst_demo", "_inst_demo-2"):
    store.delete_workflow(n)

sec("node cache")
store.cache_clear()
cw = wf([{"id": "hot", "type": "llm", "params": {"prompt": "Summarize: cache me"},
          "cache": True},
         {"id": "cold", "type": "llm", "params": {"prompt": "Summarize: never cached"}}],
        name="_cache_wf")
c1 = run_workflow(cw, {})
check("first run executes", c1["logs"][0]["status"] == "success")
c2 = run_workflow(cw, {})
check("second run hits cache", c2["logs"][0]["status"] == "cached", c2["logs"][0]["status"])
check("uncached node still runs", c2["logs"][1]["status"] == "success")
check("cached output identical", c1["context"]["hot"] == c2["context"]["hot"])
check("cache cuts llm calls", c2["usage"]["llm_calls"] < c1["usage"]["llm_calls"])
check("cache hit costs nothing", c2["logs"][0]["usage"]["cost_usd"] == 0.0)
check("hit count tracked", c2["logs"][0].get("cache_hits", 0) >= 1)
c3 = run_workflow(cw, {}, no_cache=True)
check("no_cache bypasses", c3["logs"][0]["status"] == "success")
cw2 = wf([{"id": "hot", "type": "llm", "params": {"prompt": "Summarize: DIFFERENT"},
           "cache": True}], name="_cache_wf")
check("changed params miss", run_workflow(cw2, {})["logs"][0]["status"] == "success")
cw3 = wf([{"id": "hot", "type": "llm", "params": {"prompt": "Summarize: cache me"},
           "cache": True}], name="_cache_other")
check("different workflow is a separate key",
      run_workflow(cw3, {})["logs"][0]["status"] == "success")
ttlw = wf([{"id": "t", "type": "llm", "params": {"prompt": "ttl test"},
            "cache": {"ttl": 100}}], name="_cache_ttl")
run_workflow(ttlw, {})
check("ttl cache hits inside window", run_workflow(ttlw, {})["logs"][0]["status"] == "cached")
expired = wf([{"id": "t", "type": "llm", "params": {"prompt": "ttl gone"},
               "cache": {"ttl": 0.001}}], name="_cache_exp")
run_workflow(expired, {})
time.sleep(0.05)
check("expired ttl misses", run_workflow(expired, {})["logs"][0]["status"] == "success")
allw = wf([{"id": "a", "type": "llm", "params": {"prompt": "cache-all mode"}}],
          name="_cache_all")
run_workflow(allw, {}, cache_all=True)
check("cache_all opts every node in",
      run_workflow(allw, {}, cache_all=True)["logs"][0]["status"] == "cached")
st = store.cache_stats()
check("cache stats populated", st["entries"] > 0 and st["total_hits"] > 0)
check("clear removes entries", store.cache_clear() > 0 and store.cache_stats()["entries"] == 0)

sec("retry from failed node")
rw = wf([{"id": "costly", "type": "llm", "params": {"prompt": "Summarize: expensive step"}},
         {"id": "gate", "type": "input", "params": {"key": "needed", "required": True}},
         {"id": "tail", "type": "template", "params": {"text": "ok {{gate}}"}}],
        name="_retry_wf")
fail = run_workflow(rw, {})
check("run fails at the gate", fail["status"] == "error")
failed_node = next(l["node"] for l in fail["logs"] if l["status"] == "error")
check("failing node identified", failed_node == "gate")
keep = [l for l in fail["logs"] if l["status"] == "success"]
resumed = run_workflow(rw, {"needed": "value"},
                       resume={"context": {l["node"]: fail["context"][l["node"]] for l in keep},
                               "logs": keep})
check("retry completes", resumed["status"] == "success")
check("prior work reused", resumed["context"]["costly"] == fail["context"]["costly"])
check("retry spends nothing on reused nodes", resumed["usage"]["llm_calls"] == 0,
      resumed["usage"])
check("remaining nodes ran", resumed["context"]["tail"] == "ok value")

sec("streaming LLM output")
import os as _os
_os.environ["AIFLOW_MOCK_STREAM_DELAY"] = "0"
ev = []
r = run_workflow(wf([{"id": "s", "type": "llm",
                      "params": {"prompt": "Summarize: the quick brown fox jumps over it"}}]),
                 {}, on_event=ev.append)
toks = [e for e in ev if e["event"] == "token"]
check("tokens emitted", len(toks) > 1, len(toks))
check("tokens reassemble to output", "".join(t["text"] for t in toks) == r["context"]["s"])
check("token seq is ordered", [t["seq"] for t in toks] == list(range(1, len(toks) + 1)))
check("tokens carry node id", all(t["node"] == "s" for t in toks))
check("usage marked streamed", r["logs"][0]["usage"]["tokens_out"] > 0)
ev2 = []
run_workflow(wf([{"id": "s", "type": "llm", "params": {"prompt": "hi", "stream": False}}]),
             {}, on_event=ev2.append)
check("node can opt out", not [e for e in ev2 if e["event"] == "token"])
ev3 = []
run_workflow(wf([{"id": "s", "type": "llm", "params": {"prompt": "hi"}}]),
             {}, on_event=ev3.append, stream=False)
check("run can opt out", not [e for e in ev3 if e["event"] == "token"])
r = run_workflow(wf([{"id": "s", "type": "llm", "params": {"prompt": "hi"}}]), {})
check("no listener = no streaming overhead", r["status"] == "success")
a = run_workflow(wf([{"id": "s", "type": "llm", "params": {"prompt": "Summarize: same input"}}]),
                 {}, on_event=[].append)
b = run_workflow(wf([{"id": "s", "type": "llm", "params": {"prompt": "Summarize: same input"}}]), {})
check("streamed == non-streamed text", a["context"]["s"] == b["context"]["s"])
_p = P_get = __import__("providers").get_provider("mock")
seen = []
t, u = _p.stream(on_token=seen.append, prompt="Classify this", json_mode=True)
check("json_mode still valid when streamed", isinstance(json.loads(t), dict))
base = __import__("providers").BaseProvider()
check("non-streaming providers advertise it", base.supports_streaming is False)

sec("web search tool")
import tools as _tools
_os.environ.pop("TAVILY_API_KEY", None)
_os.environ.pop("SERPER_API_KEY", None)
_os.environ.pop("BRAVE_API_KEY", None)
_os.environ.pop("AIFLOW_SEARCH_DDG", None)
check("defaults to offline stub", _tools.search_backend() == "mock")
out = _tools.t_search("llm workflow automation")
check("stub returns text", len(out) > 50)
check("stub is deterministic", out == _tools.t_search("llm workflow automation"))
check("empty query handled", _tools.t_search("") == "empty query")
_os.environ["TAVILY_API_KEY"] = "definitely-not-a-real-key"
check("key selects backend", _tools.search_backend() == "tavily")
degraded = _tools.t_search("anything")
check("bad key degrades, never raises",
      "failed" in degraded or "falling back" in degraded or len(degraded) > 20)
_os.environ.pop("TAVILY_API_KEY")
check("back to stub after unset", _tools.search_backend() == "mock")
check("formatter renders results",
      "[1]" in _tools._fmt([{"title": "T", "snippet": "S", "url": "http://x"}]))
check("formatter handles empty", _tools._fmt([]) == "no results found")
check("formatter caps count",
      _tools._fmt([{"title": f"t{i}", "snippet": "s"} for i in range(20)]).count("[") <= 5)
check("search reachable from agent",
      "search" in _tools.TOOLS and callable(_tools.TOOLS["search"]["fn"]))
r = run_workflow(wf([{"id": "a", "type": "agent", "params": {
    "goal": "assess automation", "max_steps": 3, "tools": ["search", "finish"]}}]), {})
check("agent still runs with search", r["status"] == "success")

sec("providers")
a = run_workflow(wf([{"id": "l", "type": "llm", "params": {"prompt": "Summarize: hi."}}]), {})
b = run_workflow(wf([{"id": "l", "type": "llm", "params": {"prompt": "Summarize: hi."}}]), {})
check("mock deterministic", a["context"]["l"] == b["context"]["l"])
check("json_mode valid json", isinstance(json.loads(run_workflow(wf([
    {"id": "j", "type": "llm", "params": {"prompt": "Classify x", "json_mode": True}}]),
    {})["context"]["j"]), dict))
check("http stubbed in mock", run_workflow(wf([{"id": "h", "type": "http", "params": {
    "url": "https://example.com"}}]), {}, "mock")["context"]["h"]["mock"] is True)
import providers as P
check("cost math", P.price("gpt-4o-mini", 1_000_000, 0) == 0.15)
check("auto provider falls back to mock",
      run_workflow(wf([{"id": "l", "type": "llm", "params": {"prompt": "hi"}}]),
                   {}, "auto")["status"] == "success")

sec("persistence (sqlite)")
store.save_workflow({"name": "_ver_test", "description": "v1", "nodes": []})
store.save_workflow({"name": "_ver_test", "description": "v2", "nodes": []})
check("version increments", store.get_workflow("_ver_test")["version"] == 2)
check("versions listed", len(store.list_versions("_ver_test")) == 2)
store.rollback("_ver_test", 1)
check("rollback restores", store.get_workflow("_ver_test")["description"] == "v1")
store.delete_workflow("_ver_test")
r = run_workflow(wf([{"id": "l", "type": "llm", "params": {"prompt": "hi"}}], name="_stat"), {})
store.add_run({**r, "trigger": "test"})
check("run persisted", store.get_run(r["run_id"])["run_id"] == r["run_id"])
check("runs filterable", all(x["workflow"] == "_stat"
                             for x in store.list_runs(10, workflow="_stat")))
s = store.stats()
check("stats computed", s["total_runs"] > 0 and "p95_ms" in s and "per_workflow" in s)
k = store.create_key("test-key", ["run"])
check("api key roundtrip", store.get_key(k["key"])["scopes"] == ["run"])
store.revoke_key(k["key"])
check("api key revoked", store.get_key(k["key"]) is None)
store.add_schedule({"id": "_s1", "workflow": "support-triage", "every_seconds": 30,
                    "payload": {}, "provider": "mock", "next_run": time.time() + 30})
check("schedule persisted", any(x["id"] == "_s1" for x in store.list_schedules()))
store.del_schedule("_s1")
check("schedule deleted", not any(x["id"] == "_s1" for x in store.list_schedules()))

sec("seeded workflows")
payload = {"ticket": "URGENT: double charge and a 500 error", "customer": "Priya",
           "document": "Revenue grew 18%. Ship SSO by Nov 15. Hire two SREs.",
           "topic": "llm automation", "tone": "practical",
           "reviews": ["Terrible, crashed twice", "Love it", "Billing broken", "Fine"],
           "knowledge": "AIFlow runs DAG workflows. Retries use exponential backoff. "
                        "Approval nodes pause a run until a human decides.",
           "question": "What do approval nodes do?",
           "goal": "evaluate llm automation",
           "message": "URGENT: production is down with 500 errors",
           "text": "I was billed twice and the export is broken"}
for w in store.list_workflows():
    r = run_workflow(Workflow.from_dict(w), payload)
    expected = ("success", "paused")
    check(f"{w['name']} runs", r["status"] in expected,
          [l["error"] for l in r["logs"] if l["error"]])

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)

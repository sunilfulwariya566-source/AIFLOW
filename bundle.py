"""Workflow bundle import / export.

A bundle is a portable JSON document holding one or more workflows plus the
sub-workflows they depend on, so it can be dropped into another AIFlow instance.

    {
      "aiflow_bundle": 1,
      "exported_at": "...",
      "workflows": [ {name, description, nodes, on_error, budget, layout}, ... ]
    }

Import modes:
    skip      keep the existing workflow, ignore the incoming one
    rename    import as "<name>-imported", "<name>-imported-2", ...
    overwrite replace it (a new version is created, so rollback still works)
"""
from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Set, Tuple

import store

BUNDLE_VERSION = 1
FIELDS = ("name", "description", "nodes", "on_error", "budget", "layout")


class BundleError(Exception):
    pass


# --------------------------------------------------------------------------- #
# export
# --------------------------------------------------------------------------- #
def sub_workflow_names(nodes: List[Dict[str, Any]]) -> Set[str]:
    """Names referenced by `workflow` nodes inside a node list."""
    out: Set[str] = set()
    for n in nodes or []:
        if n.get("type") == "workflow":
            sub = (n.get("params") or {}).get("workflow")
            if sub:
                out.add(sub)
        # map nodes can nest a workflow step
        step = (n.get("params") or {}).get("step")
        if isinstance(step, dict):
            out |= sub_workflow_names([step | {"id": "_step"}])
    return out


def collect(names: List[str], include_deps: bool = True) -> Tuple[List[Dict], List[str]]:
    """Resolve workflows plus their sub-workflow closure. Returns (docs, missing)."""
    seen: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []
    queue = list(names)
    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        doc = store.get_workflow(name)
        if not doc:
            missing.append(name)
            continue
        seen[name] = {k: doc.get(k) for k in FIELDS}
        if include_deps:
            for sub in sub_workflow_names(doc.get("nodes")):
                if sub not in seen:
                    queue.append(sub)
    # dependencies first so an import never references a missing workflow
    ordered = _dependency_order(seen)
    return ordered, missing


def _dependency_order(docs: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    placed: Set[str] = set()
    remaining = dict(docs)
    while remaining:
        progressed = False
        for name in sorted(remaining):
            deps = sub_workflow_names(remaining[name].get("nodes")) & set(remaining)
            if deps <= placed:
                out.append(remaining.pop(name))
                placed.add(name)
                progressed = True
                break
        if not progressed:            # cycle: emit the rest as-is
            out.extend(remaining.values())
            break
    return out


def export_bundle(names: List[str] = None, include_deps: bool = True) -> Dict[str, Any]:
    if not names:
        names = [w["name"] for w in store.list_workflows()]
    docs, missing = collect(names, include_deps)
    return {
        "aiflow_bundle": BUNDLE_VERSION,
        "exported_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "workflows": docs,
        "missing": missing,
        "count": len(docs),
    }


# --------------------------------------------------------------------------- #
# import
# --------------------------------------------------------------------------- #
def validate_bundle(doc: Any) -> List[Dict[str, Any]]:
    """Structural checks. Raises BundleError, returns the workflow list."""
    if isinstance(doc, str):
        try:
            doc = json.loads(doc)
        except Exception as e:
            raise BundleError(f"not valid JSON: {e}")
    if not isinstance(doc, dict):
        raise BundleError("bundle must be a JSON object")
    # tolerate a bare workflow or a bare list for convenience
    if "aiflow_bundle" not in doc:
        if "nodes" in doc and "name" in doc:
            doc = {"aiflow_bundle": BUNDLE_VERSION, "workflows": [doc]}
        else:
            raise BundleError("missing 'aiflow_bundle' marker")
    if int(doc.get("aiflow_bundle", 0)) > BUNDLE_VERSION:
        raise BundleError(
            f"bundle version {doc['aiflow_bundle']} is newer than supported ({BUNDLE_VERSION})")
    wfs = doc.get("workflows")
    if not isinstance(wfs, list) or not wfs:
        raise BundleError("bundle contains no workflows")
    for i, w in enumerate(wfs):
        if not isinstance(w, dict):
            raise BundleError(f"workflow #{i} is not an object")
        if not w.get("name"):
            raise BundleError(f"workflow #{i} has no name")
        if not isinstance(w.get("nodes"), list):
            raise BundleError(f"workflow '{w['name']}' has no node list")
        ids = [n.get("id") for n in w["nodes"]]
        if len(ids) != len(set(ids)):
            raise BundleError(f"workflow '{w['name']}' has duplicate node ids")
    return wfs


def _free_name(base: str) -> str:
    cand = f"{base}-imported"
    i = 1
    while store.get_workflow(cand):
        i += 1
        cand = f"{base}-imported-{i}"
    return cand


def import_bundle(doc: Any, mode: str = "rename", dry_run: bool = False) -> Dict[str, Any]:
    if mode not in ("skip", "rename", "overwrite"):
        raise BundleError("mode must be skip, rename or overwrite")
    wfs = validate_bundle(doc)

    results: List[Dict[str, Any]] = []
    renames: Dict[str, str] = {}

    for w in wfs:
        original = w["name"]
        exists = store.get_workflow(original) is not None
        action, target = "created", original

        if exists:
            if mode == "skip":
                results.append({"workflow": original, "action": "skipped",
                                "reason": "already exists"})
                continue
            if mode == "rename":
                target = _free_name(original)
                renames[original] = target
                action = "renamed"
            else:
                action = "overwritten"

        results.append({"workflow": original, "action": action, "saved_as": target})

    if dry_run:
        return {"dry_run": True, "mode": mode, "results": results,
                "would_import": len([r for r in results if r["action"] != "skipped"])}

    # second pass: write, rewiring sub-workflow references to any renamed targets
    imported = 0
    skipped_names = {r["workflow"] for r in results if r["action"] == "skipped"}
    for w in wfs:
        if w["name"] in skipped_names:
            continue
        doc_out = {k: w.get(k) for k in FIELDS}
        doc_out["name"] = renames.get(w["name"], w["name"])
        doc_out["nodes"] = _rewire(w.get("nodes") or [], renames)
        doc_out["description"] = doc_out.get("description") or ""
        doc_out["on_error"] = doc_out.get("on_error") or "stop"
        doc_out["budget"] = doc_out.get("budget") or {}
        doc_out["layout"] = doc_out.get("layout") or {}
        store.save_workflow(doc_out)
        imported += 1

    return {"imported": imported, "mode": mode, "results": results,
            "renames": renames}


def _rewire(nodes: List[Dict[str, Any]], renames: Dict[str, str]) -> List[Dict[str, Any]]:
    """Point `workflow` nodes at renamed imports so bundles stay self-consistent."""
    if not renames:
        return nodes
    out = json.loads(json.dumps(nodes))
    for n in out:
        if n.get("type") == "workflow":
            p = n.get("params") or {}
            if p.get("workflow") in renames:
                p["workflow"] = renames[p["workflow"]]
                n["params"] = p
    return out


# --------------------------------------------------------------------------- #
# workflow version diff
# --------------------------------------------------------------------------- #
NODE_FIELDS = ("type", "params", "depends_on", "retries", "on_error",
               "fallback", "when", "cache", "timeout")


def _node_map(doc: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {n.get("id"): n for n in (doc.get("nodes") or []) if n.get("id")}


def _field_diffs(a: Dict[str, Any], b: Dict[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for f in NODE_FIELDS:
        va, vb = a.get(f), b.get(f)
        if json.dumps(va, sort_keys=True, default=str) != \
           json.dumps(vb, sort_keys=True, default=str):
            out.append({"field": f, "a": _short(va), "b": _short(vb)})
    return out


def diff_versions(name: str, va: int, vb: int) -> Dict[str, Any]:
    """Structural diff between two saved versions of a workflow."""
    da = store.get_version(name, va)
    db = store.get_version(name, vb)
    if not da:
        raise BundleError(f"version {va} of '{name}' not found")
    if not db:
        raise BundleError(f"version {vb} of '{name}' not found")

    na, nb = _node_map(da), _node_map(db)
    added = [n for n in nb if n not in na]
    removed = [n for n in na if n not in nb]
    common = [n for n in nb if n in na]

    changed, unchanged = [], []
    for nid in common:
        fd = _field_diffs(na[nid], nb[nid])
        (changed if fd else unchanged).append(
            {"node": nid, "type": nb[nid].get("type"), "fields": fd})

    meta = []
    for f in ("description", "on_error", "budget", "parallel"):
        x, y = da.get(f), db.get(f)
        if json.dumps(x, sort_keys=True, default=str) != \
           json.dumps(y, sort_keys=True, default=str):
            meta.append({"field": f, "a": _short(x), "b": _short(y)})

    # a reordered node list changes execution order even if nothing else did
    order_a = [n.get("id") for n in (da.get("nodes") or [])]
    order_b = [n.get("id") for n in (db.get("nodes") or [])]
    reordered = (order_a != order_b
                 and sorted(x for x in order_a if x) == sorted(x for x in order_b if x))

    return {
        "workflow": name, "a": va, "b": vb,
        "added": [{"node": n, "type": nb[n].get("type")} for n in added],
        "removed": [{"node": n, "type": na[n].get("type")} for n in removed],
        "changed": changed,
        "unchanged": [u["node"] for u in unchanged],
        "meta": meta,
        "reordered": reordered,
        "order_a": order_a, "order_b": order_b,
        "identical": not (added or removed or changed or meta or reordered),
        "summary": {"added": len(added), "removed": len(removed),
                    "changed": len(changed), "unchanged": len(unchanged)},
    }


# --------------------------------------------------------------------------- #
# run comparison
# --------------------------------------------------------------------------- #
def _short(v: Any, n: int = 400) -> Any:
    if isinstance(v, str):
        return v if len(v) <= n else v[:n] + f"… (+{len(v)-n} chars)"
    if v is None or isinstance(v, (int, float, bool)):
        return v
    s = json.dumps(v, default=str, sort_keys=True)
    return json.loads(s) if len(s) <= n else s[:n] + f"… (+{len(s)-n} chars)"


def compare_runs(id_a: str, id_b: str) -> Dict[str, Any]:
    a, b = store.get_run(id_a), store.get_run(id_b)
    if not a:
        raise BundleError(f"run '{id_a}' not found")
    if not b:
        raise BundleError(f"run '{id_b}' not found")

    ua, ub = a.get("usage") or {}, b.get("usage") or {}
    summary = []
    for label, ka, kb in (("status", a.get("status"), b.get("status")),
                          ("workflow", a.get("workflow"), b.get("workflow")),
                          ("provider", a.get("provider"), b.get("provider")),
                          ("trigger", a.get("trigger"), b.get("trigger")),
                          ("duration_ms", a.get("duration_ms"), b.get("duration_ms")),
                          ("tokens_in", ua.get("tokens_in"), ub.get("tokens_in")),
                          ("tokens_out", ua.get("tokens_out"), ub.get("tokens_out")),
                          ("cost_usd", ua.get("cost_usd"), ub.get("cost_usd")),
                          ("llm_calls", ua.get("llm_calls"), ub.get("llm_calls"))):
        delta = None
        if isinstance(ka, (int, float)) and isinstance(kb, (int, float)):
            delta = round(kb - ka, 6)
        summary.append({"field": label, "a": ka, "b": kb,
                        "same": ka == kb, "delta": delta})

    la = {l["node"]: l for l in (a.get("logs") or [])}
    lb = {l["node"]: l for l in (b.get("logs") or [])}
    order = list(la) + [n for n in lb if n not in la]

    nodes = []
    for nid in order:
        x, y = la.get(nid), lb.get(nid)
        ox = _short(x.get("output") if x else None)
        oy = _short(y.get("output") if y else None)
        nodes.append({
            "node": nid,
            "type": (x or y or {}).get("type"),
            "in_a": x is not None, "in_b": y is not None,
            "status_a": (x or {}).get("status"), "status_b": (y or {}).get("status"),
            "ms_a": (x or {}).get("duration_ms"), "ms_b": (y or {}).get("duration_ms"),
            "cost_a": ((x or {}).get("usage") or {}).get("cost_usd"),
            "cost_b": ((y or {}).get("usage") or {}).get("cost_usd"),
            "output_a": ox, "output_b": oy,
            "output_same": json.dumps(ox, default=str, sort_keys=True)
                           == json.dumps(oy, default=str, sort_keys=True),
            "status_same": (x or {}).get("status") == (y or {}).get("status"),
        })

    changed = [n for n in nodes if not n["output_same"] or not n["status_same"]]
    return {
        "a": {"run_id": id_a, "workflow": a.get("workflow"),
              "finished_at": a.get("finished_at"), "status": a.get("status")},
        "b": {"run_id": id_b, "workflow": b.get("workflow"),
              "finished_at": b.get("finished_at"), "status": b.get("status")},
        "same_workflow": a.get("workflow") == b.get("workflow"),
        "summary": summary,
        "nodes": nodes,
        "changed_nodes": [n["node"] for n in changed],
        "identical": not changed and all(s["same"] for s in summary
                                         if s["field"] not in ("duration_ms",)),
        "skipped_a": a.get("skipped") or [],
        "skipped_b": b.get("skipped") or [],
    }

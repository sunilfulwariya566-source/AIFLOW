"""Alert rule evaluation and delivery.

A rule watches one metric over the last N runs (optionally for one workflow) and
fires when the comparison holds. Cooldown prevents alert storms.

Channels:
  log      — record only (visible in the UI feed)
  webhook  — POST the event JSON to `target`
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from typing import Any, Callable, Dict, List

import store

OPS: Dict[str, Callable[[float, float], bool]] = {
    ">": lambda a, b: a > b,
    ">=": lambda a, b: a >= b,
    "<": lambda a, b: a < b,
    "<=": lambda a, b: a <= b,
    "==": lambda a, b: a == b,
}

_lock = threading.Lock()
_subscribers: List[Callable[[Dict[str, Any]], None]] = []


def subscribe(fn: Callable[[Dict[str, Any]], None]) -> None:
    """Let the app push fired alerts onto the SSE bus."""
    _subscribers.append(fn)


def _deliver(al: Dict[str, Any], event: Dict[str, Any]) -> tuple[bool, str]:
    channel = (al.get("channel") or "log").lower()
    if channel == "log":
        return True, "logged"
    if channel == "webhook":
        target = al.get("target") or ""
        if not target:
            return False, "webhook channel needs a target URL"
        try:
            req = urllib.request.Request(
                target, data=json.dumps(event, default=str).encode(),
                headers={"Content-Type": "application/json",
                         "User-Agent": "AIFlow-Alerts/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:  # noqa: S310
                return True, f"HTTP {r.status}"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
    return False, f"unknown channel '{channel}'"


def evaluate(trigger_workflow: str = None) -> List[Dict[str, Any]]:
    """Check every enabled rule; deliver and record the ones that fire."""
    fired: List[Dict[str, Any]] = []
    now = time.time()

    with _lock:
        for al in store.list_alerts():
            if not al.get("enabled"):
                continue
            # a workflow-scoped rule only re-evaluates when that workflow ran
            if al.get("workflow") and trigger_workflow and al["workflow"] != trigger_workflow:
                continue
            if al.get("last_fired") and now - al["last_fired"] < (al.get("cooldown_s") or 0):
                continue

            metrics = store.alert_metrics(al.get("workflow"), al.get("window_runs") or 20)
            if not metrics.get("runs"):
                continue
            value = float(metrics.get(al["metric"], 0.0))
            op = OPS.get(al.get("op") or ">")
            if not op or not op(value, float(al["threshold"])):
                continue

            scope = al.get("workflow") or "all workflows"
            message = (f"{al['name']}: {al['metric']} is {value} "
                       f"{al['op']} {al['threshold']} over the last "
                       f"{metrics['runs']} runs ({scope})")
            event = {"alert_id": al["id"], "name": al["name"], "workflow": al.get("workflow"),
                     "metric": al["metric"], "value": value, "threshold": al["threshold"],
                     "op": al["op"], "window_runs": metrics["runs"], "message": message,
                     "fired_at": time.strftime("%Y-%m-%d %H:%M:%S")}

            ok, detail = _deliver(al, event)
            store.record_alert_event(al, value, message, ok, detail)
            event["delivered"] = ok
            event["detail"] = detail
            fired.append(event)

            for fn in list(_subscribers):
                try:
                    fn({"event": "alert", **event})
                except Exception:
                    pass
    return fired

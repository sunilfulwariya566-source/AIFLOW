"""Starter templates — ready-made workflow patterns.

Each template is a complete, runnable workflow plus a sample payload, so a user can
create it and hit Run immediately. Templates are data only; instantiating one just
saves it through the normal store path (so it gets a version, layout, the lot).
"""
from __future__ import annotations

from typing import Any, Dict, List

import store

TEMPLATES: List[Dict[str, Any]] = [
    {
        "id": "summarize",
        "title": "Summarize & extract",
        "category": "Text",
        "blurb": "Condense a document, pull structured action items, and bundle both.",
        "icon": "▤",
        "sample": {"document": "Q3 board memo. Revenue grew 18% QoQ to $4.2M. Churn "
                               "fell to 2.1%. Platform must ship SSO by Nov 15. "
                               "Hiring two SREs is approved."},
        "workflow": {
            "description": "Summarize a document and extract action items.",
            "on_error": "stop",
            "nodes": [
                {"id": "doc", "type": "input", "params": {"key": "document", "required": True}},
                {"id": "summary", "type": "llm", "cache": True, "retries": 2,
                 "params": {"system": "Summarize faithfully in 3 sentences.",
                            "prompt": "{{doc}}"}},
                {"id": "actions", "type": "llm", "cache": True,
                 "params": {"system": "Extract action items. Return strict JSON.",
                            "prompt": "{{doc}}", "json_mode": True}},
                {"id": "out", "type": "output", "params": {"value": {
                    "summary": "{{summary}}", "actions": "{{actions}}"}}},
            ],
        },
    },
    {
        "id": "classify-route",
        "title": "Classify & route",
        "category": "Routing",
        "blurb": "Score an inbound message, then run only the branch that matches.",
        "icon": "⑃",
        "sample": {"message": "URGENT: checkout is returning 500s for every customer"},
        "workflow": {
            "description": "Classify a message and take only the matching branch.",
            "on_error": "stop",
            "nodes": [
                {"id": "message", "type": "input", "params": {"key": "message", "required": True}},
                {"id": "raw", "type": "llm", "retries": 2, "cache": True,
                 "params": {"system": "Classify. Return strict JSON with category, "
                                      "priority, sentiment, summary.",
                            "prompt": "{{message}}", "json_mode": True}},
                {"id": "parsed", "type": "python", "params": {"expr": "json.loads(raw)"}},
                {"id": "urgent", "type": "llm", "when": "parsed.priority == 'high'",
                 "params": {"system": "You are an on-call engineer.",
                            "prompt": "One-line incident summary for: {{message}}"}},
                {"id": "routine", "type": "llm", "when": "parsed.priority != 'high'",
                 "params": {"system": "You are a friendly support agent.",
                            "prompt": "Draft a reply to: {{message}}"}},
                {"id": "out", "type": "output", "params": {"value": {
                    "priority": "{{parsed.priority}}", "category": "{{parsed.category}}",
                    "incident": "{{urgent}}", "reply": "{{routine}}"}}},
            ],
        },
    },
    {
        "id": "batch-map",
        "title": "Batch process a list",
        "category": "Scale",
        "blurb": "Fan out over many items in parallel, filter, then aggregate counts.",
        "icon": "⋔",
        "sample": {"items": ["The app crashed twice during checkout.",
                             "Love the new dashboard, much faster.",
                             "Billed twice, no refund button.",
                             "Works fine, nothing special."]},
        "workflow": {
            "description": "Parallel fan-out over a list with filtering and aggregation.",
            "on_error": "stop",
            "nodes": [
                {"id": "items", "type": "input", "params": {"key": "items", "required": True}},
                {"id": "scored", "type": "map", "params": {
                    "over": "{{items}}", "as": "item", "workers": 4,
                    "step": {"type": "llm", "params": {
                        "system": "Return strict JSON with category, priority, sentiment.",
                        "prompt": "Classify: {{item}}", "json_mode": True}}}},
                {"id": "objs", "type": "python", "params": {"expr": "[json.loads(s) for s in scored]"}},
                {"id": "negative", "type": "filter", "params": {
                    "over": "{{objs}}", "as": "item",
                    "condition": "item['sentiment'] == 'negative'"}},
                {"id": "counts", "type": "python", "params": {
                    "expr": "{'total': len(objs), 'negative': len(negative)}"}},
                {"id": "out", "type": "output", "params": {"value": {
                    "counts": "{{counts}}", "negative": "{{negative}}"}}},
            ],
        },
    },
    {
        "id": "rag",
        "title": "Answer over a knowledge base",
        "category": "RAG",
        "blurb": "Chunk and index text, retrieve the relevant passages, answer grounded.",
        "icon": "◈",
        "sample": {"knowledge": "AIFlow runs workflows as a DAG. Map nodes fan out over "
                                "lists using a thread pool. Retries use exponential "
                                "backoff with jitter. Approval nodes pause a run until a "
                                "human decides. Budgets stop a run that spends too much.",
                   "question": "What do approval nodes do?"},
        "workflow": {
            "description": "Retrieval-augmented answering over supplied text.",
            "on_error": "stop",
            "nodes": [
                {"id": "kb", "type": "input", "params": {"key": "knowledge", "required": True}},
                {"id": "question", "type": "input", "params": {"key": "question", "required": True}},
                {"id": "chunks", "type": "chunk", "params": {
                    "text": "{{kb}}", "size": 240, "overlap": 40}},
                {"id": "indexed", "type": "embed", "params": {
                    "collection": "template_kb", "texts": "{{chunks}}", "reset": True}},
                {"id": "hits", "type": "retrieve", "depends_on": ["indexed"],
                 "params": {"collection": "template_kb", "query": "{{question}}", "k": 3}},
                {"id": "answer", "type": "llm", "params": {
                    "system": "Answer ONLY from the context. If unknown, say so.",
                    "prompt": "CONTEXT:\n{{hits.context}}\n\nQUESTION: {{question}}"}},
                {"id": "out", "type": "output", "params": {"value": {
                    "answer": "{{answer}}", "sources": "{{hits.matches}}"}}},
            ],
        },
    },
    {
        "id": "approval",
        "title": "Draft with human approval",
        "category": "Human-in-the-loop",
        "blurb": "Generate content, pause for a human decision, then finish.",
        "icon": "✓",
        "sample": {"topic": "why workflow automation needs cost caps"},
        "workflow": {
            "description": "Draft content and gate publication on human approval.",
            "on_error": "stop",
            "nodes": [
                {"id": "topic", "type": "input", "params": {"key": "topic", "required": True}},
                {"id": "draft", "type": "llm", "params": {
                    "prompt": "Write a short post about {{topic}}."}},
                {"id": "teaser", "type": "llm", "params": {
                    "prompt": "One-line teaser for:\n{{draft}}"}},
                {"id": "signoff", "type": "approval", "params": {
                    "prompt": "Approve publishing this post?", "value": "{{teaser}}"}},
                {"id": "out", "type": "output", "params": {"value": {
                    "draft": "{{draft}}", "teaser": "{{teaser}}",
                    "approved": "{{signoff.approved}}"}}},
            ],
        },
    },
    {
        "id": "agent",
        "title": "Research agent",
        "category": "Agents",
        "blurb": "An autonomous loop that picks tools until it can answer the goal.",
        "icon": "◎",
        "sample": {"goal": "assess whether LLM workflow automation is worth adopting"},
        "workflow": {
            "description": "Tool-using agent loop with a spend ceiling.",
            "on_error": "stop",
            "budget": {"max_llm_calls": 12},
            "nodes": [
                {"id": "goal", "type": "input", "params": {"key": "goal", "required": True}},
                {"id": "run", "type": "agent", "params": {
                    "goal": "{{goal}}", "max_steps": 5,
                    "tools": ["search", "calculator", "kb_lookup", "finish"]}},
                {"id": "out", "type": "output", "params": {"value": {
                    "answer": "{{run.answer}}", "steps": "{{run.steps}}",
                    "trace": "{{run.trace}}"}}},
            ],
        },
    },
    {
        "id": "resilient",
        "title": "Resilient API call",
        "category": "Reliability",
        "blurb": "Retries with backoff, a validation gate, and a safe fallback.",
        "icon": "⛨",
        "sample": {"query": "current status of the billing service"},
        "workflow": {
            "description": "Call a service with retries, validate the shape, fall back safely.",
            "on_error": "stop",
            "nodes": [
                {"id": "query", "type": "input", "params": {"key": "query", "required": True}},
                {"id": "fetch", "type": "http", "retries": 3,
                 "on_error": "fallback", "fallback": {"status": 0, "body": {}},
                 "params": {"url": "https://example.com/api", "method": "GET"}},
                {"id": "analysis", "type": "llm", "retries": 2,
                 "on_error": "fallback", "fallback": "{\"ok\": false}",
                 "params": {"system": "Return strict JSON with ok and note.",
                            "prompt": "Service replied {{fetch.body}} for {{query}}",
                            "json_mode": True}},
                {"id": "check", "type": "validate", "params": {
                    "value": "{{analysis}}", "soft": True,
                    "schema": {"required": ["ok"]}}},
                {"id": "out", "type": "output", "params": {"value": {
                    "healthy": "{{check.valid}}", "detail": "{{analysis}}"}}},
            ],
        },
    },
    {
        "id": "reusable-block",
        "title": "Reusable building block",
        "category": "Composition",
        "blurb": "A small library workflow other workflows can call as one node.",
        "icon": "▣",
        "sample": {"text": "I was billed twice and the export button is broken"},
        "workflow": {
            "description": "Classify any text into JSON — call this from other workflows.",
            "on_error": "stop",
            "nodes": [
                {"id": "text", "type": "input", "params": {"key": "text", "required": True}},
                {"id": "raw", "type": "llm", "retries": 2, "cache": True,
                 "params": {"system": "Classify. Return strict JSON with category, "
                                      "priority, sentiment, summary.",
                            "prompt": "{{text}}", "json_mode": True}},
                {"id": "parsed", "type": "python", "params": {"expr": "json.loads(raw)"}},
                {"id": "out", "type": "output", "params": {"value": "{{parsed}}"}},
            ],
        },
    },
]

BY_ID = {t["id"]: t for t in TEMPLATES}


def listing() -> List[Dict[str, Any]]:
    """Metadata only — the gallery does not need full node lists."""
    return [{k: t[k] for k in ("id", "title", "category", "blurb", "icon", "sample")}
            | {"nodes": len(t["workflow"]["nodes"]),
               "types": sorted({n["type"] for n in t["workflow"]["nodes"]})}
            for t in TEMPLATES]


def free_name(base: str) -> str:
    if not store.get_workflow(base):
        return base
    i = 2
    while store.get_workflow(f"{base}-{i}"):
        i += 1
    return f"{base}-{i}"


def instantiate(template_id: str, name: str = None) -> Dict[str, Any]:
    t = BY_ID.get(template_id)
    if not t:
        raise KeyError(f"unknown template '{template_id}'")
    doc = {"name": free_name(name or template_id), **t["workflow"]}
    saved = store.save_workflow(doc)
    return {"workflow": saved, "sample": t.get("sample", {}),
            "template": template_id}

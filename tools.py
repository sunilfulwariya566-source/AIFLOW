"""Agent tools + a tiny deterministic embedding/vector search (no external deps)."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
from typing import Any, Dict, List

import store

DIM = 512
STOP = {"the","a","an","is","are","was","were","be","been","of","to","in","on","for",
        "and","or","but","with","as","by","at","from","it","its","this","that","these",
        "those","how","what","when","where","who","do","does","did","can","will","would",
        "should","i","you","we","they","he","she","have","has","had","not","no","yes","if"}


# ------------------------------- embeddings -------------------------------- #
def embed(text: str) -> List[float]:
    """Deterministic hashed bag-of-words embedding. Offline, no model needed."""
    vec = [0.0] * DIM
    raw = re.findall(r"[a-z0-9]+", (text or "").lower())
    toks = [t for t in raw if t not in STOP and len(t) > 2] or raw
    for i, t in enumerate(toks):
        h = int(hashlib.md5(t.encode()).hexdigest()[:8], 16)
        vec[h % DIM] += 1.0
        # a light bigram signal so word order matters a bit
        if i:
            hb = int(hashlib.md5((toks[i - 1] + "_" + t).encode()).hexdigest()[:8], 16)
            vec[hb % DIM] += 0.5
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def cosine(a: List[float], b: List[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def chunk_text(text: str, size: int = 240, overlap: int = 40) -> List[str]:
    words = str(text).split()
    if not words:
        return []
    step = max(1, size - overlap)
    out, i = [], 0
    while i < len(words):
        out.append(" ".join(words[i:i + size]))
        if i + size >= len(words):
            break
        i += step
    return out


def index_texts(collection: str, texts: List[str], reset: bool = False,
                meta: Dict[str, Any] = None) -> int:
    if reset:
        store.reset_collection(collection)
    rows = [{"id": uuid.uuid4().hex[:12], "text": t, "vec": embed(t),
             "meta": {**(meta or {}), "pos": i}}
            for i, t in enumerate(texts) if str(t).strip()]
    store.add_docs(collection, rows)
    return len(rows)


def search(collection: str, query: str, k: int = 3) -> List[Dict[str, Any]]:
    qv = embed(query)
    docs = store.all_docs(collection)
    scored = sorted(((cosine(qv, d["vec"]), d) for d in docs),
                    key=lambda x: -x[0])[:max(1, k)]
    return [{"score": round(s, 4), "text": d["text"], "id": d["id"], "meta": d["meta"]}
            for s, d in scored if s > 0]


# --------------------------------- tools ----------------------------------- #
# --------------------------------------------------------------------------- #
# web search: real backends when configured, deterministic stub otherwise
# --------------------------------------------------------------------------- #
SEARCH_TIMEOUT = float(os.environ.get("AIFLOW_SEARCH_TIMEOUT", "10"))


def _http_json(url: str, headers: Dict[str, str] = None, body: Any = None,
               method: str = "GET") -> Any:
    import urllib.request
    req = urllib.request.Request(
        url, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"User-Agent": "AIFlow/2.0", **({"Content-Type": "application/json"}
                                                if body is not None else {}),
                 **(headers or {})})
    with urllib.request.urlopen(req, timeout=SEARCH_TIMEOUT) as r:  # noqa: S310
        return json.loads(r.read().decode("utf-8", "replace"))


def _fmt(results: List[Dict[str, str]], limit: int = 5) -> str:
    if not results:
        return "no results found"
    out = []
    for i, r in enumerate(results[:limit], 1):
        title = (r.get("title") or "").strip()
        snippet = re.sub(r"\s+", " ", r.get("snippet") or "").strip()[:220]
        url = r.get("url") or ""
        out.append(f"[{i}] {title}\n{snippet}" + (f"\n{url}" if url else ""))
    return "\n\n".join(out)


def _search_tavily(q: str, key: str) -> List[Dict[str, str]]:
    d = _http_json("https://api.tavily.com/search", method="POST",
                   body={"api_key": key, "query": q, "max_results": 5,
                         "search_depth": "basic"})
    return [{"title": r.get("title"), "snippet": r.get("content"), "url": r.get("url")}
            for r in d.get("results", [])]


def _search_serper(q: str, key: str) -> List[Dict[str, str]]:
    d = _http_json("https://google.serper.dev/search", method="POST",
                   headers={"X-API-KEY": key}, body={"q": q, "num": 5})
    return [{"title": r.get("title"), "snippet": r.get("snippet"), "url": r.get("link")}
            for r in d.get("organic", [])]


def _search_brave(q: str, key: str) -> List[Dict[str, str]]:
    from urllib.parse import quote
    d = _http_json(f"https://api.search.brave.com/res/v1/web/search?q={quote(q)}&count=5",
                   headers={"X-Subscription-Token": key, "Accept": "application/json"})
    return [{"title": r.get("title"), "snippet": r.get("description"), "url": r.get("url")}
            for r in (d.get("web") or {}).get("results", [])]


def _search_duckduckgo(q: str, _key=None) -> List[Dict[str, str]]:
    """Keyless instant-answer API. Thin coverage, but needs no signup."""
    from urllib.parse import quote
    d = _http_json(f"https://api.duckduckgo.com/?q={quote(q)}&format=json&no_html=1")
    out: List[Dict[str, str]] = []
    if d.get("AbstractText"):
        out.append({"title": d.get("Heading") or q, "snippet": d["AbstractText"],
                    "url": d.get("AbstractURL", "")})
    for topic in (d.get("RelatedTopics") or [])[:6]:
        if isinstance(topic, dict) and topic.get("Text"):
            out.append({"title": (topic.get("Text") or "")[:70],
                        "snippet": topic["Text"], "url": topic.get("FirstURL", "")})
    return out


SEARCH_BACKENDS = [
    ("tavily", "TAVILY_API_KEY", _search_tavily),
    ("serper", "SERPER_API_KEY", _search_serper),
    ("brave", "BRAVE_API_KEY", _search_brave),
    ("duckduckgo", None, _search_duckduckgo),
]


def search_backend() -> str:
    """Which backend a search would use right now."""
    if os.environ.get("AIFLOW_SEARCH", "").lower() == "mock":
        return "mock"
    for name, env, _ in SEARCH_BACKENDS:
        if env is None:
            continue
        if os.environ.get(env):
            return name
    return "duckduckgo" if os.environ.get("AIFLOW_SEARCH_DDG") == "1" else "mock"


def _search_stub(q: str) -> str:
    seed = int(hashlib.sha256(q.encode()).hexdigest()[:6], 16)
    facts = [
        f"Industry reports indicate steady growth in '{q}' over the last three quarters.",
        f"Most teams adopting '{q}' cite reduced manual handling as the primary benefit.",
        f"A 2025 survey found 61% of respondents were piloting '{q}' in production.",
    ]
    return " ".join(facts[seed % 3:] + facts[:seed % 3])[:400]


def t_search(q: str, run=None) -> str:
    """Web search. Uses a real backend when an API key is present, else a stub.

    Configure with TAVILY_API_KEY / SERPER_API_KEY / BRAVE_API_KEY, or set
    AIFLOW_SEARCH_DDG=1 for the keyless DuckDuckGo endpoint. Any failure falls
    back to the deterministic stub so workflows never break offline.
    """
    q = str(q or "").strip()
    if not q:
        return "empty query"
    backend = search_backend()
    if backend == "mock":
        return _search_stub(q)
    fn = dict((n, f) for n, _, f in SEARCH_BACKENDS)[backend]
    key = next((os.environ.get(e) for n, e, _ in SEARCH_BACKENDS if n == backend and e), None)
    try:
        results = fn(q, key) if key else fn(q)
        if not results:
            return f"no results for '{q}' (via {backend})"
        return f"via {backend}:\n" + _fmt(results)
    except Exception as e:  # noqa: BLE001
        return (f"search backend '{backend}' failed ({type(e).__name__}); "
                f"falling back to offline summary.\n" + _search_stub(q))


def t_calculator(expr: str, run=None) -> str:
    from sandbox import safe_eval
    try:
        return str(safe_eval(expr, {}))
    except Exception as e:  # noqa: BLE001
        return f"calculator error: {e}"


def t_kb_lookup(q: str, run=None) -> str:
    hits = search("kb", q, 3)
    if not hits:
        return "knowledge base is empty or nothing matched"
    return " | ".join(f"({h['score']}) {h['text'][:160]}" for h in hits)


def t_finish(answer: str, run=None) -> str:
    return answer


TOOLS = {
    "search": {"fn": t_search, "desc": "search the web for a topic"},
    "calculator": {"fn": t_calculator, "desc": "evaluate a math expression"},
    "kb_lookup": {"fn": t_kb_lookup, "desc": "look up the indexed knowledge base"},
    "finish": {"fn": t_finish, "desc": "return the final answer"},
}

"""LLM providers with token + cost accounting, and cross-provider fallback.

Mock is default (offline, deterministic). Real providers:
  openai     OPENAI_API_KEY   [OPENAI_BASE_URL, OPENAI_MODEL]   (also Ollama/vLLM/Together)
  anthropic  ANTHROPIC_API_KEY [ANTHROPIC_MODEL]
  gemini     GEMINI_API_KEY    [GEMINI_MODEL]
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import textwrap
from typing import Any, Dict, Tuple

# USD per 1M tokens (in, out)
PRICES = {
    "gpt-4o-mini": (0.15, 0.60), "gpt-4o": (2.50, 10.00), "gpt-4.1-mini": (0.40, 1.60),
    "claude-3-5-haiku-latest": (0.80, 4.00), "claude-sonnet-4-latest": (3.00, 15.00),
    "gemini-2.0-flash": (0.10, 0.40), "gemini-1.5-pro": (1.25, 5.00),
    "mock": (0.0, 0.0),
}


def estimate_tokens(text: str) -> int:
    """~4 chars/token heuristic, good enough for budgeting without tiktoken."""
    return max(1, int(len(str(text or "")) / 4)) if text else 0


def price(model: str, tin: int, tout: int) -> float:
    pin, pout = PRICES.get(model, PRICES.get(model.split(":")[0], (0.5, 1.5)))
    return round(tin / 1e6 * pin + tout / 1e6 * pout, 8)


class BaseProvider:
    name = "base"
    supports_streaming = False

    def complete(self, **kw) -> str:
        return self.complete_with_usage(**kw)[0]

    def complete_with_usage(self, **kw) -> Tuple[str, Dict[str, Any]]:
        raise NotImplementedError

    def stream(self, on_token=None, **kw) -> Tuple[str, Dict[str, Any]]:
        """Yield tokens through `on_token`, return the same (text, usage) pair.

        Providers that cannot stream fall back to one final chunk, so callers
        never need to branch on capability.
        """
        text, usage = self.complete_with_usage(**kw)
        if on_token:
            on_token(text)
        usage["streamed"] = False
        return text, usage


class MockProvider(BaseProvider):
    """Deterministic, prompt-aware fake LLM. Runs the whole system offline."""
    name = "mock"
    supports_streaming = True

    def stream(self, on_token=None, **kw):
        import time as _t
        text, usage = self.complete_with_usage(**kw)
        if on_token:
            delay = float(os.environ.get("AIFLOW_MOCK_STREAM_DELAY", "0.012"))
            parts = re.findall(r"\S+\s*", text) or [text]
            for tok in parts:
                on_token(tok)
                if delay:
                    _t.sleep(delay)
        usage["streamed"] = True
        return text, usage

    def complete_with_usage(self, prompt="", system="", model="auto", temperature=0.2,
                            json_mode=False, **extra):
        text = self._gen(prompt, system, model, json_mode, extra)
        tin = estimate_tokens(system) + estimate_tokens(prompt)
        tout = estimate_tokens(text)
        return text, {"provider": "mock", "model": "mock", "tokens_in": tin,
                      "tokens_out": tout, "cost_usd": 0.0}

    def _gen(self, prompt, system, model, json_mode, extra):
        p = (prompt or "").strip()
        low = (system + " " + p).lower()
        seed = int(hashlib.sha256((system + p).encode()).hexdigest()[:8], 16)

        # agent loop: pick a plausible tool, then finish
        if extra.get("agent_tools"):
            step = int(extra.get("agent_step", 0))
            avail = [t for t in extra["agent_tools"] if t != "finish"]
            goal = extra.get("agent_goal", "")
            if step == 0 and avail:
                return json.dumps({"tool": avail[0], "input": goal,
                                   "reason": "gather background information first"})
            if step == 1 and len(avail) > 1:
                return json.dumps({"tool": avail[1 % len(avail)], "input": goal,
                                   "reason": "cross-check against another source"})
            return json.dumps({"tool": "finish",
                               "input": f"Based on the gathered evidence, {goal} is viable: "
                                        "adoption is growing, the main benefit is less manual "
                                        "handling, and roughly 61% of teams are piloting it.",
                               "reason": "enough evidence collected"})

        if json_mode:
            return json.dumps(self._json_answer(p, seed, system), indent=2)

        if any(k in low for k in ("draft", "reply", "respond", "email")):
            return textwrap.dedent(f"""\
                Hi there,

                Thanks for reaching out. Here's what we found regarding your request:
                {re.sub(r'\\s+', ' ', p)[:180]}

                We've logged this and a specialist will follow up within one business day.

                Best regards,
                The Automation Desk""")

        if any(k in low for k in ("classify", "category", "route", "label")):
            return ["billing", "technical", "sales", "feedback"][seed % 4]
        if "sentiment" in low:
            return ["positive", "neutral", "negative"][seed % 3]
        if any(k in low for k in ("summar", "tl;dr", "condense", "teaser")):
            body = re.sub(r"\s+", " ", p)[-600:]
            sents = [s.strip() for s in re.split(r"(?<=[.!?])\s+", body) if s.strip()]
            return "Summary: " + " ".join(sents[:3] or [body[:180]])[:400]
        if "context:" in low and "question:" in low:
            ctx = p.split("CONTEXT:")[-1].split("QUESTION:")[0].strip()
            q = p.split("QUESTION:")[-1].strip()
            first = re.split(r"(?<=[.!?])\s+", ctx.replace("\n---\n", " "))[0][:220]
            return f"Grounded answer to “{q}”: {first}"
        if "?" in p:
            return f"Answer: {' '.join(p.strip('?').split()[-8:])} — (mock response)"
        return f"[mock:{model}] {re.sub(r'\\s+', ' ', p)[:220]}"

    def _json_answer(self, p, seed, system=""):
        neg = any(w in p.lower() for w in
                  ("crash", "broken", "awful", "terrible", "urgent", "double charge",
                   "unacceptable", "never replied", "cannot", "fail", "bug", "500"))
        pos = any(w in p.lower() for w in ("love", "great", "excellent", "faster", "amazing"))
        body = re.sub(r"\s+", " ", p)
        # strip a leading "FROM: ... MESSAGE:" wrapper so the summary reads naturally
        if "MESSAGE:" in body:
            body = body.split("MESSAGE:", 1)[1].strip()
        sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", body) if x.strip()]

        base = {
            "category": ["billing", "technical", "sales", "feedback"][seed % 4],
            "priority": "high" if neg else ("low" if pos else "medium"),
            "sentiment": "negative" if neg else ("positive" if pos else "neutral"),
            "entities": list({w for w in re.findall(r"\b[A-Z][a-z]{2,}\b", p)})[:5],
            "summary": " ".join(sentences[:2])[:200] or body[:160],
        }

        # Anything else the caller asked for in its system prompt gets a plausible
        # value, so workflows with custom schemas still get complete demo output.
        asked = set(re.findall(r"\b([a-z][a-z0-9_]{2,})\b", (system or "").lower()))
        extras = {
            "action_items": [s[:90] for s in sentences[:3]] or ["follow up"],
            "needs_reply": bool(re.search(r"\?|please|kindly|urgent|confirm|bata|batao|poochna",
                                          p, re.I)),
            "title": (sentences[0][:70] if sentences else body[:70]),
            "keywords": [w.lower() for w in re.findall(r"\b[a-z]{5,}\b", body)][:5],
            "language": "hinglish" if re.search(r"\b(hai|nahi|karo|kya|bhai|kal)\b", body, re.I) else "en",
            "topics": [w.lower() for w in re.findall(r"\b[a-z]{6,}\b", body)][:3],
            "confidence": round(0.7 + (seed % 30) / 100, 2),
            "urgency": "high" if neg else "normal",
            "score": seed % 10 + 1,
            "tags": [base["category"], base["priority"]],
            "next_step": (sentences[-1][:90] if sentences else "review manually"),
            "deadline": "not specified",
            "amount": (re.findall(r"\b\d{3,}\b", body) or ["none"])[0],
        }
        for key, val in extras.items():
            if key in asked:
                base[key] = val
        return base


class OpenAIProvider(BaseProvider):
    name = "openai"
    env_key, base_env, model_env = "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"
    default_base, default_model = "https://api.openai.com/v1", "gpt-4o-mini"

    def complete_with_usage(self, prompt="", system="", model="auto", temperature=0.2,
                            json_mode=False, **extra):
        import urllib.request
        key = os.environ.get(self.env_key)
        if not key:
            raise RuntimeError(f"{self.env_key} not set — use provider 'mock'")
        base = os.environ.get(self.base_env, self.default_base)
        mdl = os.environ.get(self.model_env, self.default_model) if model in ("auto", "", None) else model
        body = {"model": mdl, "temperature": temperature,
                "messages": ([{"role": "system", "content": system}] if system else [])
                            + [{"role": "user", "content": prompt}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{base}/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
        with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310
            d = json.loads(r.read())
        text = d["choices"][0]["message"]["content"]
        u = d.get("usage", {})
        tin = u.get("prompt_tokens", estimate_tokens(system + prompt))
        tout = u.get("completion_tokens", estimate_tokens(text))
        return text, {"provider": self.name, "model": mdl, "tokens_in": tin,
                      "tokens_out": tout, "cost_usd": price(mdl, tin, tout)}


    supports_streaming = True

    def stream(self, on_token=None, prompt="", system="", model="auto",
               temperature=0.2, json_mode=False, **extra):
        import urllib.request
        key = os.environ.get(self.env_key)
        if not key:
            raise RuntimeError(f"{self.env_key} not set — use provider 'mock'")
        base = os.environ.get(self.base_env, self.default_base)
        mdl = os.environ.get(self.model_env, self.default_model) \
            if model in ("auto", "", None) else model
        body = {"model": mdl, "temperature": temperature, "stream": True,
                "stream_options": {"include_usage": True},
                "messages": ([{"role": "system", "content": system}] if system else [])
                            + [{"role": "user", "content": prompt}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        req = urllib.request.Request(
            f"{base}/chat/completions", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})

        chunks, tin, tout = [], None, None
        with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    d = json.loads(data)
                except Exception:
                    continue
                for ch in d.get("choices") or []:
                    piece = (ch.get("delta") or {}).get("content")
                    if piece:
                        chunks.append(piece)
                        if on_token:
                            on_token(piece)
                if d.get("usage"):
                    tin = d["usage"].get("prompt_tokens", tin)
                    tout = d["usage"].get("completion_tokens", tout)

        text = "".join(chunks)
        tin = tin if tin is not None else estimate_tokens(system + prompt)
        tout = tout if tout is not None else estimate_tokens(text)
        return text, {"provider": self.name, "model": mdl, "tokens_in": tin,
                      "tokens_out": tout, "cost_usd": price(mdl, tin, tout),
                      "streamed": True}


class AnthropicProvider(BaseProvider):
    name = "anthropic"

    def complete_with_usage(self, prompt="", system="", model="auto", temperature=0.2,
                            json_mode=False, **extra):
        import urllib.request
        key = os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set — use provider 'mock'")
        mdl = os.environ.get("ANTHROPIC_MODEL", "claude-3-5-haiku-latest") \
            if model in ("auto", "", None) else model
        pr = prompt + ("\n\nRespond with valid JSON only." if json_mode else "")
        body = {"model": mdl, "max_tokens": 2048, "temperature": temperature,
                "system": system or "", "messages": [{"role": "user", "content": pr}]}
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-api-key": key,
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310
            d = json.loads(r.read())
        text = "".join(b.get("text", "") for b in d.get("content", []))
        u = d.get("usage", {})
        tin = u.get("input_tokens", estimate_tokens(system + prompt))
        tout = u.get("output_tokens", estimate_tokens(text))
        return text, {"provider": self.name, "model": mdl, "tokens_in": tin,
                      "tokens_out": tout, "cost_usd": price(mdl, tin, tout)}


class GeminiProvider(BaseProvider):
    name = "gemini"

    def complete_with_usage(self, prompt="", system="", model="auto", temperature=0.2,
                            json_mode=False, **extra):
        import urllib.request
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise RuntimeError("GEMINI_API_KEY not set — use provider 'mock'")
        mdl = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash") \
            if model in ("auto", "", None) else model
        cfg = {"temperature": temperature}
        if json_mode:
            cfg["responseMimeType"] = "application/json"
        body = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": cfg}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        req = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{mdl}:generateContent?key={key}",
            data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310
            d = json.loads(r.read())
        text = "".join(p.get("text", "") for p in
                       d["candidates"][0]["content"].get("parts", []))
        u = d.get("usageMetadata", {})
        tin = u.get("promptTokenCount", estimate_tokens(system + prompt))
        tout = u.get("candidatesTokenCount", estimate_tokens(text))
        return text, {"provider": self.name, "model": mdl, "tokens_in": tin,
                      "tokens_out": tout, "cost_usd": price(mdl, tin, tout)}


class FallbackProvider(BaseProvider):
    """Try providers in order; fall through on failure. Never fails past mock."""
    name = "auto"

    def __init__(self, chain):
        self.chain = chain

    supports_streaming = True

    def complete_with_usage(self, **kw):
        errs = []
        for p in self.chain:
            try:
                text, usage = p.complete_with_usage(**kw)
                usage["fallbacks"] = errs
                return text, usage
            except Exception as e:  # noqa: BLE001
                errs.append(f"{p.name}: {type(e).__name__}")
        raise RuntimeError("all providers failed: " + "; ".join(errs))

    def stream(self, on_token=None, **kw):
        errs = []
        for p in self.chain:
            try:
                text, usage = p.stream(on_token=on_token, **kw)
                usage["fallbacks"] = errs
                return text, usage
            except Exception as e:  # noqa: BLE001
                errs.append(f"{p.name}: {type(e).__name__}")
        raise RuntimeError("all providers failed: " + "; ".join(errs))


_MOCK = MockProvider()
_PROVIDERS = {"mock": _MOCK, "openai": OpenAIProvider(),
              "anthropic": AnthropicProvider(), "gemini": GeminiProvider()}
_PROVIDERS["auto"] = FallbackProvider([_PROVIDERS["openai"], _PROVIDERS["anthropic"],
                                       _PROVIDERS["gemini"], _MOCK])


def get_provider(name: str) -> BaseProvider:
    return _PROVIDERS.get(name or "mock", _MOCK)


def available() -> Dict[str, bool]:
    return {"mock": True, "openai": bool(os.environ.get("OPENAI_API_KEY")),
            "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
            "gemini": bool(os.environ.get("GEMINI_API_KEY")), "auto": True}

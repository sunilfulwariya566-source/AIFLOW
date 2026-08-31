<h1 align="center">AIFlow</h1>

<p align="center">
  <b>A DAG workflow engine for LLM automation — with a visual builder, budgets, and 517 tests.</b>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-it-does">Features</a> ·
  <a href="#node-types-16">Node types</a> ·
  <a href="#api">API</a> ·
  <a href="SETUP.md">Setup guide</a>
</p>

<p align="center">
  <img alt="tests" src="https://img.shields.io/badge/tests-517%20passing-3ecf8e">
  <img alt="python" src="https://img.shields.io/badge/python-3.9%2B-5b8cff">
  <img alt="license" src="https://img.shields.io/badge/license-MIT-a06bff">
  <img alt="deps" src="https://img.shields.io/badge/dependencies-3-8b98b0">
</p>

<p align="center">
  <img src="docs/overview.svg" alt="AIFlow canvas — a workflow with a sub-workflow call and conditional branches" width="100%">
</p>

---

Build multi-step LLM workflows as a dependency graph, run them from a UI or an API, and
keep them from breaking or overspending in production.

**Runs fully offline out of the box.** A deterministic mock LLM ships with it, so you can
clone, run, and explore every feature without an API key. Point it at OpenAI, Anthropic,
Gemini or Ollama when you're ready — no code changes.

## Quick start

```bash
git clone https://github.com/YOUR_USERNAME/aiflow.git
cd aiflow
./run.sh                    # Windows: run.bat
```

Open **http://localhost:8000**. That's it — the script creates a virtualenv, installs
three dependencies, seeds a database with eight example workflows, and starts the server.

Not sure if your machine is ready? `python3 doctor.py` checks everything and tells you
exactly what to fix.

<details>
<summary>Manual install / Docker</summary>

```bash
pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

```bash
docker compose up -d
```
</details>

## A workflow in 20 lines

```json
{
  "name": "smart-router",
  "nodes": [
    {"id": "message", "type": "input",    "params": {"key": "message", "required": true}},
    {"id": "triage",  "type": "workflow", "params": {"workflow": "lib-classify",
                                                     "payload": {"text": "{{message}}"}}},

    {"id": "urgent",  "type": "llm", "when": "triage.priority == 'high'",
     "params": {"prompt": "One-line incident summary for: {{message}}"}},

    {"id": "routine", "type": "llm", "when": "triage.priority != 'high'",
     "params": {"prompt": "Draft a friendly reply to: {{message}}"}},

    {"id": "out", "type": "output", "params": {"value": {
        "priority": "{{triage.priority}}", "incident": "{{urgent}}", "reply": "{{routine}}"}}}
  ]
}
```

No wiring, no `depends_on` — edges are inferred from `{{references}}`. `triage` calls
another workflow as a single node. Only the branch whose `when` matches actually runs;
the other is skipped and costs nothing.

```bash
curl -X POST localhost:8000/api/workflows/smart-router/run \
  -H "X-API-Key: aiflow-dev-key" -H 'Content-Type: application/json' \
  -d '{"payload": {"message": "URGENT: checkout returns 500"}, "budget": {"max_cost_usd": 0.10}}'
```

## Why this exists

Most LLM "chains" are a script that calls a model three times. That works until it
doesn't: one call fails and you lose the other two, a runaway agent burns $40 overnight,
a prompt change silently breaks step 7, and nobody can tell you what actually ran.

AIFlow treats a workflow as a **graph with an execution contract** — retries and
fallbacks per node, hard spend ceilings, a full trace of every run, cached steps, and the
ability to resume from the exact node that failed.

## What it does

| | |
|---|---|
| **Visual builder** | Drag-and-drop canvas, live-inferred edges, in-place node editing |
| **16 node types** | LLM, agent loop, RAG, sub-workflows, map/filter, approval gates, HTTP |
| **Conditional edges** | `when` expressions; dead branches collapse automatically |
| **Reliability** | Retries with backoff, 4 error policies, timeouts, dead-letter capture |
| **Budget caps** | Cost/token/call ceilings enforced *during* a run, inherited by sub-workflows |
| **Human-in-the-loop** | Approval nodes pause a run; resuming continues from the same point |
| **Observability** | Live SSE trace, token streaming, per-node cost, dashboard, alert rules |
| **Cost control** | Per-node caching, retry-from-failure, batch runs |
| **Portability** | Import/export bundles, workflow versioning with structural diffs |
| **Security** | Scoped API keys, rate limits, HMAC webhooks, AST-sandboxed expressions |

## Tests

```bash
python3 test_engine.py   # 272 checks — engine, sandbox, RAG, agent, budget, cache, diffs
bash    test_api.sh      # 114 checks — auth, HMAC, batch, async jobs, SSE, import/export
node    test_canvas.js   #  55 checks — canvas graph logic, headless
```

**441 checks total**, no test framework required — the suites are plain scripts.
The API suite needs a running server; start it with `AIFLOW_RATE_LIMIT=1000 ./run.sh`
if you plan to run it repeatedly.

## Files
| file | role |
|---|---|
| `engine.py` | DAG resolution, templating, retries/backoff, error policies, cost accounting, streaming events |
| `sandbox.py` | AST-whitelist expression evaluator (replaces bare `eval`) |
| `providers.py` | mock / openai / anthropic / gemini + `auto` fallback chain, token & cost math |
| `tools.py` | hashed embeddings, chunking, vector search, agent tools |
| `store.py` | SQLite: workflows (versioned), runs, schedules, keys, approvals, vectors |
| `app.py` | FastAPI: auth, rate limits, HMAC webhooks, job queue, SSE, scheduler |
| `run.sh` / `run.bat` | one-command launcher (venv + deps + server) |
| `static/index.html` | Builder (Run · **Canvas** · Definition · Webhook) · Dashboard · Approvals · Admin |

## Visual canvas builder
The **Canvas** tab is a drag-and-drop editor built with plain SVG + DOM — no external
library, so it works in a sandboxed preview.

- Drag any of the 15 types from the palette onto the grid to add a node.
- Drag node bodies to reposition; drag from the ● output port to another node to add an
  explicit `depends_on` edge.
- Edges are **inferred live** from `{{refs}}`, expression identifiers, explicit
  `depends_on`, and approval barriers — the same rules the engine uses, so what you see is
  the real execution order.
- Click a node to inspect and edit its id, params, `retries`, and `on_error` in place.
- **Auto-layout** does longest-path layering (dependencies always sit left of dependents).
- **Validate** and **Save** round-trip through `/api/validate` — an invalid graph is never
  saved, and saving creates a new version.

## Node types (16)
| type | purpose |
|---|---|
| `input` | pull from the run payload (`required`, `default`) |
| `llm` | prompt a model (`system`, `prompt`, `temperature`, `json_mode`) |
| `python` | sandboxed expression over prior outputs |
| `template` | static / interpolated text |
| `branch` | `condition` → `if_true` / `if_false` |
| `http` | GET/POST (stubbed in mock mode) |
| `map` | fan out an inner `step` over a list, in parallel threads |
| `filter` | keep list items matching `condition` |
| `validate` | schema check; `soft: true` warns instead of failing |
| `chunk` | split text into overlapping windows |
| `embed` | index texts into a vector collection |
| `retrieve` | top-k semantic search, returns `context` + `matches` |
| `approval` | **pauses the run** until a human decides, then resumes |
| `agent` | ReAct loop — the model picks tools until it finishes |
| `workflow` | **call another workflow as one node** — reuse, with recursion guards |
| `output` | marks a value as a final result |

Reference other nodes with `{{node_id}}` or `{{node.field.0}}` (negative indices work).
Dependencies are inferred from refs *and* from identifiers inside expressions.

## Conditional edges (`when`)
Any node can carry a `when` expression. If it evaluates falsy the node is **skipped**, and
the skip **propagates**: a node whose dependencies were *all* skipped is skipped too, so a
dead branch collapses on its own. A join node that also depends on a live node still runs
and simply sees `None` for the skipped side.

```json
{"id": "escalate", "type": "llm", "when": "triage.priority == 'high'",
 "params": {"prompt": "Draft an incident summary for {{message}}"}}
```
Skipped nodes cost nothing, appear in the trace as `⊘ when: ...`, and are listed in
`run.skipped`. Inside `when` (and `expr`/`condition`) dot-notation reads dict keys exactly
like `{{a.b}}` templates do.

## Sub-workflows
A `workflow` node runs another saved workflow as a single step:

```json
{"id": "triage", "type": "workflow",
 "params": {"workflow": "lib-classify", "payload": {"text": "{{message}}"}}}
```
- `payload` omitted → the child inherits the parent's payload.
- A single-output child is **unwrapped**, so `{{triage.priority}}` just works
  (full form stays at `{{triage.outputs.out.priority}}`).
- Child token/cost usage rolls up into the parent's meter.
- Guarded against self-recursion, mutual recursion, and nesting deeper than 5.
- `ignore_errors: true` lets the parent continue when the child fails.

## Budget caps
Ceilings can be set per run (`POST /run` body) or per workflow (`budget` field). They are
checked **after every provider call**, so a runaway agent or a wide `map` stops mid-flight:

```json
{"budget": {"max_cost_usd": 0.50, "max_tokens": 100000, "max_llm_calls": 25}}
```
A breach ends the run with status `budget_exceeded`, keeps all work completed so far, and
**overrides `on_error` policies** — `continue` cannot spend past a cap. Sub-workflows
inherit the parent's *remaining* budget, so nesting can't be used to escape it.

## Alerting
Rules watch one metric over a sliding window of recent runs and fire when the comparison
holds. Evaluation happens automatically after every run.

| metric | meaning |
|---|---|
| `failure_rate` | % of runs that errored (incl. budget breaches) |
| `error_count` | absolute failures in the window |
| `budget_exceeded` | runs stopped by a cap |
| `p95_ms` / `avg_ms` | latency |
| `cost_usd` | total spend in the window |

Rules can be scoped to one workflow or watch all of them, and support a cooldown so a
sustained outage doesn't spam. Channels: `log` (in-app feed) or `webhook` (POSTs the event
JSON to your URL — Slack, PagerDuty, anything). Fired alerts also stream over SSE, so the
UI badge updates live.

## Starter templates
The **Templates** tab has eight ready-made patterns. Each is a *complete, runnable*
workflow bundled with a sample payload — pick one, name it, and it opens on the Run tab
already laid out and pre-filled, so you can hit Run before editing anything.

| template | shows off |
|---|---|
| Summarize & extract | caching, retries, JSON mode |
| Classify & route | conditional edges (`when`) |
| Batch process a list | parallel `map`, `filter`, aggregation |
| Answer over a knowledge base | `chunk` → `embed` → `retrieve` |
| Draft with human approval | approval gate, pause/resume |
| Research agent | tool loop with a budget ceiling |
| Resilient API call | retries, `fallback`, validation |
| Reusable building block | a library workflow to call from others |

Every template is asserted in the test suite to be a valid DAG **and** to run
successfully from its own sample, so the gallery can't silently rot.

## Parallel execution
By default nodes run serially in declaration order. Pass `parallel: N` (per run, or as a
workflow field) and the executor switches to a **wave scheduler**: every node whose
dependencies are satisfied runs concurrently, up to N at a time.

```bash
curl -X POST localhost:8000/api/workflows/my-wf/run \
  -H "X-API-Key: aiflow-dev-key" -H 'Content-Type: application/json' \
  -d '{"payload": {...}, "parallel": 4}'
```

Four independent LLM branches measured **3.9× faster** than serial, with byte-identical
outputs. Everything still holds under concurrency — dependency order, `when` skips,
budget caps, error policies, caching, and approval pauses are all asserted in the tests.
Serial stays the default so run traces remain deterministic unless you opt in.

## Node timeouts
`timeout` on a node is now enforced (it was previously accepted and ignored). The handler
runs on a worker thread and is abandoned if it overruns:

```json
{"id": "flaky", "type": "http", "timeout": 5, "retries": 2, "on_error": "fallback"}
```

Timeouts count as *transient*, so they interact with `retries` and `on_error` exactly like
any other failure — retry it, fall back, or halt. The trace marks the node `⏱ timed out`.
Python cannot safely kill a running thread, so an abandoned handler may still complete in
the background; its result is discarded.

## Node cache
Mark any node `"cache": true` (or `{"ttl": 3600}`) and its output is reused whenever the
inputs are unchanged. The key hashes the workflow, node id, **rendered** params and the
provider, so editing a prompt or switching model is automatically a miss.

```json
{"id": "classify", "type": "llm", "cache": {"ttl": 3600}, "params": {...}}
```
Cache hits show as `⚡ cache hit` in the trace, cost zero, and report what they saved.
Bypass per run with `"no_cache": true`, or opt every node in with `"cache_all"`.
Manage from **Admin → Node cache** or `GET/DELETE /api/cache`.

## Retry from a failed node
`POST /api/runs/{id}/retry` re-runs only the part that broke. Everything that succeeded
before the failing node is replayed from the stored context — so an expensive chain that
died on step 7 costs nothing to resume:

```bash
curl -X POST localhost:8000/api/runs/$RUN_ID/retry \
  -H "X-API-Key: aiflow-dev-key" -H 'Content-Type: application/json' \
  -d '{"payload": {"missing_field": "now provided"}}'
```
Pass `from_node` to rewind further back, or a new `payload`/`provider` to change what the
retry uses. In the UI a **↻ Retry from failure** button appears on any failed run.

## Streaming LLM output
`llm` nodes stream token-by-token to the UI over SSE, so long generations render as they
arrive instead of appearing all at once. Real streaming is implemented for OpenAI-compatible
endpoints (SSE `chat/completions`), and the mock provider streams word-by-word so the
behaviour is visible offline.

Every token event carries `{node, seq, text}`, so a client can reassemble per node — the
tests assert the reassembled stream equals the final output exactly. Providers that cannot
stream fall back to one final chunk, so callers never branch on capability. Opt out per
node with `"stream": false`, or per run with `stream=False`.

## Web search
The agent's `search` tool uses a real backend when one is configured, picked in this order:

| backend | env var |
|---|---|
| Tavily | `TAVILY_API_KEY` |
| Serper (Google) | `SERPER_API_KEY` |
| Brave | `BRAVE_API_KEY` |
| DuckDuckGo (keyless) | `AIFLOW_SEARCH_DDG=1` |

With none set it uses a deterministic offline stub, and **any backend failure degrades to
that stub** rather than breaking the workflow. The active backend is shown in the header
and on the Admin tab.

## Import / export
Export a workflow and its **sub-workflow closure** as one portable JSON bundle, ordered so
dependencies always load first:

```bash
curl -H "X-API-Key: aiflow-dev-key" \
  "localhost:8000/api/export?names=smart-router" -o smart-router.bundle.json
```

Import handles name collisions three ways — `skip`, `rename` (default), or `overwrite`
(which still creates a version, so rollback works). When renaming, `workflow` node
references are **rewired to the renamed copies** so an imported bundle stays self-consistent.
`dry_run: true` previews the outcome without writing.

In the UI: **Export** / **Export all** / **Import bundle…** in the sidebar.

## Scheduling
Two modes. Interval — `{"every_seconds": 300}` — or **cron**:

```json
{"workflow": "daily-inbox", "cron": "0 9 * * mon-fri"}
```

Standard 5-field syntax with steps (`*/15`), ranges (`1-5`), lists (`1,3,5`), names
(`mon-fri`, `jan`) and shorthands (`@daily`, `@hourly`, `@weekly`). Parsed by a
dependency-free module in `cron.py`.

Unsupported syntax (`L`, `W`, `#`, seconds) is **rejected rather than ignored** — a
schedule that quietly does the wrong thing is worse than one that refuses to save.
`GET /api/cron/check?expr=...` validates an expression, describes it in English, and
previews the next five firings; the UI shows this live as you type.

## Audit log
Every state-changing action is recorded with the API key that made it, the target, an IP,
and a detail payload:

```
GET /api/audit?action=workflow.delete&limit=50    # admin scope only
GET /api/audit/summary
```

Tracked: workflow save/delete/rollback, schedule create/delete, approval decisions, key
create/revoke, bundle imports, template use, alert changes, cache clears. Logging never
throws — a failure to audit can't break the request it is auditing. Oversized detail
payloads are shrunk to stay valid JSON rather than truncated into corruption.

Visible under **Admin → Audit log**, filterable by action.

## Batch runs
Run one workflow over many payloads in a single call, concurrently:

```bash
curl -X POST localhost:8000/api/workflows/smart-router/batch \
  -H "X-API-Key: aiflow-dev-key" -H 'Content-Type: application/json' \
  -d '{"payloads": [{"message": "a"}, {"message": "b"}], "concurrency": 8}'
```

Eight payloads measured **7.9× faster** at `concurrency: 8` (4980ms → 630ms). Each payload
becomes a normal run with its own `run_id`, so it appears in history and can be opened or
retried individually. **A failing payload never kills the batch** — the response summarises
counts by status alongside total tokens and cost. Set `stop_on_error` to cancel the rest on
the first failure. Concurrency is capped at 8, batch size at 200.

In the UI: the **Batch** tab, one JSON payload per line.

## Workflow version diff
Every save creates a version; now you can diff any two structurally:

```
GET /api/workflows/{name}/versions/diff?a=1&b=4
```

Reports nodes added, removed and changed — and for changed nodes, exactly which fields
differ (`params`, `retries`, `when`, `timeout`, `on_error`, …) with old and new side by
side. Workflow-level fields (`description`, `on_error`, `budget`, `parallel`) are diffed
too, and a pure **reordering** of the node list is flagged separately since it changes
execution order without changing any node.

In the UI: click two version numbers in the sidebar, then **Diff**.

## Run comparison
Diff any two runs side by side — status, duration, tokens, cost (with deltas), plus a
node-by-node output comparison that highlights only what actually changed and flags nodes
present in one run but not the other.

In the UI: **Compare runs** above the run history, pick two, and the diff opens.

## Reliability
- **Retries** with exponential backoff + jitter; deterministic errors (syntax, sandbox,
  missing input) are *not* retried.
- **Error policies** per node or per workflow: `stop` · `continue` · `fallback` (uses
  `fallback:`) · `dead_letter` (collects into `run.dead_letter`).
- **Approval gates** persist to SQLite and act as topological barriers — nothing declared
  after a gate runs until it's decided. Resuming replays context and keeps the same `run_id`.

## Production checklist

Local defaults are deliberately convenient. Before exposing this to a network, set:

```bash
AIFLOW_ADMIN_KEY=<long random string>      # else the seeded admin key is the public default
AIFLOW_WEBHOOK_SECRET=<long random string> # else anyone can forge webhook calls
AIFLOW_RATE_LIMIT=60                       # tune to your traffic
```

The server **prints a loud warning at startup** for each insecure default still in place,
and reports them on `GET /api/health` under `insecure_defaults`. An empty list there means
the box is configured safely.

Also recommended: terminate TLS with a reverse proxy (nginx/Caddy), and rotate the seeded
key from **Admin → API keys** once you have your own.

## Security
- **API keys** on all `/api/*` routes via `X-API-Key`, scopes `run|write|approve|admin`.
- **Rate limiting** per key+IP (`AIFLOW_RATE_LIMIT`, default 120/min).
- **HMAC-SHA256 webhooks** — body must be signed with `AIFLOW_WEBHOOK_SECRET`.
- **AST sandbox** blocks imports, dunder traversal, `getattr`/`eval`/`open`, lambdas,
  non-whitelisted attributes, and memory/CPU bombs, with node-count and time budgets.

Disable auth for local hacking with `AIFLOW_AUTH=0`.

## API
```
GET    /api/health                       public
GET    /api/workflows                    list
GET    /api/workflows/{n}                fetch
PUT    /api/workflows/{n}                save (auto-versions)
DELETE /api/workflows/{n}
GET    /api/workflows/{n}/versions       version history
POST   /api/workflows/{n}/rollback/{v}   restore a version
POST   /api/workflows/{n}/run            run (sync, or {"async_mode":true})
GET    /api/jobs/{id}                    async job status
POST   /api/validate                     static check + execution order
GET    /api/runs?workflow=&status=&limit=&offset=
GET    /api/runs/{id}
GET    /api/runs/compare?a=&b=           side-by-side diff of two runs
POST   /api/workflows/{n}/batch          run many payloads concurrently
GET    /api/workflows/{n}/versions/diff?a=&b=   structural version diff
POST   /api/runs/{id}/retry              re-run from the failed node
GET/DELETE /api/cache                    node cache stats / clear
GET    /api/cron/check?expr=             validate + preview a cron expression
GET    /api/audit                        audit log (admin)
GET    /api/audit/summary                counts by action and actor
GET    /api/templates                    starter gallery
POST   /api/templates                    {template, name} → new workflow
GET    /api/export?names=&include_deps=  portable bundle download
POST   /api/import                       {bundle, mode, dry_run}
GET    /api/stats                        success rate, p95, tokens, cost
GET/POST/DELETE /api/alerts              alert rules
POST   /api/alerts/evaluate              force an evaluation pass
GET    /api/alerts/events                fired-alert history
GET    /api/alerts/metrics?window=20     current metric window
GET    /api/events                       SSE live execution stream
GET    /api/approvals?status=pending
POST   /api/approvals/{id}               decide + resume the paused run
GET/POST/DELETE /api/schedules
GET/POST/DELETE /api/keys                admin scope
POST   /hooks/{n}                        HMAC-signed webhook trigger
```

```bash
BODY='{"ticket":"URGENT: charged twice","customer":"Priya"}'
SIG=$(python3 -c "import hmac,hashlib;print(hmac.new(b'dev-webhook-secret',b'''$BODY''',hashlib.sha256).hexdigest())")
curl -X POST localhost:8000/hooks/support-triage \
  -H 'Content-Type: application/json' -H "X-Signature: $SIG" -d "$BODY"
```

## Real models
```bash
export OPENAI_API_KEY=sk-...        # or OPENAI_BASE_URL for Ollama/vLLM/Together
export ANTHROPIC_API_KEY=sk-ant-...
export GEMINI_API_KEY=...
```
Pick a provider in the header dropdown, or `auto` to try each in turn and fall back to
mock. Costs are priced per model and rolled up per node, per run, and on the dashboard.

## Seeded workflows
`support-triage` · `doc-summarize-chain` · `batch-review-miner` (parallel fan-out) ·
`content-pipeline` (approval gate) · `kb-rag-answer` (RAG) · `agent-research` (tool loop) ·
`lib-classify` (reusable block) · `smart-router` (sub-workflow + conditional edges)

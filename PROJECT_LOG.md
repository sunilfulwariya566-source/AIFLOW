# AIFlow — Project Log

**Save date:** 2026-08-27 · **Status:** complete, chal raha hai, GitHub-ready · **Tests:** 517/517 passing

Ye file is project ki poori kahani hai — kya banaya, kaise banaya, kya decide kiya,
aur aage kya kar sakte hain. Baad me chat continue karne ke liye bas ye padh lena.

---

## 🔍 PRODUCTION AUDIT (round 27) — sab pass

Poora audit chalaya: 517 test + 25 subsystem + 22 security + 25 route checks. **Sab green.**
Teen production issues mile aur fix kiye:

1. **Seeded admin key hardcoded tha** (`aiflow-dev-key`) — public repo me sab ko pata chal
   jaata aur rotate karne ke liye code edit karna padta. Ab `AIFLOW_ADMIN_KEY` env var se
   override hota hai.
2. **Insecure defaults chupchaap chalte the** — ab startup pe loud warning print hoti hai
   aur `/api/health` me `insecure_defaults` array aata hai.
3. **`PORT` aur `AIFLOW_SEARCH` undocumented the** — `.env.example` me add kar diye.

Detail README ke "Production checklist" section me hai.

---

## 🔴 ABHI KAHAN RUKE THE (sabse zaroori)

**Round 25:** User ne "asli workflow banao" chuna. Banaya **`daily-inbox`** — koi bhi text
(WhatsApp/email/notes) se summary + action items + priority + draft reply nikaalta hai.
Chalta hua verify kiya. **User se abhi feedback lena hai** ki ye unke kaam ka hai ya nahi,
aur unka asli use case kya hai.

**Uske alawa: GitHub pe publish karna abhi bhi pending hai.** Sab tayyar hai, bas upload karna hai.

- **`aiflow-github.zip`** already bani hui hai (`/home/user/aiflow-github.zip`, 120 KB, 34 files)
  — usme database aur secrets nahi hain, verify kiya hua hai
- **`PUBLISH.md`** me 3 tarike likhe hain. **Tarika A (browser drag-drop)** sabse aasan hai —
  koi git command nahi chahiye
- User ne kaha tha "kuch samajh nahi aa raha" — isliye guide bilkul step-by-step likhi hai
  hidden files (`.gitignore`, `.github`) wali warning ke saath

**Continue karte waqt pehle ye puchna:** GitHub pe daal diya ya nahi? Atke to kahan?

---

## 1. Kya banaya

**AIFlow** — ek AI automation system. Workflows ko DAG (directed acyclic graph) ki tarah
chalata hai, web UI ke saath. Poora offline chalta hai ek deterministic mock LLM pe;
real model (OpenAI / Anthropic / Gemini / Ollama) sirf env var se plug ho jata hai.

- **~5,900 lines** code, 10 Python modules + 1 single-file UI
- **16 node types**, 9 workflows (8 seeded + `daily-inbox`), 8 starter templates
- **517 automated tests** (332 engine + 130 API + 55 canvas), sab green
- Server: `http://localhost:8000`

---

## 2. Kaise yahan tak pahunche (chat ka safar)

| Round | User ne kaha | Kya bana |
|---|---|---|
| 1 | "make a ai automation system" | Clarify kiya → workflow engine + web app + mock LLM. Basic DAG engine, 7 node types, UI, scheduler, 24 tests |
| 2 | "next" | `map` (parallel fan-out), `filter`, `validate`; webhook trigger; `/api/validate` |
| 3 | "isme aur kya kya baki h" | Honest gap analysis — 14 items, red/yellow/green priority |
| 4 | "isme jo jo baki h wo pura kro" | **Sab kuch**: SQLite, auth, AST sandbox, cost tracking, approval gates, async+SSE, error policies, RAG, agent loop, versioning, dashboard, multi-provider |
| 5 | "next" | Visual drag-drop canvas builder + headless canvas tests |
| 6 | "run the server again" | Server restart |
| 7 | "isko laptop me setup kese kru" | `SETUP.md`, `run.sh`, `run.bat`, `requirements.txt`, `.env.example` |
| 8 | "chat save kar lo" | `PROJECT_LOG.md` (ye file) |
| 9 | "next" | Canvas gaps fix (edge delete + layout persistence) + Docker. Tests →159 |
| 10 | "next" | **Sub-workflows** + **conditional edges** (`when`). Sandbox dot-notation fix. Tests →201 |
| 11 | "next" | **Budget caps** + **alerting** (cooldown, webhook delivery, live SSE). Tests →238 |
| 12 | "next" | **Import/export bundles** + **run comparison**. Tests →287 |
| 13 | "next" | **Streaming LLM output** (SSE tokens) + **real web search** (4 backends). Tests →312 |
| 14 | "next" | **Node cache** (hashed keys, TTL) + **retry from failed node**. Tests →345 |
| 15 | "next" | **Templates gallery** — 8 runnable patterns. Tests →388 |
| 16 | "next" | **Parallel execution** (wave scheduler, 3.9x) + **node timeout enforcement**. Tests →415 |
| 17 | "next" | **Batch runs** (7.9x faster) + **workflow version diff**. Tests →441 |
| 18 | "ab or kya baki hai kab pura hoga" | Bataya ki ye **already complete hai** — jo bacha wo alag product hai (multi-tenancy/Postgres). Salaah di: naye features band karo, use karna shuru karo |
| 19 | "ab muje isko setup krna hai guide kar" | `SETUP.md` rewrite + **clean-room test** (naye folder me poora setup chalake verify kiya) |
| 20 | "kya tum khud setup kar skte ho" | Bataya ki unke laptop tak access nahi. Banaya **`doctor.py`** — setup checker jo khud diagnose karta hai |
| 21 | "8 workflow pr me kese dekhu" | UI tour diya |
| 22 | "kya isse erning hogi" | Honest answer: software bech ke nahi, **usse kaam karke**. 4 raaste bataye (freelancing sabse realistic, portfolio sabse fast) |
| 23 | "next" | **GitHub-ready**: LICENSE, .gitignore, CI workflow, README rewrite + SVG diagram, PUBLISH.md |
| 24 | "kese publish kru samajh nahi aa raha" | **`aiflow-github.zip`** banai + PUBLISH.md rewrite — browser drag-drop tarika, bina git ke |
| 26 | "next" | **Cron expressions** (`cron.py`, dependency-free) + **audit log**. Do bugs mile: duplicate scheduler (do server processes) aur audit detail truncation JSON corrupt kar rahi thi. Tests →517 |
| 25 | "continue automation system" | **`daily-inbox`** workflow banaya (9th). Mock provider ab **koi bhi JSON schema honour karta hai** — pehle sirf fixed fields deta tha, isliye custom workflows me `action_items`/`needs_reply` khaali aate the. Tests →442 |

---

## 3. Files

```
aiflow/
├── engine.py        836  DAG resolution, templating, retries, error policies, cost,
│                         streaming, approval pause/resume, cache, budget, parallel, timeout
├── app.py           755  FastAPI — auth, rate limit, HMAC webhooks, job queue, SSE,
│                         scheduler, templates, import/export, retry, batch, alerts
├── store.py         652  SQLite — workflows(versioned), runs, schedules, keys,
│                         approvals, vectors, alerts, cache
├── bundle.py        359  import/export bundles + run diffing + version diffing
├── providers.py     341  mock / openai / anthropic / gemini + auto-fallback, streaming
├── templates.py     254  8 starter templates + instantiation
├── tools.py         225  embeddings, chunking, vector search, web search (4 backends)
├── sandbox.py       150  AST-whitelist expression evaluator
├── alerts.py        100  rule evaluation, cooldown, log/webhook delivery
├── cron.py          180  5-field cron parser, describe + preview
├── doctor.py        230  setup checker — files, deps, port, db, live engine test
├── static/index.html      Builder(Run·Canvas·Definition·Batch·Webhook) + Dashboard +
│                          Templates + Approvals + Alerts + Compare + Admin
├── test_engine.py   272 checks · test_api.sh 114 · test_canvas.js 55
├── run.sh / run.bat       one-command launcher
├── Dockerfile / compose.yml
├── docs/overview.svg      README diagram
├── .github/workflows/tests.yml   CI on 3 Python versions
├── LICENSE (MIT) · .gitignore · .env.example
├── README.md · SETUP.md · PUBLISH.md · PROJECT_LOG.md
└── data/aiflow.db         saara data (gitignored)
```

---

## 4. 16 Node types

| type | kaam |
|---|---|
| `input` | run payload se value uthao |
| `llm` | model ko prompt karo (streams by default) |
| `python` | sandboxed expression |
| `template` | static / interpolated text |
| `branch` | `condition` → `if_true` / `if_false` |
| `http` | GET/POST (mock mode me stubbed) |
| `map` | list pe inner `step` parallel threads me |
| `filter` | `condition` match karne wale items |
| `validate` | schema check; `soft:true` warn karta hai |
| `chunk` | text ko overlapping windows me todo |
| `embed` | texts ko vector collection me index karo |
| `retrieve` | top-k semantic search |
| `approval` | **run rok deta hai** insaan ke decide karne tak |
| `agent` | ReAct loop — model khud tools chunta hai |
| `workflow` | dusre workflow ko ek node ki tarah call karo |
| `output` | final result mark karo |

**Node modifiers:** `retries`, `on_error` (stop/continue/fallback/dead_letter), `fallback`,
`when` (conditional edge), `cache` (true ya `{ttl}`), `timeout`, `depends_on`.

---

## 5. Ahem technical decisions (kyun aisa kiya)

**SQLite, JSON files nahi** — concurrent write race condition thi. WAL mode, indexed,
additive migrations (`ALTER TABLE`), purana data safe.

**AST whitelist, bare `eval()` nahi** — imports, dunder traversal, `getattr`, `eval`,
`open`, lambdas, memory/CPU bombs block. Node-count aur time budget.

**Dot-notation dicts pe** — `when: "triage.priority == 'high'"` chale kyunki `{{a.b}}`
templates me chalta hai. AST rewriter `a.b` ko guarded `_attr()` me badalta hai.

**Approval gate = topological barrier** — gate ke baad ke nodes decision tak nahi chalte.

**Skip propagation conservative** — node tabhi skip jab **saari** deps skip hui hon,
warna join node (`output`) gayab ho jaata.

**Deterministic errors pe retry nahi** — SandboxError, WorkflowError, NameError, SyntaxError.

**Lazy render subtrees** — `map` ke `step` ko outer scope me render nahi karte
(`LAZY_KEYS = {"step"}`), warna `{{item}}` khaali ho jaata.

**Budget har provider call ke baad** — poore run ke baad nahi, taaki bhaagta agent beech me
ruke. `on_error: continue` bhi cap se aage kharch nahi kar sakta.

**Cache key me rendered params** — prompt edit karo ya model badlo = automatic miss.

**Search failure pe stub** — galat key, network down: agent chalta rehta hai.

**Serial default, parallel opt-in** — traces deterministic rahein.

**Mock provider deterministic** — tests reliable, demo bina key ke.

**Canvas plain SVG+DOM** — koi external library nahi, sandboxed preview me bhi chale.

---

## 6. Bugs jo tests ne pakde

1. **`map` ke andar `{{item}}` blank** — pura params tree pehle render ho jaata tha, saare
   batch LLM prompts khaali. Fix: `LAZY_KEYS`.
2. **Approval ke baad wale nodes chal jaate the** — pause hone ke bawajood. Fix: barrier.
3. **`9**9**9` CPU bomb** — sandbox hang. Fix: exponent literal, ≤64.
4. **Comprehension loop var reject** — `ast.Store` whitelist me nahi tha.
5. **Embeddings me common words dominate** — Fix: stopwords + 128→512 dim.
6. **Version rows orphan** — workflow delete pe versions rah jaate the.
7. **Sandbox dot-notation** — templates se inconsistent tha. Fix: AST rewriter.
8. **Skip propagation zyada aggressive** — join node bhi skip ho jaata tha.
9. **Layout save version bump karta tha** — alag `save_layout()` banaya.
10. **`timeout` field dead code tha** — accept hota tha par enforce nahi. Ab thread-based.
11. **Test suite apni hi rate limit se takra gayi** — 114 API tests + back-to-back run =
    120/min cross. Bug nahi tha; `AIFLOW_RATE_LIMIT=1000` use karo.
12. **README stale test count** — "148 checks" likha tha jab actual 441 the. Header rewrite.
14. **Audit detail truncation JSON corrupt kar deti thi** — 2000 chars pe kaat dene se
    invalid JSON banta tha, aur phir **poora audit log unreadable** ho jaata tha (ek bada
    entry sab kuch tod deta). Fix: serialise karne se *pehle* shrink karo, aur reader ko
    tolerant banao.
15. **Do uvicorn processes chal rahe the** — purana properly mara nahi tha, dono ke apne
    scheduler threads the, isliye cron duplicate fire ho raha tha. Engine ka bug nahi tha.
13. **Mock provider fixed schema deta tha** — `json_mode` me hamesha wahi 5 fields
    (category/priority/sentiment/entities/summary), chahe workflow kuch aur maange. Custom
    workflows me fields `None` aa rahe the aur `when` conditions galat evaluate ho rahi thi.
    Fix: ab system prompt se field names padh ke plausible values bhar deta hai.

---

## 7. Security

- **API keys** — `X-API-Key`, scopes `run|write|approve|admin`
- **Rate limiting** — per key+IP, default 120/min
- **HMAC-SHA256 webhooks**
- **AST sandbox** — imports/dunders/builtins blocked, resource budgets
- Dev key: `aiflow-dev-key` · Band karna ho: `AIFLOW_AUTH=0`

---

## 8. Abhi ka state

- Server chal raha hai port 8000 pe
- `data/aiflow.db` — 8 workflows, 714 runs, integrity `ok`, WAL checkpointed
- Sab tests green:
  ```bash
  python3 test_engine.py    # 272
  bash    test_api.sh       # 114   (AIFLOW_RATE_LIMIT=1000 se server chalao)
  node    test_canvas.js    #  55
  ```
- Setup: `./run.sh` · `run.bat` · `docker compose up -d` · `python3 doctor.py`
- **GitHub package ready:** `/home/user/aiflow-github.zip`

---

## 9. Aage kya (priority order)

**1. GitHub pe publish karo** ← yahin ruke the. `PUBLISH.md` Tarika A follow karo.

**2. Ise actually use karo.** Ab tak har feature *anumaan* se bana hai. Ek asli workflow
banao — koi cheez jo aap roz karte ho. Jo kami lage wahi banao.

**3. Earning ke raaste** (round 22 me discuss hua):
- **Freelancing/agency** — sabse realistic. AIFlow bech nahi, usse client ka kaam karo.
  ₹20k–1L per project. Chahiye: clients, code nahi
- **Portfolio/job** — sabse fast aur certain. 441 tests + AST sandbox + DAG engine =
  senior-level signal
- **Vertical SaaS** — ek niche ki ek problem. 6-12 mahine
- **Open source → consulting** — slow burn

**Technical scope jo bacha hai** (zaroori nahi, alag product hai):
- **Multi-tenancy** — users, teams, workspace isolation
- **Postgres** — try kiya tha, sandbox me server install nahi ho saka (no root). Bina asli
  DB pe test kiye ship karna theek nahi tha. `psycopg2` install ho jaata hai, to laptop pe
  ho sakta hai — `store.py` ka connection layer swap karna hoga (`?`→`%s`,
  `INSERT OR REPLACE`→`ON CONFLICT`)
- Prompt playground

---

## 10. Baad me continue karne ke liye

Chat me likh dena:
> "aiflow project continue karna hai — PROJECT_LOG.md padh lo"

Server chalane ke liye:
```bash
cd aiflow && ./run.sh
```
Sandbox ne Python packages wipe kar diye ho (ye hota rehta hai):
```bash
pip install -r requirements.txt
```

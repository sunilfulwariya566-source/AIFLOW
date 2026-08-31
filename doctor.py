#!/usr/bin/env python3
"""AIFlow doctor — setup check karta hai aur seedha bata deta hai kya galat hai.

Chalao:  python3 doctor.py       (Windows: python doctor.py)

Ye kuch install nahi karta, kuch badalta nahi — sirf dekhta hai aur batata hai.
Kuch samajh na aaye to iska poora output copy karke bhej dena.
"""
from __future__ import annotations

import os
import platform
import socket
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OK, WARN, BAD = "  [ ok ]", "  [warn]", "  [FAIL]"
problems: list[str] = []
warnings_: list[str] = []


def ok(msg):
    print(f"{OK} {msg}")


def bad(msg, fix):
    print(f"{BAD} {msg}")
    problems.append((msg, fix))


def warn(msg, fix):
    print(f"{WARN} {msg}")
    warnings_.append((msg, fix))


print("=" * 62)
print(" AIFlow doctor")
print("=" * 62)

# ---------------------------------------------------------------- python
print("\n1. Python")
v = sys.version_info
print(f"     version: {platform.python_version()}  ({platform.system()})")
if v < (3, 9):
    bad(f"Python {platform.python_version()} bahut purana hai (3.9+ chahiye)",
        "python.org/downloads se naya Python install karo. "
        "Windows pe 'Add Python to PATH' tick karna.")
else:
    ok("version theek hai")

in_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
if in_venv:
    ok("virtual environment ke andar chal rahe ho")
else:
    warn("virtual environment ke bahar ho",
         "Koi dikkat nahi agar ./run.sh use kar rahe ho — wo khud venv banata hai.")

# ---------------------------------------------------------------- files
print("\n2. Zaroori files")
need = {
    "app.py": "web server",
    "engine.py": "workflow engine",
    "store.py": "database layer",
    "providers.py": "LLM providers",
    "sandbox.py": "expression sandbox",
    "tools.py": "search + embeddings",
    "alerts.py": "alerting",
    "bundle.py": "import/export + diffs",
    "templates.py": "starter templates",
    "static/index.html": "web UI",
    "requirements.txt": "dependency list",
}
missing = [f for f in need if not os.path.exists(os.path.join(HERE, f))]
for f, what in need.items():
    if f not in missing:
        ok(f"{f:22} ({what})")
for f in missing:
    bad(f"{f} MISSING ({need[f]})",
        "Folder poora download nahi hua. `aiflow` folder dobara download karo — "
        "static/ folder bhi saath aana chahiye.")

# ---------------------------------------------------------------- deps
print("\n3. Dependencies")
for mod, pkg in (("fastapi", "fastapi"), ("uvicorn", "uvicorn"), ("pydantic", "pydantic")):
    try:
        m = __import__(mod)
        ok(f"{mod:10} {getattr(m, '__version__', getattr(m, 'VERSION', '?'))}")
    except ImportError:
        bad(f"{mod} install nahi hai",
            "Chalao:  pip install -r requirements.txt\n"
            "         (ya seedha ./run.sh — wo khud install kar deta hai)")

try:
    import sqlite3
    ok(f"sqlite3    {sqlite3.sqlite_version} (Python ke andar aata hai)")
except ImportError:
    bad("sqlite3 nahi mila", "Ye normally Python ke saath aata hai. Python reinstall karo.")

# ---------------------------------------------------------------- port
print("\n4. Port")
port = int(os.environ.get("PORT", "8000"))
s = socket.socket()
s.settimeout(1)
busy = s.connect_ex(("127.0.0.1", port)) == 0
s.close()
if busy:
    warn(f"port {port} pehle se busy hai",
         f"Ya to AIFlow already chal raha hai (browser me localhost:{port} kholo), "
         f"ya dusra port use karo:  PORT=9000 ./run.sh")
else:
    ok(f"port {port} free hai")

# ---------------------------------------------------------------- data
print("\n5. Database")
data_dir = os.path.join(HERE, "data")
db = os.path.join(data_dir, "aiflow.db")
if not os.path.exists(db):
    ok("abhi nahi bani — pehli baar server chalte hi apne aap ban jaayegi")
else:
    size = os.path.getsize(db) / 1024
    try:
        import sqlite3
        c = sqlite3.connect(db)
        integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
        n_wf = c.execute("SELECT COUNT(*) FROM workflows").fetchone()[0]
        n_runs = c.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
        c.close()
        if integrity == "ok":
            ok(f"database theek hai ({size:.0f} KB, {n_wf} workflows, {n_runs} runs)")
        else:
            bad(f"database corrupt: {integrity}",
                "data/ folder delete kar do — fresh seed ban jaayegi. "
                "(purani history chali jaayegi)")
    except Exception as e:
        bad(f"database khul nahi rahi: {e}",
            "data/ folder delete kar do aur server dobara chalao.")

if os.path.exists(data_dir) and not os.access(data_dir, os.W_OK):
    bad("data/ folder me likhne ki permission nahi",
        f"Chalao:  chmod u+w {data_dir}")

# ---------------------------------------------------------------- imports
print("\n6. Code load hota hai ya nahi")
sys.path.insert(0, HERE)
if not missing and not problems:
    try:
        import engine  # noqa: F401
        import store   # noqa: F401
        import templates  # noqa: F401
        ok("saare modules import ho gaye")
        try:
            store.init()
            wfs = store.list_workflows()
            ok(f"store chal raha hai ({len(wfs)} workflows)")
            from engine import Workflow, run_workflow
            r = run_workflow(
                Workflow.from_dict({"name": "_doctor", "nodes": [
                    {"id": "a", "type": "template", "params": {"text": "hello"}},
                    {"id": "b", "type": "llm", "params": {"prompt": "Summarize: {{a}}"}},
                    {"id": "out", "type": "output", "params": {"value": "{{b}}"}}]}),
                {})
            if r["status"] == "success":
                ok("test workflow chal gaya — engine kaam kar raha hai")
            else:
                bad(f"test workflow fail hua: {r.get('error')}",
                    "Ye unexpected hai. Poora output bhej dena.")
        except Exception as e:
            bad(f"engine chala nahi: {type(e).__name__}: {e}",
                "Ye unexpected hai. Poora output bhej dena.")
    except Exception as e:
        bad(f"import fail: {type(e).__name__}: {e}",
            "Files adhoori ho sakti hain. Folder dobara download karo.")
else:
    warn("skip kiya — pehle upar wale problems theek karo", "")

# ---------------------------------------------------------------- config
print("\n7. Config (sab optional)")
providers_set = [k for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY")
                 if os.environ.get(k)]
if providers_set:
    ok(f"real LLM configured: {', '.join(providers_set)}")
else:
    ok("koi API key nahi — offline mock model use hoga (bilkul theek hai)")

search = [k for k in ("TAVILY_API_KEY", "SERPER_API_KEY", "BRAVE_API_KEY")
          if os.environ.get(k)]
ok(f"web search: {search[0] if search else 'offline stub'}")
ok(f"auth: {'ON' if os.environ.get('AIFLOW_AUTH', '1') != '0' else 'OFF'}"
   f"  ·  rate limit: {os.environ.get('AIFLOW_RATE_LIMIT', '120')}/min")

# ---------------------------------------------------------------- verdict
print("\n" + "=" * 62)
if problems:
    print(f" {len(problems)} PROBLEM{'S' if len(problems) > 1 else ''} MILE\n")
    for i, (msg, fix) in enumerate(problems, 1):
        print(f" {i}. {msg}")
        for line in fix.split("\n"):
            print(f"    → {line}")
        print()
    print(" Ye theek karke doctor dobara chalao.")
    sys.exit(1)

print(" SAB THEEK HAI ✓\n")
if warnings_:
    for msg, fix in warnings_:
        print(f" note: {msg}")
        if fix:
            print(f"       → {fix}")
    print()
print(" Ab chalao:")
print(f"   ./run.sh                 (Windows: run.bat)")
print(f"   phir browser me kholo:   http://localhost:{port}")
print("=" * 62)

# AIFlow — Setup Guide

Sirf **Python 3.9+** chahiye. Koi database install nahi karna (SQLite Python ke andar hai),
koi API key nahi chahiye (offline mock model se pura system chalta hai).

> Ye guide clean environment pe test ki gayi hai — venv banna, deps install hona,
> database seed hona, workflow chalna, sab verify kiya gaya hai.

---

## Step 1 — Files download karo

Workspace se **`aiflow` folder poora** download karke laptop pe rakh do, jaise
`~/Desktop/aiflow` (Windows: `C:\Users\<naam>\Desktop\aiflow`).

**⚠️ Dhyan do:** `.env.example` file ka naam dot se shuru hota hai, to kuch systems pe wo
**hidden** hoti hai aur copy karte waqt chhoot jaati hai. Wo optional hai (sirf API keys ke
liye), par agar mile to saath le lena.

Ye 22 files honi chahiye:

```
aiflow/
├── app.py  engine.py  store.py  providers.py  sandbox.py     ← core
├── tools.py  alerts.py  bundle.py  templates.py              ← features
├── static/index.html                                          ← UI (ye folder zaroori hai)
├── requirements.txt  run.sh  run.bat  .env.example            ← setup
├── doctor.py                                                  ← setup checker
├── test_engine.py  test_api.sh  test_canvas.js                ← tests
├── Dockerfile  compose.yml                                    ← docker (optional)
└── README.md  SETUP.md  PROJECT_LOG.md                        ← docs
```

Check karne ke liye — folder me jaake:
```bash
ls app.py static/index.html requirements.txt
```
Teeno dikhne chahiye. Agar `static/index.html` nahi hai to UI nahi chalega.

---

## Step 2 — Python check karo

Terminal (Mac/Linux) ya Command Prompt (Windows) kholo:

```bash
python3 --version      # Windows pe: python --version
```

`Python 3.9` ya usse naya aana chahiye. Nahi hai to
[python.org/downloads](https://www.python.org/downloads/) se install karo.
**Windows pe install karte waqt "Add Python to PATH" wala box zaroor tick karna.**

---

## Step 2.5 — Doctor chalao (sabse aasan tarika)

Kuch bhi karne se pehle ye chala lo — ye khud check kar lega ki sab theek hai ya nahi:

```bash
cd ~/Desktop/aiflow
python3 doctor.py          # Windows: python doctor.py
```

Ye kuch install nahi karta, kuch badalta nahi — sirf dekhta hai aur seedha batata hai
kya kami hai aur uska fix kya hai. Aakhir me `SAB THEEK HAI ✓` aana chahiye.

Agar kuch bhi samajh na aaye, **doctor ka poora output copy karke mujhe bhej dena** —
usme sab kuch hota hai jo diagnose karne ke liye chahiye.

---

## Step 3 — Chalao

### Mac / Linux
```bash
cd ~/Desktop/aiflow
chmod +x run.sh        # sirf pehli baar
./run.sh
```

### Windows
`run.bat` pe **double-click** karo, ya:
```cmd
cd C:\Users\<naam>\Desktop\aiflow
run.bat
```

**Pehli baar ~1 minute lagega** — virtual environment banega aur 3 packages install honge.
Ye output aana chahiye:

```
→ Creating virtual environment (one time)...
→ Dependencies installed.

  AIFlow chal raha hai:  http://localhost:8000
  Rokne ke liye Ctrl+C dabao

INFO:     Uvicorn running on http://0.0.0.0:8000
```

Uske baad har baar 2 second me start hoga.

---

## Step 4 — Browser kholo

### **http://localhost:8000**

Bas! Aath workflows pehle se loaded milenge.

Band karne ke liye terminal me **Ctrl+C**.

---

## Step 5 — Pehla test (30 second)

1. Left sidebar se **`smart-router`** chuno
2. **▶ Run** dabao
3. Trace me dekho: `triage` node sub-workflow chalata hai, `normal` node
   `⊘ when: ...` ke saath **skip** hota hai
4. Ab payload me message badlo `"how do I reset my password"` aur phir Run karo —
   ab ulta hoga, `urgent` skip hoga

Ye kaam kar gaya to sab theek hai.

---

## Sab theek hai ya nahi — verify karo

Server chalte waqt **dusri** terminal window me:

```bash
cd aiflow
source .venv/bin/activate          # Windows: .venv\Scripts\activate

python test_engine.py              # 272 checks
bash   test_api.sh                 # 114 checks  (Windows pe Git Bash chahiye)
node   test_canvas.js              # 55 checks   (optional — Node.js chahiye)
```

Sab `0 failed` dikhna chahiye.

**Note:** API suite ~110 requests karti hai. Bar-bar chalani ho to server ko aise start karo,
warna rate limiter 429 dene lagega:
```bash
AIFLOW_RATE_LIMIT=1000 ./run.sh
```

---

## Docker se chalana (alternative)

Docker installed hai to Python setup ki zaroorat hi nahi:

```bash
cd aiflow
docker compose up -d          # pehli baar build hoga, ~1 min
```
Kholo **http://localhost:8000**

```bash
docker compose logs -f        # logs dekho
docker compose down           # band karo
docker compose up -d --build  # code change ke baad rebuild
```

Database `./data/aiflow.db` me host pe rehta hai, to container delete karne pe bhi data
safe hai. Port badalna ho: `PORT=9000 docker compose up -d`.

---

## Manual tarika (agar script na chale)

```bash
cd aiflow
python3 -m venv .venv

# Mac/Linux:
source .venv/bin/activate
# Windows:
.venv\Scripts\activate

pip install -r requirements.txt
python -m uvicorn app:app --host 0.0.0.0 --port 8000
```

---

## Real LLM connect karna (optional)

**Bina key ke sab kuch chalta hai** — mock model deterministic hai, seekhne aur testing ke
liye perfect. Asli model chahiye to:

```bash
cp .env.example .env      # Windows: copy .env.example .env
```

`.env` khol ke apni key daalo:
```bash
OPENAI_API_KEY=sk-...
OPENAI_MODEL=gpt-4o-mini
```

Server restart karo, phir UI ke upar-right dropdown se provider **openai** chuno.

Anthropic aur Gemini bhi supported hain. `auto` chuno to ek fail hone pe agla try karega,
aur aakhir me mock pe gir jaayega — kabhi crash nahi hoga.

**Local model (Ollama):** pehle `ollama serve` chalao, phir `.env` me:
```bash
OPENAI_API_KEY=ollama
OPENAI_BASE_URL=http://localhost:11434/v1
OPENAI_MODEL=llama3.1
```

**Web search (agent ke liye):** `TAVILY_API_KEY`, `SERPER_API_KEY` ya `BRAVE_API_KEY`
daal do, ya `AIFLOW_SEARCH_DDG=1` keyless ke liye.

---

## Problem aaye to

| Problem | Hal |
|---|---|
| `Address already in use` | Port busy hai. `PORT=9000 ./run.sh` (Windows: `set PORT=9000` phir `run.bat`) |
| `python3: command not found` | Windows pe `python` likho, ya Python reinstall karke "Add to PATH" tick karo |
| `permission denied: ./run.sh` | `chmod +x run.sh` chalao |
| Windows pe script block ho rahi hai | PowerShell ki jagah **Command Prompt (cmd)** use karo |
| Page khali / UI nahi dikh raha | `static/index.html` missing hai. Dobara download karo |
| `http://localhost:8000` nahi khulta | `file://` se index.html mat kholo — backend chahiye |
| `401 Unauthorized` API pe | Header lagao: `-H "X-API-Key: aiflow-dev-key"`, ya `.env` me `AIFLOW_AUTH=0` |
| `429 Too Many Requests` | Rate limit. `AIFLOW_RATE_LIMIT=1000 ./run.sh` se chalao |
| Tests fail ho rahe hain | Server chal raha hai? API/canvas tests ko running server chahiye |
| Fresh start chahiye | `data/` folder delete kar do — seed workflows dobara ban jaayenge |
| `pip install` fail | Internet check karo. Proxy ho to `pip install --proxy http://proxy:port -r requirements.txt` |
| **Kuch bhi samajh na aaye** | `python3 doctor.py` chalao aur output bhej do |

---

## Data kahan rehta hai

Sab kuch **`aiflow/data/aiflow.db`** (SQLite) me — workflows, versions, run history,
schedules, API keys, approvals, alerts, cache, vectors.

- **Backup:** bas ye ek file copy kar lo
- **Reset:** `data/` folder delete kar do

---

## Network pe dusre device se access

Server pehle se `0.0.0.0` pe bind hai, to same WiFi pe phone/dusre laptop se bhi khulega:

```bash
ipconfig getifaddr en0      # Mac
hostname -I                 # Linux
ipconfig                    # Windows
```
Phir dusre device pe `http://<wo-IP>:8000` kholo. Firewall allow karna pad sakta hai.

> **Dhyan rahe:** ye setup local development ke liye hai. Internet pe expose karna ho to
> `.env` me `AIFLOW_WEBHOOK_SECRET` badlo, `AIFLOW_AUTH=1` rakho, naye API keys banao
> (Admin tab se), aur aage HTTPS reverse proxy (nginx/Caddy) lagao.

---

## Aage kya padho

- **`README.md`** — saare features, 16 node types, poori API reference
- **`PROJECT_LOG.md`** — kya kaise bana, design decisions, aage ke ideas
- **Templates tab** — 8 ready-made workflows, har ek ek feature dikhata hai. Naya
  workflow banane se pehle inhe dekh lo

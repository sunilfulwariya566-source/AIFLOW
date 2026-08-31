# GitHub pe kaise daalein — bina git seekhe

Git commands confusing hain. **Aapko unki zaroorat nahi hai.** Sab kuch browser se drag-and-drop
karke ho jaayega.

Neeche do tarike hain. **Tarika A** sabse aasan hai — usi se karo.

---

# 🟢 TARIKA A — Browser se (koi command nahi)

Lagega ~10 minute. Sirf browser chahiye.

---

## Step 1 — Zip download karo

Workspace se **`aiflow-github.zip`** download kar lo — file list me sabse upar `/home/user/`
me milegi (folder ke andar nahi, uske bahar).

**35 files, 132 KB.** Isme sirf wahi hai jo GitHub pe jaana chahiye — aapki database
(`data/`) aur keys (`.env`) isme **nahi** hain, check kiya hua hai.

> Zip purani lage (naye features add hue ho) to `bash make_zip.sh` chala ke nayi bana lo.

## Step 2 — Zip ko kholo (extract)

- **Windows:** zip pe right-click → **"Extract All"** → Extract
- **Mac:** zip pe double-click

Ab ek **`aiflow`** naam ka folder ban jaayega. Usko khol ke dekho — andar `app.py`,
`engine.py`, `static` folder waghera dikhne chahiye.

> ⚠️ Ye folder khula rakhna, Step 5 me ismein se files uthani hain.

## Step 3 — GitHub account

Account nahi hai to [github.com/signup](https://github.com/signup) pe bana lo — 2 minute,
free hai.

## Step 4 — Naya repo banao

[github.com/new](https://github.com/new) pe jao aur bharo:

| Field | Kya daalein |
|---|---|
| **Repository name** | `aiflow` |
| **Description** | `A DAG workflow engine for LLM automation — visual builder, budgets, 441 tests` |
| **Public / Private** | **Public** chuno |
| Add a README file | ❌ **tick mat karo** |
| Add .gitignore | ❌ **None hi rehne do** |
| Choose a license | ❌ **None hi rehne do** |

> Ye teen cheezein isliye tick nahi karni kyunki humare paas already hain — warna clash hoga.

Neeche **"Create repository"** dabao.

## Step 5 — Files upload karo (yahi asli kaam hai)

Ab jo page khulega usme likha hoga *"Quick setup — if you've done this kind of thing before"*.

Us page pe ek line milegi: **"uploading an existing file"** — us **link pe click karo**.

(Direct link bhi kaam karega: `github.com/TUMHARA-USERNAME/aiflow/upload/main`)

Ab upload page khulega. Yahan:

1. Apna **`aiflow` folder** kholo (jo Step 2 me extract kiya tha)
2. Andar ki **saari files select karo** — `Ctrl+A` (Mac pe `Cmd+A`)
3. Unhe **drag karke browser wale box me chhod do**

⏳ 30-60 second lagega upload hone me.

### ⚠️ Ek zaroori baat

Kuch files ka naam **dot (.)** se shuru hota hai — jaise `.gitignore`, `.env.example`,
`.github`. Ye aapke computer pe **chhupi hui (hidden)** ho sakti hain, aur select nahi hongi.

Inhe dikhane ke liye:

- **Windows:** File Explorer me upar **View** tab → **"Hidden items"** tick karo
- **Mac:** folder me `Cmd + Shift + .` dabao

Phir dobara `Ctrl+A` karke drag karo.

> Agar `.github` folder chhoot gaya to bas CI badge nahi chalega — baaki sab theek rahega.
> Baad me bhi add kar sakte ho.

## Step 6 — Commit karo

Upload hone ke baad neeche scroll karo:

- Box me likho: `AIFlow — DAG workflow engine for LLM automation`
- **"Commit changes"** button dabao

✅ **Ho gaya!** Aapka project ab live hai:
`https://github.com/TUMHARA-USERNAME/aiflow`

---

## Step 7 — Aakhri touch (2 minute)

### a) Clone link me apna username daalo

README me abhi `YOUR_USERNAME` likha hai. Theek karne ke liye:

1. Repo pe `README.md` file pe click karo
2. Upar **pencil (✏️) icon** dabao
3. `Ctrl+F` se `YOUR_USERNAME` dhundo, apna username likh do
4. Neeche **"Commit changes"**

### b) Repo ko dikhne layak banao

Repo page pe right side **"About"** ke paas ⚙️ gear icon dabao:

**Topics** me ye daal do (search me help karta hai):
```
llm  workflow-engine  ai-automation  dag  fastapi  python  llmops  agents  rag
```

Save dabao.

### c) Check karo sab theek hai

- [ ] README me **diagram dikh raha hai**?
- [ ] `data` folder **nahi** dikhna chahiye (usme aapki run history thi)
- [ ] `.env` **nahi** dikhna chahiye
- [ ] **Actions** tab me green tick (agar `.github` upload hua ho)

---

---

# 🔵 TARIKA B — GitHub Desktop app se

Agar aage bhi code update karte rehna hai to ye behtar hai. Bhi bina commands ke.

1. [desktop.github.com](https://desktop.github.com) se **GitHub Desktop** install karo
2. Kholo → **Sign in to GitHub.com** → login karo
3. **File → Add Local Repository** → apna `aiflow` folder chuno
4. "This directory does not appear to be a Git repository" aaye to
   **"create a repository"** pe click karo → **Create Repository** dabao
5. Left side me saari files dikhengi. Neeche box me likho `AIFlow` → **Commit to main**
6. Upar **"Publish repository"** dabao
   - Name: `aiflow`
   - **"Keep this code private" ka tick HATA do**
   - **Publish Repository**

Ho gaya. Aage jab bhi code badlo — app kholo, commit karo, **Push origin** dabao.

---

---

# 🟠 TARIKA C — Commands se (agar aapko git aata hai)

```bash
cd ~/Desktop/aiflow
git init
git add .
git commit -m "AIFlow — DAG workflow engine for LLM automation"
git branch -M main
git remote add origin https://github.com/TUMHARA-USERNAME/aiflow.git
git push -u origin main
```

Password maange to GitHub password **kaam nahi karega** — [Personal Access Token](https://github.com/settings/tokens)
banana padega (`repo` scope wala).

---

---

# Kuch atak jaaye to

| Problem | Hal |
|---|---|
| Files drag nahi ho rahi | Ek saath 100 se kam files honi chahiye — humare paas 34 hain, theek hai. Browser refresh karke dobara try karo |
| `.gitignore` / `.github` nahi dikh rahi | Hidden files ON karo (Step 5 me likha hai) |
| "Repository already exists" | Us naam ka repo pehle se hai. `aiflow-engine` jaisa dusra naam le lo |
| README me diagram nahi dikh raha | `docs` folder upload nahi hua. Usko alag se drag kar do |
| Galti se `data` folder chadh gaya | Repo me us folder pe jao → 🗑️ delete icon → Commit. Ya poora repo delete karke dobara karo |
| Password kaam nahi kar raha (Tarika C) | GitHub ab password accept nahi karta. Token banao ya Tarika A/B use karo |

---

---

# Publish ke baad — resume ke liye

LinkedIn/CV pe aise likho (feature list nahi, **problem aur scale**):

> **AIFlow** — DAG workflow engine for LLM automation (Python, FastAPI, SQLite)
> Built a production-shaped orchestration engine: dependency-inferred execution graph,
> AST-sandboxed expressions, per-node retries with backoff, hard cost ceilings enforced
> mid-run, human approval gates with pause/resume, token streaming over SSE, and a
> drag-and-drop canvas built without external libraries. 441 automated tests.

Interview me ye teen cheezein sabse impressive hain — inhe samajh lo:

1. **AST sandbox** (`sandbox.py`) — `eval()` khatarnak kyun hai, whitelist approach kya hai,
   aur CPU/memory bombs kaise roke
2. **Budget enforcement** — har LLM call ke *baad* check hota hai (run ke baad nahi),
   aur sub-workflows parent ka bacha hua budget inherit karte hain
3. **Skip propagation** — node tabhi skip hota hai jab uski *saari* dependencies skip hui
   hon, warna join node gayab ho jaata hai

`PROJECT_LOG.md` me saare design decisions aur wo 10 bugs likhe hain jo tests ne pakde —
interview se pehle ek baar padh lena. Ye baat karne layak cheezein hain.

# Library Page Route Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the PWA library off the Ask screen onto `GET /library`, leaving Ask as Q&A with a shared top nav.

**Architecture:** Keep the existing `/api/*` contract, rebuild lock, and two-stage ingest. Add two FileResponse routes (`/library`, `/app.css`). Split the current one-page UI into `index.html` (Ask + WebSocket) and `library.html` (list / upload / delete / Re-index). Share theme and nav through `static/app.css`. Bump the service-worker cache so both pages and the CSS are precached. Do not change the RAG brain.

**Tech Stack:** Python 3.12, FastAPI FileResponse, vanilla HTML/CSS/JS, existing Web App Manifest + service worker. No Jinja, no JS router, no extra deps.

**Spec:** `docs/superpowers/specs/2026-09-18-library-page-design.md`

## Global Constraints

- Commands run from `src/` with `./.venv/bin/python` (Python 3.12).
- This repo has **no pytest / no test runner**. Verify with `python -c`, FastAPI `TestClient`, `eval.py`, and a browser against `server.py`. Do not add pytest.
- Two HTML pages, real URLs: `GET /` → `index.html`, `GET /library` → `library.html`. Unknown paths stay 404.
- Shared top nav on both pages: **Ask** (`/`) and **Library** (`/library`). Current page is marked. **Ask** is how you return to the main page. No extra back button.
- Re-index lives on the library page only. Ask has no upload, delete, or Re-index controls.
- Shared stylesheet `static/app.css` for theme and nav. Inline scripts stay in each HTML file. No Jinja, no JS router, no extra deps.
- File list max-height `16rem` with `overflow-y: auto`. Upload and Re-index sit below the list, not inside the scroller.
- PWA `start_url` stays `/`. Do not edit `static/manifest.webmanifest`.
- Service worker cache name `ask-northwind-v4`. Precache `/`, `/library`, `/app.css`, `/manifest.webmanifest`, `/sw.js`, `/icons/icon-192.png`, `/icons/icon-512.png`. Never cache `/ws` or `/api/*`.
- Rebuild lock is still process-wide. Ask disables itself from `GET /api/status` (and from the existing WebSocket rebuild error) even though Re-index is on the other page.
- Two-stage ingest, markdown-only uploads, and `/api/*` handlers stay as `2026-09-18-pwa-library-reindex-design.md` defined them. Do not change `chunk.py`, `build_index.py`, `retrieve.py`, `generate.py`, `llm.py`, `library.py`, or the `/api/*` / `/ws` handlers.
- No auth. Chat history, auto-reindex, extra HTML routes beyond `/library`, and always-on Ask status polling while idle stay out of scope.
- The spec **overrides** the current `AGENTS.md` line that forbids a second HTML route. Task 4 updates that file; Tasks 1–3 must still add `/library`.
- Do not rewrite `docs/superpowers/specs/2026-09-18-pwa-library-reindex-design.md`.
- Do not commit uploaded probe files or `index/`.
- TestClient runs FastAPI lifespan → `_load()`. A local `index/chunks.faiss` is required (`python build_index.py` once if missing). First load can be slow.

## File map

| File | Responsibility |
|------|----------------|
| Create: `src/static/app.css` | Shared theme, header, nav, panels, buttons, `#doc-list` scroller |
| Create: `src/static/library.html` | Library UI + nav; upload/delete/Re-index; no WebSocket |
| Modify: `src/server.py` | Add `GET /library` and `GET /app.css` only |
| Modify: `src/static/index.html` | Ask UI + nav; drop `#library`; rebuild lock from `/api/status` + WS error |
| Modify: `src/static/sw.js` | Cache name `ask-northwind-v4`; precache `/library` and `/app.css` |
| Modify: `AGENTS.md`, `src/README.md` | Ask at `/`, Library at `/library`; drop “no second HTML route” |
| Unchanged | `chunk.py`, `build_index.py`, `retrieve.py`, `generate.py`, `llm.py`, `library.py`, `manifest.webmanifest`, `/api/*` handlers |

---

### Task 1: Shared stylesheet, `/library` page, and static routes

**Files:**
- Create: `src/static/app.css`
- Create: `src/static/library.html`
- Modify: `src/server.py` (add two routes next to `GET /`)
- Modify: `src/static/index.html` (replace the inline `<style>` block with a stylesheet link; leave the library panel on Ask until Task 2)

**Interfaces:**
- Consumes: existing `STATIC_DIR`, `FileResponse`, current Ask CSS tokens (`--bg`, `--paper`, `--ink`, `--muted`, `--line`, `--brass`, `--brass-dim`, `--ok`, `--err`)
- Produces:
  - `GET /library` → `FileResponse(STATIC_DIR / "library.html")`
  - `GET /app.css` → `FileResponse(STATIC_DIR / "app.css", media_type="text/css")`
  - CSS classes/ids: `.site-header`, `nav a.active`, `#doc-list` (`max-height: 16rem; overflow-y: auto`), `#lib-status`, `#upload`, `#reindex`, `#doc-file`
  - `library.html` header: product name **Ask Northwind** links to `/`; nav **Ask** → `/`, **Library** → `/library` with `class="active"` on Library
  - Library IDs used by later tasks: `lib-status`, `doc-list`, `upload`, `doc-file`, `reindex`

Do not add redirects for `/library/` or `/library.html`. Do not change `api_status`, `api_docs`, `api_upload`, `api_delete`, `api_reindex`, or `ws_ask`.

- [ ] **Step 1: Write the failing TestClient check**

From `src/`:

```bash
cd src
./.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
import server

with TestClient(server.app) as c:
    css = c.get("/app.css")
    assert css.status_code == 200, css.text
    assert "text/css" in css.headers.get("content-type", "")
    lib = c.get("/library")
    assert lib.status_code == 200, lib.text
    body = lib.text
    assert 'id="doc-list"' in body
    assert "Upload .md" in body
    assert "Re-index" in body
    assert 'id="ask-form"' not in body
    assert "/ws" not in body
print("library routes ok")
PY
```

- [ ] **Step 2: Run it to make sure it fails**

Expected: FAIL. Today FastAPI has no `/app.css` or `/library`, so both GETs are **404**.

- [ ] **Step 3: Create `src/static/app.css`**

Move every rule out of the current `index.html` `<style>` block. Add header/nav and the file-list scroller. Exact file:

```css
:root {
  --bg: #141311;
  --paper: #1e1c18;
  --ink: #f3efe6;
  --muted: #9c9588;
  --line: #3d382f;
  --brass: #c9a36a;
  --brass-dim: #8a7048;
  --ok: #8fbfa8;
  --err: #d27a6a;
}
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body {
  font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  background:
    radial-gradient(1200px 500px at 10% -10%, #2a2418 0%, transparent 50%),
    var(--bg);
  color: var(--ink);
  line-height: 1.45;
}
main {
  max-width: 720px;
  margin: 0 auto;
  padding: 2.5rem 1.25rem 4rem;
}
h1 {
  font-family: Georgia, "Iowan Old Style", "Palatino Linotype", serif;
  font-weight: 600;
  font-size: 2rem;
  letter-spacing: -0.02em;
  margin: 0;
}
.site-header {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 1rem;
  flex-wrap: wrap;
  margin: 0 0 0.35rem;
}
.site-header h1 a {
  color: inherit;
  text-decoration: none;
}
.site-header + .panel {
  margin-top: 1.4rem;
}
nav {
  display: flex;
  gap: 1.25rem;
}
nav a {
  color: var(--muted);
  text-decoration: none;
  font-weight: 600;
  font-size: 0.95rem;
  padding-bottom: 0.15rem;
  border-bottom: 2px solid transparent;
}
nav a.active {
  color: var(--brass);
  border-bottom-color: var(--brass);
}
.lede {
  color: var(--muted);
  margin: 0 0 1.75rem;
  max-width: 40em;
}
form {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 1.5rem;
}
input[type="text"] {
  flex: 1;
  background: var(--paper);
  border: 1px solid var(--line);
  color: var(--ink);
  padding: 0.7rem 0.85rem;
  font: inherit;
  border-radius: 4px;
}
input[type="text"]:focus {
  outline: 2px solid var(--brass);
  outline-offset: 1px;
}
button {
  font: inherit;
  background: var(--brass);
  color: #1a140c;
  border: 0;
  padding: 0.7rem 1.1rem;
  border-radius: 4px;
  font-weight: 600;
  cursor: pointer;
}
button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
button.ghost {
  background: transparent;
  color: var(--brass);
  border: 1px solid var(--brass-dim);
  padding: 0.2rem 0.6rem;
  font-weight: 500;
  font-size: 0.85rem;
}
.panel {
  background: var(--paper);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 1rem 1.1rem;
  margin-bottom: 1rem;
}
.panel h2 {
  margin: 0 0 0.7rem;
  font-size: 0.72rem;
  text-transform: uppercase;
  letter-spacing: 0.08em;
  color: var(--muted);
  font-weight: 600;
}
#status {
  color: var(--ok);
  min-height: 1.3em;
  margin: 0 0 0.75rem;
}
#status.idle { color: var(--muted); }
#status.err { color: var(--err); }
#chunks {
  display: grid;
  gap: 0.65rem;
}
.chunk {
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 0.65rem 0.75rem;
}
.chunk .meta {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.78rem;
  color: var(--brass);
  margin-bottom: 0.3rem;
}
.chunk .preview {
  color: var(--muted);
  font-size: 0.9rem;
}
#answer {
  white-space: pre-wrap;
  min-height: 4rem;
  margin: 0;
  font-size: 1.02rem;
}
footer {
  display: flex;
  align-items: center;
  gap: 0.75rem;
  color: var(--muted);
  font-size: 0.85rem;
  margin-top: 0.5rem;
}
#conn::before {
  content: "";
  display: inline-block;
  width: 0.5rem;
  height: 0.5rem;
  border-radius: 50%;
  background: var(--muted);
  margin-right: 0.4rem;
}
#conn.connected::before { background: var(--ok); }
#conn.disconnected::before, #conn.error::before { background: var(--err); }
#reconnect { display: none; }
#reconnect.show { display: inline-block; }
.row {
  display: flex;
  justify-content: space-between;
  gap: 0.75rem;
  align-items: center;
  padding: 0.45rem 0;
  border-bottom: 1px solid var(--line);
  font-size: 0.9rem;
}
.row:last-child { border-bottom: 0; }
.state { color: var(--muted); font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; }
.state.not_indexed { color: var(--brass); }
.state.missing_on_disk { color: var(--err); }
.lib-actions { display: flex; gap: 0.5rem; flex-wrap: wrap; margin: 0.75rem 0 0; align-items: center; }
#lib-status { min-height: 1.3em; margin: 0 0 0.5rem; color: var(--muted); font-size: 0.9rem; }
#lib-status.err { color: var(--err); }
#lib-status.ok { color: var(--ok); }
#doc-file { display: none; }
#doc-list {
  max-height: 16rem;
  overflow-y: auto;
}
@media (min-width: 640px) {
  #chunks { grid-template-columns: 1fr 1fr; }
}
```

- [ ] **Step 4: Add the two FileResponse routes in `src/server.py`**

Immediately after the existing `GET /` handler:

```python
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/library")
def library_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "library.html")


@app.get("/app.css")
def app_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.css", media_type="text/css")
```

Leave `/manifest.webmanifest`, `/sw.js`, `/icons/{name}`, `/api/*`, and `/ws` as they are.

- [ ] **Step 5: Create `src/static/library.html`**

Full file. No Ask form, no `/ws`, no connection footer. Register the service worker. On load: `GET /api/status` then `GET /api/docs`. Poll `/api/status` every 1s only while `rebuilding` is true.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Library · Ask Northwind</title>
  <meta name="theme-color" content="#c9a36a" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-title" content="Ask Northwind" />
  <link rel="manifest" href="/manifest.webmanifest" />
  <link rel="apple-touch-icon" href="/icons/icon-192.png" />
  <link rel="stylesheet" href="/app.css" />
</head>
<body>
  <main>
    <header class="site-header">
      <h1><a href="/">Ask Northwind</a></h1>
      <nav>
        <a href="/">Ask</a>
        <a href="/library" class="active">Library</a>
      </nav>
    </header>

    <section class="panel" id="library">
      <h2>Library</h2>
      <p id="lib-status">Loading documents…</p>
      <div id="doc-list"></div>
      <div class="lib-actions">
        <button type="button" id="upload">Upload .md</button>
        <input id="doc-file" type="file" accept=".md,text/markdown" multiple />
        <button type="button" id="reindex">Re-index</button>
      </div>
    </section>
  </main>
  <script>
    const libStatus = document.getElementById("lib-status");
    const docList = document.getElementById("doc-list");
    const uploadBtn = document.getElementById("upload");
    const fileInput = document.getElementById("doc-file");
    const reindexBtn = document.getElementById("reindex");
    const STATE_LABEL = {
      indexed: "indexed",
      not_indexed: "not indexed",
      missing_on_disk: "missing on disk",
    };

    let rebuilding = false;

    function isOffline() {
      return !navigator.onLine;
    }

    function setLibStatus(text, kind) {
      libStatus.textContent = text;
      libStatus.className = kind || "";
    }

    function setRebuildLock(locked) {
      rebuilding = locked;
      uploadBtn.disabled = locked;
      reindexBtn.disabled = locked;
      docList.querySelectorAll("button").forEach((b) => { b.disabled = locked; });
      reindexBtn.textContent = locked ? "Rebuilding…" : "Re-index";
    }

    let pollTimer = null;

    function stopRebuildPoll() {
      if (pollTimer === null) return;
      clearTimeout(pollTimer);
      pollTimer = null;
    }

    function startRebuildPoll() {
      if (pollTimer !== null) return;
      pollTimer = setTimeout(pollRebuild, 1000);
    }

    async function pollRebuild() {
      pollTimer = null;
      try {
        const res = await fetch("/api/status");
        const body = await res.json().catch(() => ({}));
        if (body.rebuilding) {
          setRebuildLock(true);
          setLibStatus("Rebuilding…");
          startRebuildPoll();
          return;
        }
        setRebuildLock(false);
        await refreshLibrary();
      } catch (err) {
        setLibStatus("You're offline.", "err");
        startRebuildPoll();
      }
    }

    function renderDocs(docs) {
      docList.replaceChildren();
      if (!docs.length) {
        const empty = document.createElement("p");
        empty.className = "preview";
        empty.textContent = "No markdown files in docs/.";
        docList.appendChild(empty);
        return;
      }
      for (const doc of docs) {
        const row = document.createElement("div");
        row.className = "row";
        const left = document.createElement("div");
        left.textContent = doc.name;
        const state = document.createElement("span");
        state.className = "state " + doc.state;
        state.textContent = STATE_LABEL[doc.state] || doc.state;
        const del = document.createElement("button");
        del.type = "button";
        del.className = "ghost";
        del.textContent = "Delete";
        del.disabled = rebuilding;
        del.addEventListener("click", () => removeDoc(doc.name));
        row.append(left, state, del);
        if (doc.state === "missing_on_disk") del.hidden = true;
        docList.appendChild(row);
      }
    }

    async function refreshLibrary() {
      if (isOffline()) {
        setLibStatus("You're offline.", "err");
        return;
      }
      try {
        const res = await fetch("/api/docs");
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          setLibStatus(body.detail || "Could not load documents.", "err");
          return;
        }
        if (body.rebuilding) {
          setRebuildLock(true);
          renderDocs(body.docs || []);
          setLibStatus("Rebuilding…");
          startRebuildPoll();
          return;
        }
        stopRebuildPoll();
        setRebuildLock(false);
        renderDocs(body.docs || []);
        const n = body.num_chunks;
        setLibStatus(n != null ? `${body.docs.length} files · ${n} chunks indexed` : "Ready");
      } catch (err) {
        setLibStatus("You're offline.", "err");
      }
    }

    async function removeDoc(name) {
      if (!confirm(`Delete ${name} from disk? Re-index afterward to update search.`)) return;
      if (isOffline()) { setLibStatus("You're offline.", "err"); return; }
      try {
        const res = await fetch("/api/docs/" + encodeURIComponent(name), { method: "DELETE" });
        const body = await res.json().catch(() => ({}));
        if (res.status === 409) {
          setRebuildLock(true);
          setLibStatus("Index is rebuilding. Try again when it finishes.", "err");
          startRebuildPoll();
          return;
        }
        if (!res.ok) { setLibStatus(body.detail || "Delete failed", "err"); return; }
        renderDocs(body.docs);
        setLibStatus("Deleted " + name + ". Re-index to update search.");
      } catch (err) {
        setLibStatus("You're offline.", "err");
      }
    }

    uploadBtn.addEventListener("click", () => {
      if (isOffline()) { setLibStatus("You're offline.", "err"); return; }
      fileInput.click();
    });
    fileInput.addEventListener("change", async () => {
      const files = fileInput.files;
      if (!files || !files.length) return;
      if (isOffline()) { setLibStatus("You're offline.", "err"); return; }
      const fd = new FormData();
      for (const f of files) fd.append("files", f);
      try {
        const res = await fetch("/api/docs", { method: "POST", body: fd });
        fileInput.value = "";
        const body = await res.json().catch(() => ({}));
        if (res.status === 409) {
          setRebuildLock(true);
          setLibStatus("Index is rebuilding. Try again when it finishes.", "err");
          startRebuildPoll();
          return;
        }
        if (body.docs) renderDocs(body.docs);
        if (res.status === 207 || (body.errors && body.errors.length)) {
          const msg = (body.errors || []).map((e) => e.message).filter(Boolean).join(" ") || "Upload failed";
          setLibStatus(msg, "err");
          return;
        }
        if (!res.ok) {
          setLibStatus(body.detail || "Upload failed", "err");
          return;
        }
        setLibStatus("Saved. Re-index to update search.");
      } catch (err) {
        fileInput.value = "";
        setLibStatus("You're offline.", "err");
      }
    });

    reindexBtn.addEventListener("click", async () => {
      if (isOffline()) { setLibStatus("You're offline.", "err"); return; }
      setRebuildLock(true);
      setLibStatus("Rebuilding…");
      try {
        const res = await fetch("/api/reindex", { method: "POST" });
        const body = await res.json().catch(() => ({}));
        if (res.status === 409) {
          setLibStatus("Index is rebuilding. Try again when it finishes.", "err");
          startRebuildPoll();
          return;
        }
        setRebuildLock(false);
        await refreshLibrary();
        if (!res.ok) setLibStatus(body.detail || "Re-index failed", "err");
        else setLibStatus("Indexed " + body.num_chunks + " chunks.", "ok");
      } catch (err) {
        setLibStatus("You're offline.", "err");
        startRebuildPoll();
      }
    });

    window.addEventListener("offline", () => setLibStatus("You're offline.", "err"));

    async function init() {
      if (isOffline()) {
        setLibStatus("You're offline.", "err");
        return;
      }
      try {
        const statusRes = await fetch("/api/status");
        const status = await statusRes.json().catch(() => ({}));
        if (status.rebuilding) {
          setRebuildLock(true);
          setLibStatus("Rebuilding…");
          startRebuildPoll();
        }
      } catch (err) {
        setLibStatus("You're offline.", "err");
      }
      await refreshLibrary();
    }
    init();

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js");
    }
  </script>
</body>
</html>
```

Copy rules that must stay exact:

- Delete confirm: `Delete ${name} from disk? Re-index afterward to update search.`
- After save: `Saved. Re-index to update search.`
- After delete: `Deleted ` + name + `. Re-index to update search.`
- Rebuild button label: `Rebuilding…` / `Re-index`
- Rebuild status: `Rebuilding…`
- Success: `Indexed ` + `body.num_chunks` + ` chunks.`
- Offline: `You're offline.`
- Empty list: `No markdown files in docs/.`
- Hide Delete when `doc.state === "missing_on_disk"`
- Multipart field name: `files`

- [ ] **Step 6: Point Ask at the shared stylesheet**

In `src/static/index.html`, delete the entire `<style>…</style>` block in `<head>` and replace it with:

```html
  <link rel="stylesheet" href="/app.css" />
```

Leave the rest of `index.html` (including `#library`) unchanged in this task.

- [ ] **Step 7: Re-run the TestClient check and assert CSS + APIs**

```bash
cd src
./.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
import server

with TestClient(server.app) as c:
    css = c.get("/app.css")
    assert css.status_code == 200, css.text
    assert "text/css" in css.headers.get("content-type", "")
    assert "max-height: 16rem" in css.text
    assert ".site-header" in css.text
    assert "nav a.active" in css.text

    lib = c.get("/library")
    assert lib.status_code == 200, lib.text
    body = lib.text
    assert 'id="doc-list"' in body
    assert "Upload .md" in body
    assert "Re-index" in body
    assert 'href="/library" class="active"' in body
    assert 'id="ask-form"' not in body
    assert "/ws" not in body
    assert 'href="/app.css"' in body
    assert "manifest.webmanifest" in body
    assert "serviceWorker" in body

    home = c.get("/")
    assert home.status_code == 200
    assert 'href="/app.css"' in home.text
    assert "<style>" not in home.text

    unknown = c.get("/no-such-page")
    assert unknown.status_code == 404

    status = c.get("/api/status")
    assert status.status_code == 200
    assert "rebuilding" in status.json()

    docs = c.get("/api/docs")
    assert docs.status_code == 200
    names = [x["name"] for x in docs.json()["docs"]]
    assert "pto_policy.md" in names
print("library routes ok")
PY
```

Expected: `library routes ok`

- [ ] **Step 8: Commit**

```bash
git add src/static/app.css src/static/library.html src/server.py src/static/index.html
git commit -m "Serve a shared stylesheet and the /library page"
```

---

### Task 2: Ask page is Q&A only, with nav and rebuild lock

**Files:**
- Modify: `src/static/index.html` (replace the file)

**Interfaces:**
- Consumes: `GET /app.css`, `.site-header` / `nav a.active` from Task 1, `GET /api/status` `{rebuilding: bool, …}`, WebSocket error `{ "type": "error", "message": "Index is rebuilding. Try again when it finishes." }`
- Produces: Ask at `GET /` with shared header; no `#library` panel; rebuild lock via `setRebuildLock(locked)`, `startRebuildPoll()`, `pollRebuild()`, `checkRebuildOnLoad()`; constant `REBUILD_MSG = "Index is rebuilding. Try again when it finishes."`

Ask still opens `/ws`, still replaces the previous turn, still registers `/sw.js`. If the page loads idle (`rebuilding` false), do **not** poll. After a successful rebuild poll, unlock and set pipeline status to `Ready` (class `idle`). After a rebuild HTTP failure the server already dropped the lock; Ask unlocks when `/api/status` reports `rebuilding: false`. Ask has no library status line.

- [ ] **Step 1: Write the failing TestClient check**

```bash
cd src
./.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
import server

with TestClient(server.app) as c:
    html = c.get("/").text
    assert 'id="ask-form"' in html
    assert 'id="question"' in html
    assert "/ws" in html
    assert 'href="/library"' in html
    assert 'href="/" class="active"' in html
    assert 'id="library"' not in html
    assert "Re-index" not in html
    assert "Upload .md" not in html
    lib = c.get("/library").text
    assert "Re-index" in lib
    assert 'id="ask-form"' not in lib
print("ask page ok")
PY
```

- [ ] **Step 2: Run it to make sure it fails**

Expected: FAIL on `id="library"` / `Re-index` / `Upload .md` still present in `GET /`, and/or missing `href="/" class="active"`.

- [ ] **Step 3: Replace `src/static/index.html` with the Ask-only page**

Full file. Product name is the existing `h1` serif, linking to `/`. It is not a third nav item. No second page title under the header. Keep the lede, form, pipeline, answer, footer.

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Ask Northwind</title>
  <meta name="theme-color" content="#c9a36a" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-title" content="Ask Northwind" />
  <link rel="manifest" href="/manifest.webmanifest" />
  <link rel="apple-touch-icon" href="/icons/icon-192.png" />
  <link rel="stylesheet" href="/app.css" />
</head>
<body>
  <main>
    <header class="site-header">
      <h1><a href="/">Ask Northwind</a></h1>
      <nav>
        <a href="/" class="active">Ask</a>
        <a href="/library">Library</a>
      </nav>
    </header>
    <p class="lede">Internal assistant for Northwind Retail Co. Answers come only from company docs — you will see which excerpts were retrieved before the model writes.</p>

    <form id="ask-form">
      <input id="question" type="text" autocomplete="off"
             placeholder="How many PTO days do I get per year?" />
      <button type="submit" id="ask">Ask</button>
    </form>

    <section class="panel" aria-live="polite">
      <h2>Pipeline</h2>
      <p id="status" class="idle">Ready</p>
      <div id="chunks"></div>
    </section>

    <section class="panel">
      <h2>Answer</h2>
      <p id="answer"></p>
    </section>

    <footer>
      <span id="conn">disconnected</span>
      <button type="button" class="ghost" id="reconnect">Reconnect</button>
    </footer>
  </main>
  <script>
    const form = document.getElementById("ask-form");
    const input = document.getElementById("question");
    const askBtn = document.getElementById("ask");
    const statusEl = document.getElementById("status");
    const chunksEl = document.getElementById("chunks");
    const answerEl = document.getElementById("answer");
    const connEl = document.getElementById("conn");
    const reconnectBtn = document.getElementById("reconnect");

    let ws = null;
    let answering = false;
    let rebuilding = false;
    const REBUILD_MSG = "Index is rebuilding. Try again when it finishes.";

    const STAGE = {
      retrieving: "Searching documents…",
      generating: "Writing answer…",
    };

    function setConn(state) {
      connEl.className = state;
      connEl.textContent = state;
      reconnectBtn.classList.toggle("show", state !== "connected");
    }

    function setBusy(busy) {
      answering = busy;
      input.disabled = busy || rebuilding;
      askBtn.disabled = busy || rebuilding;
    }

    function setStatus(text, kind) {
      statusEl.textContent = text;
      statusEl.className = kind || "";
    }

    function setRebuildLock(locked) {
      rebuilding = locked;
      input.disabled = locked || answering;
      askBtn.disabled = locked || answering;
      if (locked) setStatus(REBUILD_MSG, "err");
    }

    let pollTimer = null;

    function stopRebuildPoll() {
      if (pollTimer === null) return;
      clearTimeout(pollTimer);
      pollTimer = null;
    }

    function startRebuildPoll() {
      if (pollTimer !== null) return;
      pollTimer = setTimeout(pollRebuild, 1000);
    }

    async function pollRebuild() {
      pollTimer = null;
      try {
        const res = await fetch("/api/status");
        const body = await res.json().catch(() => ({}));
        if (body.rebuilding) {
          setRebuildLock(true);
          startRebuildPoll();
          return;
        }
        setRebuildLock(false);
        setStatus("Ready", "idle");
      } catch (err) {
        startRebuildPoll();
      }
    }

    async function checkRebuildOnLoad() {
      try {
        const res = await fetch("/api/status");
        const body = await res.json().catch(() => ({}));
        if (body.rebuilding) {
          setRebuildLock(true);
          startRebuildPoll();
        }
      } catch (err) {
        // Idle or offline: do not poll.
      }
    }

    function connect() {
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
        return;
      }
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => setConn("connected");
      ws.onerror = () => setConn("error");
      ws.onclose = () => {
        setConn("disconnected");
        setBusy(false);
        if (!rebuilding) setStatus("Ready", "idle");
      };
      ws.onmessage = (ev) => handle(JSON.parse(ev.data));
    }

    function handle(event) {
      if (event.type === "status") {
        setStatus(STAGE[event.stage] || event.stage);
        return;
      }
      if (event.type === "sources") {
        chunksEl.replaceChildren();
        if (!event.chunks.length) {
          const empty = document.createElement("p");
          empty.className = "preview";
          empty.textContent = "No matching excerpts.";
          chunksEl.appendChild(empty);
          return;
        }
        for (const chunk of event.chunks) {
          const card = document.createElement("article");
          card.className = "chunk";
          const meta = document.createElement("div");
          meta.className = "meta";
          meta.textContent = `[${chunk.n}] ${chunk.source_file}`;
          const preview = document.createElement("div");
          preview.className = "preview";
          preview.textContent = chunk.preview;
          card.append(meta, preview);
          chunksEl.appendChild(card);
        }
        return;
      }
      if (event.type === "token") {
        answerEl.textContent += event.text;
        return;
      }
      if (event.type === "done") {
        setBusy(false);
        setStatus("Ready", "idle");
        input.focus();
        return;
      }
      if (event.type === "error") {
        setBusy(false);
        if (event.message === REBUILD_MSG) {
          setRebuildLock(true);
          startRebuildPoll();
          return;
        }
        setStatus(event.message, "err");
        input.focus();
      }
    }

    form.addEventListener("submit", (e) => {
      e.preventDefault();
      const question = input.value.trim();
      if (!question || answering) return;
      if (!ws || ws.readyState !== WebSocket.OPEN) {
        setStatus("Not connected. Use Reconnect.", "err");
        return;
      }
      chunksEl.replaceChildren();
      answerEl.textContent = "";
      setStatus("Sending…");
      setBusy(true);
      ws.send(JSON.stringify({ question }));
    });

    reconnectBtn.addEventListener("click", connect);
    connect();
    checkRebuildOnLoad();
    input.focus();

    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js");
    }
  </script>
</body>
</html>
```

Do not leave any `libStatus` / `docList` / `uploadBtn` / `reindexBtn` / `refreshLibrary` references on this page.

- [ ] **Step 4: Re-run the TestClient check**

Same script as Step 1.

Expected: `ask page ok`

- [ ] **Step 5: Browser check (required; TestClient does not click nav)**

Start the server if it is not running:

```bash
cd src
./.venv/bin/python server.py
```

Open `http://127.0.0.1:8000` in a real browser.

1. Header shows **Ask Northwind** on the left and **Ask** / **Library** on the right. **Ask** has the brass underline. There is no Library panel, no Upload, no Re-index, no extra Back button.
2. Click **Library**. URL becomes `/library`. File list, **Upload .md**, and **Re-index** are present. **Library** has the brass underline. There is no question form and no connection footer.
3. Click **Ask** (and separately the product name). Both return to `/`.
4. Narrow the viewport (~375px). Header wraps; nav stays visible. Repeat at desktop width.
5. On Library, confirm `#doc-list` is the scroller (Upload and Re-index stay below it). If the corpus is short, upload several unique `*.md` files until the list exceeds `16rem`; the list scrolls, Upload/Re-index stay on screen. Delete those extras afterward or leave them unindexed — do not commit them.
6. Two-stage ingest: upload a unique-fact `zz_upload_probe.md` on Library → switch to Ask → question about that fact should not cite it → Library **Re-index** → Ask cites `zz_upload_probe.md`. Delete the probe → Ask can still cite it → Re-index → Ask does not.
7. Rebuild lock across pages: click **Re-index** on Library, immediately click **Ask**. Question input and Ask button are disabled; pipeline reads `Index is rebuilding. Try again when it finishes.` When rebuild finishes, Ask unlocks and status returns to Ready. Do not leave a status poll running after that.

If any of those fail, fix `index.html` / `library.html` / `app.css` and re-run Steps 4–5 before committing.

- [ ] **Step 6: Commit**

```bash
git add src/static/index.html
git commit -m "Move library controls off the Ask page"
```

---

### Task 3: Precache `/library` and `/app.css` (`ask-northwind-v4`)

**Files:**
- Modify: `src/static/sw.js`

**Interfaces:**
- Consumes: `GET /`, `GET /library`, `GET /app.css` from Tasks 1–2
- Produces: `CACHE = "ask-northwind-v4"`; `SHELL` includes `/`, `/library`, `/app.css`, `/manifest.webmanifest`, `/sw.js`, `/icons/icon-192.png`, `/icons/icon-512.png`; fetch handler still returns immediately (network-only) for `/ws` and `/api/*`; `skipWaiting` + `clients.claim` stay

Do not change `static/manifest.webmanifest`. `start_url` remains `/`.

- [ ] **Step 1: Write the failing TestClient check**

```bash
cd src
./.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
import server

with TestClient(server.app) as c:
    js = c.get("/sw.js").text
    assert 'ask-northwind-v4' in js
    assert 'ask-northwind-v3' not in js
    for url in ["/", "/library", "/app.css", "/manifest.webmanifest", "/sw.js",
                "/icons/icon-192.png", "/icons/icon-512.png"]:
        assert f'"{url}"' in js, url
    assert 'url.pathname === "/ws"' in js
    assert 'url.pathname.startsWith("/api/")' in js
    assert "skipWaiting" in js
    assert "clients.claim" in js
    manifest = c.get("/manifest.webmanifest").json()
    assert manifest["start_url"] == "/"
print("sw v4 ok")
PY
```

- [ ] **Step 2: Run it to make sure it fails**

Expected: FAIL. Current `CACHE` is `ask-northwind-v3` and `SHELL` does not list `/library` or `/app.css`.

- [ ] **Step 3: Replace `src/static/sw.js`**

```javascript
const CACHE = "ask-northwind-v4";
const SHELL = [
  "/",
  "/library",
  "/app.css",
  "/manifest.webmanifest",
  "/sw.js",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET") return;
  if (url.pathname === "/ws" || url.pathname.startsWith("/api/")) return;
  event.respondWith(
    caches.match(event.request).then((hit) => {
      if (hit) return hit;
      return fetch(event.request).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        }
        return res;
      });
    })
  );
});
```

- [ ] **Step 4: Re-run the TestClient check**

Expected: `sw v4 ok`

- [ ] **Step 5: Browser / airplane-mode check**

With `./.venv/bin/python server.py` running at `http://127.0.0.1:8000`:

1. Hard-refresh `/` and `/library` so the new worker installs.
2. DevTools → Application → Cache Storage → `ask-northwind-v4` lists the seven `SHELL` URLs. Old `ask-northwind-v3` is gone after activate.
3. Toggle Offline (or airplane mode). Both `/` and `/library` still open from cache. Ask footer / pipeline uses `Not connected. Use Reconnect.` when you submit. Library status uses `You're offline.` Upload, delete, Re-index, and questions still fail without the network. `/api/*` and `/ws` are not served from the cache.

- [ ] **Step 6: Commit**

```bash
git add src/static/sw.js
git commit -m "Precache /library and /app.css in the PWA shell"
```

---

### Task 4: Document the two-page PWA

**Files:**
- Modify: `AGENTS.md`
- Modify: `src/README.md`

**Interfaces:**
- Consumes: Ask at `/`, Library at `/library`, Re-index on the library page, cache `ask-northwind-v4`
- Produces: agent and human docs that match the spec. Do **not** edit `docs/superpowers/specs/2026-09-18-pwa-library-reindex-design.md`.

- [ ] **Step 1: Update `AGENTS.md`**

Commands table — `server.py` row becomes:

```
| `./.venv/bin/python server.py` | Installable PWA at http://127.0.0.1:8000: Ask at `/` (WebSocket), Library at `/library` (upload/delete `docs/`), Re-index on the library page (`build()` + `retrieve.reload()`). Same LLM env as CLI. |
```

Architecture ascii — keep the CLI path; the `server.py` line can stay “PWA library writes docs/; Re-index → build() + retrieve.reload()”.

**Library and Re-index** — after the rebuild-lock bullets, replace:

```
Do not add auto-reindex on upload, a second HTML route, chat history, login, incremental FAISS, or filesystem watchers.
```

with:

```
Library is `GET /library` (`library.html`), not a panel on Ask. The only extra HTML route is `/library`. Shell layout is `docs/superpowers/specs/2026-09-18-library-page-design.md` (it supersedes the same-page UI in `2026-09-18-pwa-library-reindex-design.md`; do not rewrite that file). Do not add auto-reindex on upload, chat history, login, incremental FAISS, filesystem watchers, or further HTML routes.
```

**Key files** — replace the `server.py` / `index.html` / `sw.js` bullets with:

```
- `src/server.py` — FastAPI: `GET /`, `GET /library`, `GET /app.css`, `WS /ws`, `GET/POST/DELETE /api/docs`, `POST /api/reindex`, `GET /api/status`, PWA static routes
- `src/static/index.html` — Ask UI + top nav + service worker register (no library panel)
- `src/static/library.html` — Library UI: file list, upload, delete, Re-index
- `src/static/app.css` — shared theme, header, nav
- `src/static/manifest.webmanifest` — PWA install metadata (name “Ask Northwind”, standalone, `start_url` `/`)
- `src/static/sw.js` — caches the UI shell (`ask-northwind-v4`); precaches `/`, `/library`, `/app.css`; never `/ws` or `/api/*`. Bump the cache name when the shell changes.
```

**Coding conventions** — replace:

```
- Keep it framework-light. FastAPI is the existing PWA/WebSocket shell only. Do not add LangChain, Flask, a second HTML route, or extra deps unless the task needs them.
```

with:

```
- Keep it framework-light. FastAPI is the existing PWA/WebSocket shell only. Do not add LangChain, Flask, extra HTML routes beyond `/library`, or extra deps unless the task needs them.
```

**Gotchas** — PWA bullet becomes:

```
- PWA caches the shell only (`/`, `/library`, `/app.css`, manifest, sw, icons). Ask, upload, and re-index still need the server. Offline Library copy is “You're offline.”; Ask still uses “Not connected. Use Reconnect.”
```

- [ ] **Step 2: Update `src/README.md`**

**Setup** — replace the `server.py` comment block:

```
./.venv/bin/python server.py
# open http://127.0.0.1:8000
# Installable (Add to Home Screen). The page has a Library panel;
# Re-index rebuilds search from docs/. Upload does not change
# answers until Re-index.
```

with:

```
./.venv/bin/python server.py
# open http://127.0.0.1:8000          Ask
# open http://127.0.0.1:8000/library  Library (upload/delete/Re-index)
# Installable (Add to Home Screen). Re-index lives on the Library
# page and rebuilds search from docs/. Upload does not change
# answers until Re-index.
```

**Files table** — replace the `server.py` / `static/index.html` / `sw.js` rows with:

```
| `server.py` | FastAPI WebSocket shell + library REST + Re-index lock + `GET /library` |
| `static/index.html` | Ask UI + top nav + PWA registration |
| `static/library.html` | Library UI: list, upload, delete, Re-index |
| `static/app.css` | Shared theme and nav |
| `static/manifest.webmanifest` | Install metadata (Add to Home Screen) |
| `static/sw.js` | Cache the UI shell only (not `/ws` or `/api/*`) |
```

**Web UI** section — replace the first paragraph with:

```
`server.py` serves Ask at `/` (`static/index.html`) and Library at `/library` (`static/library.html`), plus a WebSocket at `/ws`. Ask sends `{ "question": "..." }` and renders events in order: retrieving → source excerpts → tokens → done. The RAG path is still retrieve-then-rerank-then-generate; the socket is only transport. Both pages share a top nav and `static/app.css`. The app is a PWA: Add to Home Screen installs it (`start_url` `/`); the service worker caches the shell, not answers.
```

**Library and re-index** section — replace the first paragraph with:

```
Library is a separate page at `/library`. It lists `docs/*.md`. Upload is markdown only (same filename overwrites). Delete removes the file from disk. There is no login — anyone who can open the app can change the corpus. Upload and delete do not change answers until you click **Re-index** on that page, which rebuilds FAISS from every `docs/*.md` and hot-reloads search. While it rebuilds, Ask (via `/api/status` / the WebSocket error) and upload/delete are locked. A long file list scrolls inside the list; Upload and Re-index stay on screen.
```

Keep the out-of-scope sentence. Do not rewrite `docs/superpowers/specs/2026-09-18-pwa-library-reindex-design.md`; that file’s same-page UI section is superseded by `docs/superpowers/specs/2026-09-18-library-page-design.md`.

- [ ] **Step 3: Run eval.py (UI-only change; confirm retrieval did not drift)**

```bash
cd src
./.venv/bin/python eval.py
```

Expected: script completes; recall @3 and @5 stay **100%**. Fail this task if @3 drops below 100%.

- [ ] **Step 4: Commit**

```bash
git add AGENTS.md src/README.md
git commit -m "Document Ask at / and Library at /library"
```

---

## Self-review

**Spec coverage**

| Spec section | Task |
|--------------|------|
| `GET /` Ask, `GET /library` Library, `GET /app.css` | 1, 2 |
| Shared header: brand → `/`, nav Ask/Library, `active` brass underline, wrap on narrow viewports | 1 (CSS + library.html), 2 (Ask) |
| No extra Back button; Ask / brand return to `/` | 1, 2 |
| Re-index / upload / delete only on Library; Ask has no `#library` | 2 |
| `#doc-list` `max-height: 16rem; overflow-y: auto`; Upload/Re-index below | 1 |
| Ask: lede, form, pipeline, answer, footer; SW; `/ws`; one-turn replace | 2 |
| Ask rebuild lock from `GET /api/status` + WS rebuild error; poll 1s only while rebuilding; idle load does not poll | 2 |
| Library: status, list, Upload, Re-index; no WS/form/footer; copy + hide Delete on `missing_on_disk` | 1 |
| Process-wide lock unchanged; `/api/*` unchanged | 1 (explicit non-edit) |
| Manifest `start_url` `/`; SW `ask-northwind-v4`; precache both pages + CSS; never `/ws` or `/api/*` | 3 |
| Offline: Ask “Not connected. Use Reconnect.”; Library “You're offline.” | 1, 2, 3 |
| Unknown paths 404 | 1 |
| TestClient + browser nav + two-stage ingest + cross-page lock | 1, 2 |
| Airplane-mode shells | 3 |
| `eval.py` @3 stays 100% | 4 |
| `AGENTS.md` / README; do not rewrite the 2026-09-18 reindex spec | 4 |
| Out of scope (auth, PDF, auto-reindex, JS router, Jinja, extra routes, always-on Ask poll) | Global constraints |

**Placeholder scan:** no TBD/TODO; verification commands are concrete; library.html / index.html / app.css / sw.js are inlined rather than “similar to Task N”.

**Type consistency:** `REBUILD_MSG` matches the existing WebSocket/HTTP copy `Index is rebuilding. Try again when it finishes.`; CSS `#doc-list` / `.site-header` / `nav a.active` used by both HTML files; cache name `ask-northwind-v4` in `sw.js` and docs; routes `/`, `/library`, `/app.css` in server, SW `SHELL`, and tests.

# PWA Library and Re-index Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Ask Northwind an installable PWA with a markdown library and a UI Re-index button that fully rebuilds FAISS and hot-reloads search.

**Architecture:** Keep `chunk.py` / `build_index.build()` / `retrieve.py` / `generate.py` as the RAG brain. Add `library.py` for `docs/` I/O. `build()` writes index artifacts via `index/.tmp/` then replaces. `retrieve.reload()` re-reads FAISS under a lock. FastAPI grows REST `/api/docs` + `/api/reindex` + `/api/status` behind a process-wide `rebuilding` flag. The existing `static/index.html` page gains a library panel, manifest, and service worker that caches the shell only.

**Tech Stack:** Python 3.12, FastAPI, uvicorn, python-multipart, existing sentence-transformers + faiss-cpu, one HTML page + vanilla JS, Web App Manifest + service worker.

**Spec:** `docs/superpowers/specs/2026-09-18-pwa-library-reindex-design.md`

## Global Constraints

- Commands run from `rag-demo/` with `./.venv/bin/python` (Python 3.12).
- This repo has **no pytest / no test runner**. Verify with `python -c`, FastAPI `TestClient`, `eval.py`, and curl against `server.py`. Do not add pytest.
- Markdown only: `*.md`. No PDF, Word, or `.txt`.
- Upload/delete write `docs/` only. Search changes only after `POST /api/reindex`.
- No auth. Filenames are basenames; reject `..`, `/`, `\`, NUL, non-`.md`.
- Max **1 MB** per file (`MAX_UPLOAD_BYTES = 1_000_000`), max **20 files** per request (`MAX_UPLOAD_FILES = 20`).
- Multipart field name is `files`.
- While rebuilding: Ask / upload / delete locked. HTTP **409**. WebSocket `{ "type": "error", "message": "Index is rebuilding. Try again when it finishes." }`.
- Empty `docs/` → `IndexBuildError("No chunks to index.")` → HTTP 400; live `index/` untouched.
- `build()` succeeds and `reload()` fails → HTTP 500 `"Index rebuilt on disk but failed to load. Restart the server."`
- PWA caches `/`, `/manifest.webmanifest`, `/sw.js`, `/icons/icon-192.png`, `/icons/icon-512.png`. Never cache `/ws` or `/api/*`.
- Do not add LangChain, a second HTML route, chat history, auto-reindex, or login.
- Imports stay flat (`from library import …`). Paths from `Path(__file__).parent`.
- Do not commit uploaded probe files or `index/`.

## File map

| File | Responsibility |
|------|----------------|
| Create: `rag-demo/library.py` | Safe names, list/save/delete markdown, index-vs-disk `state` |
| Modify: `rag-demo/build_index.py` | `IndexBuildError`; write `index/.tmp/` then replace; `indexed_at` + `files` in `config.json`; `build()` returns that config |
| Modify: `rag-demo/retrieve.py` | `reload()`; `_state_lock` around `_index`/`_chunks` |
| Modify: `rag-demo/server.py` | Static PWA routes, `/api/*`, rebuilding flag, wait for in-flight answers |
| Modify: `rag-demo/requirements.txt` | `python-multipart` |
| Modify: `rag-demo/static/index.html` | Manifest/SW registration, library panel, lock UI, offline copy |
| Create: `rag-demo/static/manifest.webmanifest` | Install metadata |
| Create: `rag-demo/static/sw.js` | Shell cache |
| Create: `rag-demo/static/icons/icon-192.png`, `icon-512.png` | PWA icons |
| Modify: `rag-demo/README.md`, `AGENTS.md` | How to use library + Re-index |

---

### Task 1: `library.py` — safe names and docs snapshot

**Files:**
- Create: `rag-demo/library.py`
- Test: none (run `python -c` from `rag-demo/`)

**Interfaces:**
- Consumes: `docs/*.md` on disk; `index/config.json` if present
- Produces:
  - `DOCS_DIR: Path`
  - `INDEX_DIR: Path`
  - `MAX_UPLOAD_BYTES = 1_000_000`
  - `MAX_UPLOAD_FILES = 20`
  - `class UnsafeNameError(ValueError)`
  - `class UploadError(ValueError)` — `args[0]` is a human message
  - `def safe_md_name(name: str) -> str`
  - `def index_meta(index_dir: Path | None = None) -> dict` — keys `indexed_at`, `num_chunks`, `files` (list[str]); missing config → `{"indexed_at": None, "num_chunks": None, "files": []}`
  - `def list_docs(docs_dir: Path | None = None, index_dir: Path | None = None) -> list[dict]` — each `{name, size, mtime, state}` sorted by `name`
  - `def save_upload(filename: str, data: bytes, docs_dir: Path | None = None) -> str`
  - `def delete_doc(name: str, docs_dir: Path | None = None) -> None` — `FileNotFoundError` if missing after a safe name

Defaults resolve **inside** the function (`docs_dir = docs_dir or DOCS_DIR`) so tests can patch `library.DOCS_DIR`.

`state`: `indexed` if name in `config.files`; `not_indexed` if on disk but not in `files` (including when `files` is missing/empty); `missing_on_disk` if in `files` but the file is gone.

`mtime` is UTC ISO-8601 from `stat().st_mtime`.

- [ ] **Step 1: Write `library.py` with the types above**

Module docstring should say why names are validated (path traversal / the chunker only reads `*.md`).

```python
"""Operate docs/ for the PWA library.

Upload and delete only touch markdown on disk. They do not rebuild FAISS —
search still reads index/ until someone calls build() + reload().
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

DOCS_DIR = Path(__file__).parent / "docs"
INDEX_DIR = Path(__file__).parent / "index"
MAX_UPLOAD_BYTES = 1_000_000
MAX_UPLOAD_FILES = 20

_UNSAFE = re.compile(r"[\\/]|^\.\.($|\.)|\x00")


class UnsafeNameError(ValueError):
    pass


class UploadError(ValueError):
    pass


def safe_md_name(name: str) -> str:
    base = Path(name).name
    if not base or base != name.replace("\\", "/").split("/")[-1]:
        # Path(name).name already strips directories; still reject if original had separators
        pass
    if "/" in name or "\\" in name or "\x00" in name or name in (".", "..") or ".." in Path(name).parts:
        raise UnsafeNameError("Unsafe filename.")
    base = Path(name).name
    if base.startswith(".") or not base.lower().endswith(".md") or base.lower() == ".md":
        raise UnsafeNameError("Only .md filenames are allowed.")
    if base != Path(base).name:
        raise UnsafeNameError("Unsafe filename.")
    return base


def index_meta(index_dir: Path | None = None) -> dict:
    index_dir = index_dir or INDEX_DIR
    path = index_dir / "config.json"
    if not path.exists():
        return {"indexed_at": None, "num_chunks": None, "files": []}
    with open(path) as f:
        cfg = json.load(f)
    files = cfg.get("files") or []
    if not isinstance(files, list):
        files = []
    return {
        "indexed_at": cfg.get("indexed_at"),
        "num_chunks": cfg.get("num_chunks"),
        "files": [str(x) for x in files],
    }


def list_docs(docs_dir: Path | None = None, index_dir: Path | None = None) -> list[dict]:
    docs_dir = docs_dir or DOCS_DIR
    meta = index_meta(index_dir)
    indexed = set(meta["files"])
    disk = {}
    if docs_dir.exists():
        for path in docs_dir.glob("*.md"):
            st = path.stat()
            disk[path.name] = {
                "name": path.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            }
    names = sorted(set(disk) | indexed)
    rows = []
    for name in names:
        if name in disk:
            state = "indexed" if name in indexed else "not_indexed"
            rows.append({**disk[name], "state": state})
        else:
            rows.append(
                {
                    "name": name,
                    "size": 0,
                    "mtime": None,
                    "state": "missing_on_disk",
                }
            )
    return rows


def save_upload(filename: str, data: bytes, docs_dir: Path | None = None) -> str:
    docs_dir = docs_dir or DOCS_DIR
    try:
        name = safe_md_name(filename)
    except UnsafeNameError as exc:
        raise UploadError(str(exc)) from exc
    if not data:
        raise UploadError("Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("File larger than 1 MB.")
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / name).write_bytes(data)
    return name


def delete_doc(name: str, docs_dir: Path | None = None) -> None:
    docs_dir = docs_dir or DOCS_DIR
    safe = safe_md_name(name)
    path = docs_dir / safe
    if not path.is_file():
        raise FileNotFoundError(safe)
    path.unlink()
```

Tighten `safe_md_name` so `../x.md`, `/tmp/x.md`, `foo/bar.md`, `foo.md.exe`, and `readme.MD` (allowed — suffix case-insensitive, stored as given basename) behave as the spec. **Store the uploaded basename** after `Path(name).name`, require `endswith .md` case-insensitive, reject if `Path(name).name !=` the stripped base or if separators present in the original string.

Correct `safe_md_name`:

```python
def safe_md_name(name: str) -> str:
    if not name or "\x00" in name or "/" in name or "\\" in name:
        raise UnsafeNameError("Unsafe filename.")
    base = Path(name).name
    if base != name or base in (".", "..") or base.startswith("."):
        raise UnsafeNameError("Unsafe filename.")
    if not base.lower().endswith(".md") or len(base) < 4:
        raise UnsafeNameError("Only .md filenames are allowed.")
    return base
```

Use this version, not the first sketch.

- [ ] **Step 2: Run checks (expect pass if Step 1 used the corrected `safe_md_name`)**

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
from pathlib import Path
import tempfile
import library as lib
from library import safe_md_name, UnsafeNameError, save_upload, delete_doc, list_docs, UploadError

for bad in ["../x.md", "/tmp/x.md", "a/b.md", "a\\b.md", "x.txt", ".md", "foo", "", "x.md\x00"]:
    try:
        safe_md_name(bad)
        raise SystemExit(f"should reject {bad!r}")
    except UnsafeNameError:
        pass
assert safe_md_name("PTO.MD") == "PTO.MD"
assert safe_md_name("pto_policy.md") == "pto_policy.md"

td = Path(tempfile.mkdtemp())
idx = Path(tempfile.mkdtemp())
(td / "a.md").write_text("# A\n")
(idx / "config.json").write_text('{"indexed_at":"t","num_chunks":1,"files":["a.md","gone.md"]}')
rows = {r["name"]: r for r in list_docs(td, idx)}
assert rows["a.md"]["state"] == "indexed"
assert rows["gone.md"]["state"] == "missing_on_disk"
save_upload("b.md", b"# B\n\n## H\n\nx", td)
assert (td / "b.md").read_bytes().startswith(b"# B")
rows = {r["name"]: r for r in list_docs(td, idx)}
assert rows["b.md"]["state"] == "not_indexed"
try:
    save_upload("b.md", b"", td)
    raise SystemExit("empty should fail")
except UploadError:
    pass
delete_doc("b.md", td)
assert not (td / "b.md").exists()
try:
    delete_doc("b.md", td)
    raise SystemExit("missing delete should fail")
except FileNotFoundError:
    pass
print("ok")
PY
```

Expected: `ok`

- [ ] **Step 3: Commit**

```bash
git add rag-demo/library.py
git commit -m "Add library helpers for markdown upload and corpus snapshot"
```

---

### Task 2: Atomic `build()` and richer `config.json`

**Files:**
- Modify: `rag-demo/build_index.py`

**Interfaces:**
- Consumes: `chunk.chunk_directory`, `library` not required
- Produces:
  - `class IndexBuildError(Exception)`
  - `def build() -> dict` — returns the config written (`embedding_model`, `num_chunks`, `indexed_at` UTC ISO, `files` sorted unique `source_file`). Raises `IndexBuildError("No chunks to index.")` if `chunk_directory` returns `[]`. On any failure before the final replace, live `index/chunks.faiss`, `metadata.pkl`, and `config.json` stay as they were.

Write to `INDEX_DIR / ".tmp"`, then `Path.replace` each of the three artifacts onto `INDEX_DIR`, then `shutil.rmtree` the tmp dir.

- [ ] **Step 1: Confirm current in-place write (baseline)**

Read `rag-demo/build_index.py`. Note it writes directly into `INDEX_DIR`.

- [ ] **Step 2: Implement atomic `build()`**

Replace `build()` with:

```python
"""
Embeds all chunks and builds a FAISS index, persisted to disk.

Run this once (and again any time docs/ changes) before querying.
Writes into index/.tmp and replaces the live artifacts only after a
full successful encode — a crash mid-write must not truncate chunks.faiss.
"""
import json
import pickle
import shutil
from datetime import datetime, timezone
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

from chunk import chunk_directory

EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
DOCUMENT_PREFIX = "search_document: "
INDEX_DIR = Path(__file__).parent / "index"


class IndexBuildError(Exception):
    pass


def build() -> dict:
    docs_dir = Path(__file__).parent / "docs"
    chunks = chunk_directory(docs_dir)
    if not chunks:
        raise IndexBuildError("No chunks to index.")

    model = SentenceTransformer(EMBEDDING_MODEL)
    texts = [DOCUMENT_PREFIX + c.text for c in chunks]
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)
    embeddings = np.asarray(embeddings, dtype="float32")

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    files = sorted({c.source_file for c in chunks})
    config = {
        "embedding_model": EMBEDDING_MODEL,
        "num_chunks": len(chunks),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    INDEX_DIR.mkdir(exist_ok=True)
    tmp = INDEX_DIR / ".tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    try:
        faiss.write_index(index, str(tmp / "chunks.faiss"))
        with open(tmp / "metadata.pkl", "wb") as f:
            pickle.dump(chunks, f)
        with open(tmp / "config.json", "w") as f:
            json.dump(config, f)
        for name in ("chunks.faiss", "metadata.pkl", "config.json"):
            (tmp / name).replace(INDEX_DIR / name)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"Index built: {len(chunks)} vectors, dim={embeddings.shape[1]}")
    print(f"Saved to {INDEX_DIR}/")
    return config
```

Keep `if __name__ == "__main__": build()`.

- [ ] **Step 3: Empty-docs failure must not wipe the live index**

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
import json, shutil
from pathlib import Path
import build_index
from build_index import IndexBuildError

docs = Path("docs")
backup = Path("_docs_backup_probe")
assert not backup.exists()
shutil.copytree(docs, backup)
index_cfg = Path("index/config.json").read_text()
try:
    for p in docs.glob("*.md"):
        p.unlink()
    try:
        build_index.build()
        raise SystemExit("empty docs should raise")
    except IndexBuildError as e:
        assert "No chunks" in str(e)
    after = Path("index/config.json").read_text()
    assert after == index_cfg, "live config.json was modified"
finally:
    if docs.exists():
        shutil.rmtree(docs)
    shutil.copytree(backup, docs)
    shutil.rmtree(backup)
print("ok empty")
PY
```

Expected: `ok empty`. If this fails because `docs/` was left empty, restore from `_docs_backup_probe` or git checkout `rag-demo/docs`.

- [ ] **Step 4: Rebuild for real and check config keys + eval**

```bash
cd rag-demo
./.venv/bin/python build_index.py
./.venv/bin/python - <<'PY'
import json
cfg = json.load(open("index/config.json"))
assert cfg["embedding_model"].startswith("nomic")
assert cfg["num_chunks"] > 0
assert "indexed_at" in cfg and cfg["files"]
assert "pto_policy.md" in cfg["files"]
print("config ok", cfg["num_chunks"], "chunks")
PY
./.venv/bin/python eval.py
```

Expected: config ok; eval still ~95% @1 and 100% @3/@5 for both methods (do not fail the task on a 1-query flip; fail if @3 drops below 100% or the script errors).

- [ ] **Step 5: Commit**

```bash
git add rag-demo/build_index.py
git commit -m "Write FAISS artifacts atomically and record indexed files"
```

---

### Task 3: `retrieve.reload()` and index lock

**Files:**
- Modify: `rag-demo/retrieve.py`

**Interfaces:**
- Consumes: `index/chunks.faiss`, `metadata.pkl`, `config.json` as today
- Produces:
  - `def reload() -> None` — load embedder/reranker if missing; always re-read FAISS + pickle into locals; on read failure raise and **do not** clear old `_index`/`_chunks`; on success assign both under `_state_lock`
  - `_load()` — if `_index is None`, call `reload()`; else return
  - `vector_search` / `rerank` use `_state_lock` when reading `_index` and `_chunks` (embedder encode stays outside the lock)

- [ ] **Step 1: Add `threading.Lock`, `reload()`, and lock the readers**

At module top, `import threading`. After the globals:

```python
_state_lock = threading.Lock()
```

Replace `_load` and add `reload`:

```python
def reload() -> None:
    """Re-read FAISS + chunk metadata from disk. Keep models if already loaded."""
    global _embedder, _reranker, _index, _chunks
    config_path = INDEX_DIR / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        indexed_model = cfg.get("embedding_model")
        if indexed_model and indexed_model != EMBEDDING_MODEL:
            raise RuntimeError(
                f"Index was built with {indexed_model!r}, but retrieve.py "
                f"uses {EMBEDDING_MODEL!r}. Re-run build_index.py."
            )
    if _embedder is None:
        _embedder = SentenceTransformer(EMBEDDING_MODEL)
        _reranker = CrossEncoder(RERANKER_MODEL)
    new_index = faiss.read_index(str(INDEX_DIR / "chunks.faiss"))
    with open(INDEX_DIR / "metadata.pkl", "rb") as f:
        new_chunks = pickle.load(f)
    with _state_lock:
        _index = new_index
        _chunks = new_chunks


def _load():
    if _index is not None:
        return
    reload()
```

In `vector_search`, after `_load()` and encoding `q_emb`:

```python
    with _state_lock:
        index = _index
        chunks = _chunks
    scores, indices = index.search(q_emb, top_k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        c = chunks[idx]
        ...
```

In `rerank`, `_load()` then:

```python
    with _state_lock:
        reranker = _reranker
    scores = reranker.predict(pairs)
```

(Reranker is not swapped on reload; lock is still fine.)

- [ ] **Step 2: Verify reload sees a newly built index**

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
import retrieve
from retrieve import reload, vector_search

# First load
hits = vector_search("PTO days per year", top_k=1)
assert hits
n_before = retrieve._index.ntotal
reload()
assert retrieve._index.ntotal == n_before
hits2 = vector_search("PTO days per year", top_k=1)
assert hits2[0].source_file == hits[0].source_file
print("reload ok", n_before)
PY
```

Expected: `reload ok <n>` with no exception. First run may download models.

- [ ] **Step 3: Failed reload must keep the old index**

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
import shutil
from pathlib import Path
import retrieve
from retrieve import reload, vector_search

vector_search("PTO", top_k=1)
old = retrieve._index
src = Path("index/chunks.faiss")
bak = Path("index/chunks.faiss.bak_probe")
shutil.copy2(src, bak)
try:
    src.write_bytes(b"not-a-faiss-index")
    try:
        reload()
        raise SystemExit("reload should fail")
    except Exception as exc:
        print("raised", type(exc).__name__)
    assert retrieve._index is old
finally:
    shutil.move(str(bak), str(src))
reload()
print("restored", retrieve._index.ntotal)
PY
```

Expected: `raised` some FAISS/error type; `restored <n>`. If this errors mid-way, `mv rag-demo/index/chunks.faiss.bak_probe rag-demo/index/chunks.faiss`.

- [ ] **Step 4: Commit**

```bash
git add rag-demo/retrieve.py
git commit -m "Hot-reload FAISS metadata without restarting the process"
```

---

### Task 4: Status/docs GET + PWA static routes + rebuilding flag

**Files:**
- Modify: `rag-demo/server.py`
- Modify: `rag-demo/requirements.txt` — add `python-multipart>=0.0.9` (needed in Task 5; add now so TestClient uploads work)

**Interfaces:**
- Consumes: `library.list_docs`, `library.index_meta`
- Produces:
  - Module globals: `_rebuilding: bool = False`, `_active_answers: int = 0`, `_answers_idle: asyncio.Event` (set at startup)
  - `GET /api/status` → `{rebuilding, indexed_at, num_chunks}`
  - `GET /api/docs` → `{rebuilding, indexed_at, num_chunks, docs}` where `docs` is `list_docs()`
  - `GET /manifest.webmanifest`, `GET /sw.js`, `GET /icons/{name}` — FileResponse (icons/manifest/sw may 404 until Task 7; return 404 if missing, do not crash)
  - `python-multipart` in requirements

Do **not** implement POST/DELETE/reindex yet. WebSocket Ask is unchanged except: if `_rebuilding`, reject with the spec error message (flag stays False until Task 6, so this is dead until then).

- [ ] **Step 1: Install multipart and add GET routes**

```bash
cd rag-demo
./.venv/bin/pip install 'python-multipart>=0.0.9'
```

Append to `requirements.txt`:

```
python-multipart>=0.0.9
```

In `server.py`:

- `import asyncio`
- `from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect`
- `from fastapi.responses import FileResponse, JSONResponse`
- `from library import delete_doc, index_meta, list_docs, save_upload, MAX_UPLOAD_FILES, UploadError, UnsafeNameError`
- After `app = FastAPI(...)`:

```python
_rebuilding = False
_active_answers = 0
_answers_idle: asyncio.Event | None = None


def _idle() -> asyncio.Event:
    global _answers_idle
    if _answers_idle is None:
        _answers_idle = asyncio.Event()
        _answers_idle.set()
    return _answers_idle


def _status_body() -> dict:
    meta = index_meta()
    return {
        "rebuilding": _rebuilding,
        "indexed_at": meta.get("indexed_at"),
        "num_chunks": meta.get("num_chunks"),
    }


def _docs_body() -> dict:
    return {**_status_body(), "docs": list_docs()}
```

Routes:

```python
@app.get("/api/status")
def api_status() -> dict:
    return _status_body()


@app.get("/api/docs")
def api_docs() -> dict:
    return _docs_body()


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    path = STATIC_DIR / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    path = STATIC_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/icons/{name}")
def icon(name: str) -> FileResponse:
    path = (STATIC_DIR / "icons" / name).resolve()
    if not str(path).startswith(str((STATIC_DIR / "icons").resolve())) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)
```

In `ws_ask`, after parsing `question`, if `_rebuilding`: send `{"type": "error", "message": "Index is rebuilding. Try again when it finishes."}` and `continue`.

Track in-flight answers around `_pump`:

```python
            _active_answers += 1
            _idle().clear()
            try:
                await _pump(websocket, question, cancel)
            finally:
                _active_answers -= 1
                if _active_answers == 0:
                    _idle().set()
                busy = False
```

(Remove the old `finally: busy = False` duplication — one `finally` that decrements and sets `busy = False`.)

- [ ] **Step 2: Hit GET with TestClient**

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
import server

# TestClient runs lifespan → _load() (slow first time)
with TestClient(server.app) as c:
    s = c.get("/api/status")
    assert s.status_code == 200, s.text
    body = s.json()
    assert "rebuilding" in body and body["rebuilding"] is False
    d = c.get("/api/docs")
    assert d.status_code == 200
    names = [x["name"] for x in d.json()["docs"]]
    assert "pto_policy.md" in names
    assert c.get("/").status_code == 200
print("get ok")
PY
```

Expected: `get ok`. Lifespan still requires `index/chunks.faiss`.

- [ ] **Step 3: Commit**

```bash
git add rag-demo/server.py rag-demo/requirements.txt
git commit -m "Add library status endpoints and PWA static routes"
```

---

### Task 5: Upload and delete

**Files:**
- Modify: `rag-demo/server.py`

**Interfaces:**
- Consumes: `save_upload`, `delete_doc`, `UploadError`, `UnsafeNameError`, `MAX_UPLOAD_FILES`
- Produces:
  - `POST /api/docs` multipart `files`
  - `DELETE /api/docs/{name}`
  - While `_rebuilding`: both return **409** `{"detail": "Index is rebuilding. Try again when it finishes."}`

Status mapping:
- `UnsafeNameError` / bad name → 400
- empty / too large / not md → `UploadError` → per-file error
- `> MAX_UPLOAD_FILES` → 400 for the whole request
- mixed batch: save good files, **207** `{docs, errors:[{name, message}]}`
- all fail → **400** `{errors: [...]}` (include `docs` as well so the UI can refresh)
- delete missing → 404
- delete unsafe name → 400

```python
from fastapi import File, UploadFile

@app.post("/api/docs")
async def api_upload(files: list[UploadFile] = File(...)):
    if _rebuilding:
        raise HTTPException(409, "Index is rebuilding. Try again when it finishes.")
    if not files or len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, "Send between 1 and 20 markdown files.")
    errors = []
    for uf in files:
        data = await uf.read()
        try:
            save_upload(uf.filename or "", data)
        except UploadError as exc:
            errors.append({"name": uf.filename or "", "message": str(exc)})
    body = _docs_body()
    if errors and len(errors) == len(files):
        return JSONResponse(status_code=400, content={**body, "errors": errors})
    if errors:
        return JSONResponse(status_code=207, content={**body, "errors": errors})
    return body


@app.delete("/api/docs/{name}")
def api_delete(name: str):
    if _rebuilding:
        raise HTTPException(409, "Index is rebuilding. Try again when it finishes.")
    try:
        delete_doc(name)
    except UnsafeNameError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(404, "File not found.")
    return _docs_body()
```

- [ ] **Step 1: Add the two handlers as above**

- [ ] **Step 2: Verify with TestClient against a probe file, then delete it**

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
import server

probe = "zz_upload_probe.md"
content = b"# Probe\n\n## Secret\n\nThe cafeteria serves lunar waffles on Tuesdays.\n"

with TestClient(server.app) as c:
    r = c.post("/api/docs", files=[("files", (probe, content, "text/markdown"))])
    assert r.status_code == 200, r.text
    states = {d["name"]: d["state"] for d in r.json()["docs"]}
    assert states[probe] == "not_indexed"
    bad = c.post("/api/docs", files=[("files", ("x.txt", b"nope", "text/plain"))])
    assert bad.status_code == 400
    gone = c.delete("/api/docs/nope.md")
    assert gone.status_code == 404
    d = c.delete(f"/api/docs/{probe}")
    assert d.status_code == 200, d.text
    names = [x["name"] for x in d.json()["docs"]]
    assert probe not in names
print("upload ok")
PY
```

Expected: `upload ok`. Confirm `rag-demo/docs/zz_upload_probe.md` is gone.

- [ ] **Step 3: Commit**

```bash
git add rag-demo/server.py
git commit -m "Accept markdown uploads and deletes without rebuilding the index"
```

---

### Task 6: `POST /api/reindex` and the app lock

**Files:**
- Modify: `rag-demo/server.py`

**Interfaces:**
- Consumes: `build_index.build` → `dict`, `build_index.IndexBuildError`, `retrieve.reload`
- Produces: `POST /api/reindex`
  1. If `_rebuilding`: 409
  2. Set `_rebuilding = True` immediately
  3. `await _idle().wait()` so in-flight `_pump` calls finish
  4. `await asyncio.to_thread(_rebuild_sync)`
  5. `finally: _rebuilding = False`
  - Success 200 `{ok: true, num_chunks, indexed_at, files}`
  - `IndexBuildError` → 400 `{"detail": "No chunks to index."}`
  - `reload()` exception after successful `build()` → 500 `{"detail": "Index rebuilt on disk but failed to load. Restart the server."}`
  - other `build()` exceptions → 500 with a short `str(exc)` (cap ~300 chars)

```python
from build_index import IndexBuildError, build
from retrieve import reload as reload_index


def _rebuild_sync() -> dict:
    config = build()
    try:
        reload_index()
    except Exception as exc:
        raise RuntimeError("Index rebuilt on disk but failed to load. Restart the server.") from exc
    return config


@app.post("/api/reindex")
async def api_reindex():
    global _rebuilding
    if _rebuilding:
        raise HTTPException(409, "Index is rebuilding. Try again when it finishes.")
    _rebuilding = True
    try:
        await _idle().wait()
        try:
            config = await asyncio.to_thread(_rebuild_sync)
        except IndexBuildError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, str(exc)[:300]) from exc
        return {
            "ok": True,
            "num_chunks": config["num_chunks"],
            "indexed_at": config["indexed_at"],
            "files": config["files"],
        }
    finally:
        _rebuilding = False
```

- [ ] **Step 1: Add `_rebuild_sync` and `POST /api/reindex`**

- [ ] **Step 2: Double-submit lock with a monkeypatched slow build**

Do **not** wait on a real encode for this check:

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
import time, threading
from fastapi.testclient import TestClient
import server

real = server._rebuild_sync

def slow():
    time.sleep(1.5)
    return {"num_chunks": 1, "indexed_at": "t", "files": []}

server._rebuild_sync = slow
try:
    with TestClient(server.app) as c:
        results = {}
        def call(key):
            results[key] = c.post("/api/reindex").status_code
        t1 = threading.Thread(target=call, args=("a",))
        t2 = threading.Thread(target=call, args=("b",))
        t1.start(); time.sleep(0.2); t2.start()
        t1.join(); t2.join()
        print(results)
        assert 200 in results.values()
        assert 409 in results.values()
        # upload while rebuilding: start another slow rebuild
        def slow2():
            time.sleep(0.8)
            return {"num_chunks": 1, "indexed_at": "t", "files": []}
        server._rebuild_sync = slow2
        t = threading.Thread(target=lambda: results.__setitem__("c", c.post("/api/reindex").status_code))
        t.start(); time.sleep(0.1)
        up = c.post("/api/docs", files=[("files", ("zz.md", b"# Z\n\n## H\nx", "text/markdown"))])
        t.join()
        print("upload during", up.status_code)
        assert up.status_code == 409
        # cleanup if the slow rebuild finished after and something wrote — zz.md should not exist
finally:
    server._rebuild_sync = real
    from pathlib import Path
    Path("docs/zz.md").unlink(missing_ok=True)
print("lock ok")
PY
```

Expected: `lock ok`. If `zz.md` exists, delete it.

Note: TestClient may serialize HTTP; if both reindex calls return 200 because TestClient is not thread-safe, fall back to `httpx` + a live `uvicorn` in a subprocess, or use two `asyncio` tasks with `httpx.ASGITransport` / `AsyncClient`. If the threaded TestClient is flaky, use this instead:

```python
import asyncio
from httpx import ASGITransport, AsyncClient
import server

async def main():
    transport = ASGITransport(app=server.app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # trigger lifespan
        await c.get("/api/status")
        async def one():
            return (await c.post("/api/reindex")).status_code
        # patch slow first
        ...
        s1, s2 = await asyncio.gather(one(), one())
        print(s1, s2)

asyncio.run(main())
```

Starlette may still serialize. **Acceptable proof of the lock:** set `_rebuilding = True` then `POST /api/reindex` → 409, `POST /api/docs` → 409, then set False. Also run one real reindex (next step).

Minimum required check if concurrency is flaky:

```python
with TestClient(server.app) as c:
    server._rebuilding = True
    try:
        assert c.post("/api/reindex").status_code == 409
        assert c.post("/api/docs", files=[("files", ("zz.md", b"# Z\n", "text/markdown"))]).status_code == 409
        assert c.delete("/api/docs/pto_policy.md").status_code == 409
        assert c.get("/api/docs").status_code == 200
        assert c.get("/api/docs").json()["rebuilding"] is True
    finally:
        server._rebuilding = False
```

Include this check. Then one real rebuild:

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
import server
with TestClient(server.app) as c:
    r = c.post("/api/reindex")
    print(r.status_code, r.text[:500])
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["num_chunks"] > 0
    assert "pto_policy.md" in body["files"]
print("reindex ok")
PY
```

Expected: `reindex ok` (this re-embeds the corpus; can take a minute).

- [ ] **Step 3: Commit**

```bash
git add rag-demo/server.py
git commit -m "Rebuild the index from the API and block asks while it runs"
```

---

### Task 7: PWA shell assets

**Files:**
- Create: `rag-demo/static/manifest.webmanifest`
- Create: `rag-demo/static/sw.js`
- Create: `rag-demo/static/icons/icon-192.png`
- Create: `rag-demo/static/icons/icon-512.png`
- Modify: `rag-demo/static/index.html` (head + SW register only; library panel is Task 8)

**Interfaces:**
- Consumes: Task 4 static routes
- Produces: installable shell; cache name `ask-northwind-v1`

- [ ] **Step 1: Write icons (stdlib PNG, brass `#c9a36a` on `#141311`)**

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
import struct, zlib
from pathlib import Path

def write_png(path: Path, size: int, rgb=(0xC9, 0xA3, 0x6A), bg=(0x14, 0x13, 0x11)):
    r, g, b = rgb
    br, bgc, bb = bg
    rows = []
    margin = size // 6
    for y in range(size):
        row = bytearray()
        row.append(0)
        for x in range(size):
            if margin <= x < size - margin and margin <= y < size - margin:
                row += bytes((r, g, b))
            else:
                row += bytes((br, bgc, bb))
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)

root = Path("static/icons")
write_png(root / "icon-192.png", 192)
write_png(root / "icon-512.png", 512)
print("icons", (root / "icon-192.png").stat().st_size)
PY
```

- [ ] **Step 2: Write `static/manifest.webmanifest`**

```json
{
  "name": "Ask Northwind",
  "short_name": "Northwind",
  "start_url": "/",
  "display": "standalone",
  "background_color": "#141311",
  "theme_color": "#c9a36a",
  "icons": [
    { "src": "/icons/icon-192.png", "sizes": "192x192", "type": "image/png" },
    { "src": "/icons/icon-512.png", "sizes": "512x512", "type": "image/png" }
  ]
}
```

- [ ] **Step 3: Write `static/sw.js`**

```javascript
const CACHE = "ask-northwind-v1";
const SHELL = [
  "/",
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

- [ ] **Step 4: Register in `index.html` `<head>`** (keep existing CSS)

After `<title>Ask Northwind</title>` add:

```html
  <meta name="theme-color" content="#c9a36a" />
  <meta name="apple-mobile-web-app-capable" content="yes" />
  <meta name="apple-mobile-web-app-title" content="Ask Northwind" />
  <link rel="manifest" href="/manifest.webmanifest" />
  <link rel="apple-touch-icon" href="/icons/icon-192.png" />
```

Before `</body>`, after the existing script (or at the end of the script):

```javascript
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js");
    }
```

- [ ] **Step 5: Verify routes**

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
import server
with TestClient(server.app) as c:
    assert c.get("/manifest.webmanifest").status_code == 200
    assert c.get("/sw.js").status_code == 200
    assert c.get("/icons/icon-192.png").status_code == 200
    assert c.get("/icons/icon-512.png").status_code == 200
    html = c.get("/").text
    assert "manifest.webmanifest" in html
    assert "serviceWorker" in html
print("pwa files ok")
PY
```

Expected: `pwa files ok`

- [ ] **Step 6: Commit**

```bash
git add rag-demo/static/manifest.webmanifest rag-demo/static/sw.js rag-demo/static/icons rag-demo/static/index.html
git commit -m "Add an installable PWA shell that caches the UI only"
```

---

### Task 8: Library panel UI

**Files:**
- Modify: `rag-demo/static/index.html`

**Interfaces:**
- Consumes: `GET /api/docs`, `GET /api/status`, `POST /api/docs`, `DELETE /api/docs/{name}`, `POST /api/reindex`
- Produces: Library panel above the footer; lock Ask/upload/delete/Re-index while `rebuilding` or while a reindex fetch is in flight

- [ ] **Step 1: CSS + markup**

Add CSS (match existing brass/paper tokens):

```css
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
```

Insert **above** `<footer>`:

```html
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
```

- [ ] **Step 2: JS — fetch library, upload, delete, reindex, lock, offline**

Add beside the existing script variables and wire as follows (keep Ask/WebSocket code). Use these exact strings from the spec:

- Confirm delete: `Delete ${name} from disk? Re-index afterward to update search.`
- Offline library: `You're offline.`
- Rebuild label: `Rebuilding…`
- Success: `Indexed ${n} chunks.`
- Ask still: `Not connected. Use Reconnect.`

```javascript
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
      input.disabled = locked || answering;
      askBtn.disabled = locked || answering;
      uploadBtn.disabled = locked;
      reindexBtn.disabled = locked;
      docList.querySelectorAll("button").forEach((b) => { b.disabled = locked; });
      reindexBtn.textContent = locked ? "Rebuilding…" : "Re-index";
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
      const res = await fetch("/api/docs");
      const body = await res.json();
      setRebuildLock(!!body.rebuilding);
      renderDocs(body.docs || []);
      if (body.rebuilding) setLibStatus("Rebuilding…");
      else if (!libStatus.classList.contains("err")) {
        const n = body.num_chunks;
        setLibStatus(n != null ? `${body.docs.length} files · ${n} chunks indexed` : "Ready");
      }
    }

    async function removeDoc(name) {
      if (!confirm(`Delete ${name} from disk? Re-index afterward to update search.`)) return;
      if (isOffline()) { setLibStatus("You're offline.", "err"); return; }
      const res = await fetch("/api/docs/" + encodeURIComponent(name), { method: "DELETE" });
      if (res.status === 409) { setRebuildLock(true); setLibStatus("Index is rebuilding. Try again when it finishes.", "err"); return; }
      if (!res.ok) { setLibStatus((await res.json()).detail || "Delete failed", "err"); return; }
      const body = await res.json();
      renderDocs(body.docs);
      setLibStatus("Deleted " + name + ". Re-index to update search.");
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
      const res = await fetch("/api/docs", { method: "POST", body: fd });
      fileInput.value = "";
      const body = await res.json().catch(() => ({}));
      if (res.status === 409) { setRebuildLock(true); setLibStatus("Index is rebuilding. Try again when it finishes.", "err"); return; }
      if (body.docs) renderDocs(body.docs);
      if (!res.ok && res.status !== 207) {
        const msg = (body.errors && body.errors[0] && body.errors[0].message) || body.detail || "Upload failed";
        setLibStatus(msg, "err");
        return;
      }
      setLibStatus("Saved. Re-index to update search.");
    });

    reindexBtn.addEventListener("click", async () => {
      if (isOffline()) { setLibStatus("You're offline.", "err"); return; }
      setRebuildLock(true);
      setLibStatus("Rebuilding…");
      try {
        const res = await fetch("/api/reindex", { method: "POST" });
        const body = await res.json().catch(() => ({}));
        if (!res.ok) {
          setLibStatus(body.detail || "Re-index failed", "err");
          return;
        }
        setLibStatus("Indexed " + body.num_chunks + " chunks.", "ok");
        await refreshLibrary();
      } catch (err) {
        setLibStatus("You're offline.", "err");
      } finally {
        if (!rebuilding) setRebuildLock(false);
        else {
          // refreshLibrary sets the lock from server; if we skipped it on error, unlock
          const s = await fetch("/api/status").then((r) => r.json()).catch(() => ({ rebuilding: false }));
          setRebuildLock(!!s.rebuilding);
        }
      }
    });

    window.addEventListener("offline", () => setLibStatus("You're offline.", "err"));
    refreshLibrary();
```

Fix the `finally` of reindex so a failed reindex **unlocks** (server clears `_rebuilding` in `finally`). After the fetch completes (ok or error), `setRebuildLock(false)` then `refreshLibrary()`.

Correct reindex handler `finally`:

```javascript
      } finally {
        setRebuildLock(false);
        await refreshLibrary();
      }
```

On success, `refreshLibrary` after unlock; success message: set it **after** refresh so refresh does not overwrite — either skip overwriting when we just succeeded, or set the success line after refresh:

```javascript
        const res = await fetch("/api/reindex", { method: "POST" });
        const body = await res.json().catch(() => ({}));
        setRebuildLock(false);
        await refreshLibrary();
        if (!res.ok) setLibStatus(body.detail || "Re-index failed", "err");
        else setLibStatus("Indexed " + body.num_chunks + " chunks.", "ok");
```

Use that. Wrap with `setRebuildLock(true)` at start.

Also update `setBusy` so it does not enable Ask while `rebuilding`:

```javascript
    function setBusy(busy) {
      answering = busy;
      input.disabled = busy || rebuilding;
      askBtn.disabled = busy || rebuilding;
    }
```

Call `refreshLibrary()` on load (in addition to `connect()`).

- [ ] **Step 3: Smoke the HTML contains the new panel**

```bash
cd rag-demo
./.venv/bin/python - <<'PY'
from fastapi.testclient import TestClient
import server
html = TestClient(server.app).get("/").text
for needle in ["id=\"library\"", "id=\"reindex\"", "Upload .md", "serviceWorker"]:
    assert needle in html, needle
print("ui ok")
PY
```

Expected: `ui ok`

- [ ] **Step 4: Manual browser pass (required)**

Start `./.venv/bin/python server.py`. In the browser at `http://127.0.0.1:8000`:

1. Library lists the Northwind `.md` files as `indexed`.
2. Upload a file `zz_upload_probe.md` containing a unique fact (`lunar waffles`). State becomes `not indexed`. Ask about lunar waffles — should not cite the new file.
3. Click **Re-index**, wait, status `Indexed N chunks.`, file becomes `indexed`. Ask again — should cite `zz_upload_probe.md`.
4. Delete the probe, confirm the dialog. Ask can still cite it. Re-index again — Ask should not.
5. Click Re-index twice quickly if possible; UI stays on Rebuilding… until done.
6. Chrome/Safari: install / Add to Home Screen if offered; confirm standalone still streams Ask.

Leave `docs/` without the probe file.

- [ ] **Step 5: Commit**

```bash
git add rag-demo/static/index.html
git commit -m "Add a library panel and Re-index control to the PWA"
```

---

### Task 9: Docs + eval regression

**Files:**
- Modify: `rag-demo/README.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: behavior from Tasks 1–8
- Produces: README/AGENTS describe PWA, library, Re-index, `python-multipart`, `library.py`, `retrieve.reload()`

- [ ] **Step 1: README**

In Setup, after `python server.py`, add that the UI is installable (Add to Home Screen), has a Library panel, and **Re-index** rebuilds search from `docs/`. Upload does not change answers until Re-index.

In the pipeline diagram, mention the browser can write `docs/` and call `build()`.

Add a short “Library and re-index” subsection: markdown only, overwrite on same name, no auth, lock during rebuild.

- [ ] **Step 2: AGENTS.md**

- Commands table: `server.py` line should mention PWA + library + Re-index.
- Key files: add `library.py`, `static/manifest.webmanifest`, `static/sw.js`; note `retrieve.reload()` and atomic `build()`.
- Gotchas: rebuild from the UI still required after `docs/` edits; CLI `build_index.py` is not picked up until Re-index or restart.
- Architecture ascii art: keep CLI path; add server library/reindex one-liner.

- [ ] **Step 3: Final eval**

```bash
cd rag-demo
./.venv/bin/python eval.py
```

Expected: script completes; @3/@5 remain 100%.

- [ ] **Step 4: Commit**

```bash
git add rag-demo/README.md AGENTS.md
git commit -m "Document the PWA library and Re-index flow"
```

---

## Self-review

**Spec coverage**

| Spec section | Task |
|--------------|------|
| Markdown only, upload/delete disk-only | 1, 5 |
| Atomic `index/.tmp` + `files`/`indexed_at` | 2 |
| `retrieve.reload()` + lock | 3 |
| GET `/api/status`, `/api/docs` | 4 |
| POST/DELETE `/api/docs`, 207 mixed | 5 |
| POST `/api/reindex`, wait in-flight, 409 | 6 |
| PWA manifest/SW/icons, no API cache | 7 |
| Same-page library UI, lock, offline copy | 8 |
| Empty docs 400, reload-fail 500 | 2, 6 |
| eval.py + manual upload/delete/reindex | 2, 8, 9 |
| README/out of scope (no auth/PDF/chat history) | 9 |

**Placeholder scan:** no TBD/TODO; verification commands are concrete; no “similar to Task N”.

**Type consistency:** `list_docs` → `{name, size, mtime, state}`; `build() -> dict` with `num_chunks`, `indexed_at`, `files`; `reload()` no args; `_rebuilding` bool; error copy matches the spec.

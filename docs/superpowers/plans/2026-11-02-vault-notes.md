# Vault Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stage uploads into the folder that is open, open `[[wikilinks]]` in the preview, and re-index one changed file without embedding the rest of the corpus.

**Architecture:** The upload folder is a prefix checked like any other corpus path, then joined to a basename that still cannot contain a slash. Wikilink resolution is a pure function over the file list the page already has, duplicated in the two renderers. `rebuild_paths` reconstructs the kept FAISS rows, embeds only the requested files, and publishes through the same `index/.tmp` replace as `build()`. A splice that would leave no chunks, or an index whose row count disagrees with the pickle, does not publish a partial index.

**Tech Stack:** Existing `library.py`, `review.py`, `build_index.py`, FastAPI, FAISS, vanilla JS. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-02-ask-northwind-next-design.md` (section Vault notes)

## Global Constraints

- Run from `src/` with `./.venv/bin/python`. No pytest, no new dependency, no new HTML route.
- Checks use `tempfile` directories. Do not write the user’s `DOCS_DIR`, `src/review/`, `src/index/`, `src/sessions.json`, or `src/.env`.
- Upload names still pass `safe_md_name`. The folder is a separate field. 1 MB and 20 files stay.
- Approve still does not rebuild the index. The page Re-index button still posts no body and still rebuilds everything.
- At most 20 paths on `POST /api/reindex`. A bad path is HTTP 400. An empty result is HTTP 400 and the live index bytes stay.
- `![[...]]` is not a wikilink. Unresolved and ambiguous links stay text.
- Service worker: `ask-northwind-v15` becomes `ask-northwind-v16`. If `CACHE` is not `v15`, add 1 to the integer that is there.
- No filesystem watcher.

## Review Focus

- `folder=../secret` and `folder=.hidden` are rejected, and no proposal file is written.
- A filename `hr/evil.md` is still rejected when `folder` is `notes`.
- `index.ntotal != len(chunks)` calls full `build()` and does not splice.
- A rebuild that would leave zero chunks raises `IndexBuildError` and leaves `chunks.faiss` byte-identical.
- `[[../x]]`, `[[.secret]]`, and a stem that matches two files all resolve to nothing. `hr/notes` still resolves when `other/notes.md` also exists.
- Digests of files that were not in the request are copied unchanged.

---

### Task 1: Upload into the open folder

**Files:**
- Modify: `src/library.py`
- Modify: `src/review.py`
- Modify: `src/server.py`
- Modify: `src/static/library.html`

**Interfaces:**
- Produces: `safe_folder(name: str) -> str`
- `stage_upload(filename: str, data: bytes, docs_dir: Path | None = None, review_dir: Path | None = None, folder: str = "") -> Proposal`
- `POST /api/docs` reads multipart field `folder`. Empty means the corpus root.

- [ ] **Step 1: Write the failing check**

Save as `/tmp/folder_upload_check.py` and run from `src/`:

```python
import tempfile
from pathlib import Path
import review
from library import UnsafeNameError, safe_folder

review._formatted = lambda text: (text, "manual", None)

root = Path(tempfile.mkdtemp())
review_dir = Path(tempfile.mkdtemp())
try:
    safe_folder("../secret")
    raise SystemExit("folder traversal was accepted")
except UnsafeNameError:
    pass
try:
    safe_folder(".hidden")
    raise SystemExit("hidden folder was accepted")
except UnsafeNameError:
    pass

proposal = review.stage_upload(
    "Note.md", b"# Note\n\n## Body text here\n", root, review_dir, folder="hr"
)
assert proposal.target == "hr/Note.md"
assert not (root / "hr" / "Note.md").exists()
assert (review_dir / "hr" / "Note.md.json").is_file()

try:
    review.stage_upload("hr/evil.md", b"# X\n", root, review_dir, folder="notes")
    raise SystemExit("slash filename was accepted")
except Exception as exc:
    assert "filename" in str(exc).lower()
print("folder ok")
```

`_proposal_path` writes `review_dir / f"{rel}.json"`, so the staged file is `hr/Note.md.json`. Patch `_formatted` so the check does not call a model. `stage_upload` looks that name up on the `review` module.

Run: `./.venv/bin/python /tmp/folder_upload_check.py`
Expected: FAIL because `safe_folder` does not exist.

- [ ] **Step 2: Implement**

`safe_folder` in `src/library.py`: strip the string, reject empty, `\`, NUL, absolute paths, and any part that is `.`, `..`, or starts with `.`. Return `Path(name).as_posix()`.

`stage_upload`: keep `safe_md_name` on the filename. When `folder` is non-empty, `rel = safe_md_relpath(safe_folder(folder) + "/" + name)`. An `UnsafeNameError` from the folder becomes `UploadError`, matching the filename branch. Pass `rel` to `live_path` instead of the basename.

Update the module docstring’s first sentences so they say a multipart `folder` prefixes the basename, and a missing folder still writes the root.

`api_upload`: add `folder: str = Form("")`. When `folder.strip()` is non-empty, call `safe_folder` once before the loop and map `UnsafeNameError` to HTTP 400. Pass `folder=folder` into every `stage_upload`. A bad folder writes no proposal.

In `uploadFiles`, when the selected pill is not `all`, `fd.append("folder", folder)`.

- [ ] **Step 3: Run the check**

Run: `./.venv/bin/python /tmp/folder_upload_check.py`
Expected: `folder ok`

Also confirm a `TestClient` is unnecessary if the check calls `stage_upload`. Read `api_upload` and confirm the 400 happens before `stage_upload` when `safe_folder` raises.

- [ ] **Step 4: Commit**

```bash
git add src/library.py src/review.py src/server.py src/static/library.html
git commit -m "feat: stage uploads into the open folder"
```

### Task 2: Wikilinks

**Files:**
- Modify: `src/library.py`
- Modify: `src/static/index.html`
- Modify: `src/static/library.html`
- Modify: `src/static/app.css`

**Interfaces:**
- Produces: `resolve_wikilink(target: str, names: list[str]) -> str | None`

- [ ] **Step 1: Write the failing check**

```python
from library import resolve_wikilink

names = ["hr/notes.md", "other/notes.md", "hr/pto.md", "index.md"]
assert resolve_wikilink("hr/notes", names) == "hr/notes.md"
assert resolve_wikilink("hr/notes.md", names) == "hr/notes.md"
assert resolve_wikilink("HR/PTO", names) == "hr/pto.md"
assert resolve_wikilink("notes", names) is None
assert resolve_wikilink("index", names) == "index.md"
assert resolve_wikilink("index#Section|Label", names) == "index.md"
assert resolve_wikilink("../secret", names) is None
assert resolve_wikilink(".secret", names) is None
assert resolve_wikilink("", names) is None
print("wikilink ok")
```

Run: `./.venv/bin/python /tmp/wikilink_check.py`
Expected: FAIL because `resolve_wikilink` is missing.

- [ ] **Step 2: Implement the resolver**

In `src/library.py`:

```python
def resolve_wikilink(target: str, names: list[str]) -> str | None:
    if not isinstance(target, str):
        return None
    raw = target.split("|", 1)[0]
    raw = raw.split("#", 1)[0].strip()
    if not raw or "\x00" in raw or "\\" in raw:
        return None
    path = Path(raw)
    if path.is_absolute() or path.anchor:
        return None
    parts = path.parts
    if not parts or any(part in (".", "..") or part.startswith(".") for part in parts):
        return None
    want = path.as_posix()
    lowered = {name.lower(): name for name in names}
    exact = lowered.get(want.lower()) or lowered.get((want if want.lower().endswith(".md") else want + ".md").lower())
    if exact:
        return exact
    stem = Path(want).stem.lower()
    stems = [name for name in names if Path(name).stem.lower() == stem]
    if len(stems) == 1:
        return stems[0]
    return None
```

Exact path wins before the stem count, so `hr/notes` resolves while bare `notes` does not. The stem is the filename stem: `[[pto]]` resolves to `hr/pto.md` when that stem is unique, and `[[notes]]` resolves to nothing when both `hr/notes.md` and `other/notes.md` exist.

- [ ] **Step 3: Render the links**

Both `renderInline` functions, after `escapeHtml` and before the strong/em replacements. The escaped text still contains `[[`.

Skip a match whose preceding character is `!`. Pattern for the rest:

```javascript
/\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]+))?\]\]/g
```

Resolve with a JS copy of `resolve_wikilink` over the file names already loaded (`loadVault` docs on Ask, `lastDocs` on Library). Keep the same rejection rules as the Python function, including case-insensitive exact path and unique stem. On a hit, emit `<button type="button" class="wikilink" data-path="...">label</button>`. The label is the alias when the second group exists, otherwise the target. Set the path with `escapeHtml`. On a miss, leave the original `[[...]]` text.

Ask: delegate clicks on `#preview-body .wikilink` to `openPreview(dataset.path)`. Library: the same, using its `openPreview`. Do not navigate the browser.

```css
button.wikilink { color: var(--amber); background: none; border: 0; padding: 0; cursor: pointer; font: inherit; }
```

- [ ] **Step 4: Run the check**

Run: `./.venv/bin/python /tmp/wikilink_check.py`
Expected: `wikilink ok`

- [ ] **Step 5: Commit**

```bash
git add src/library.py src/static/index.html src/static/library.html src/static/app.css
git commit -m "feat: open wikilinks in the note preview"
```

### Task 3: Rebuild one file

**Files:**
- Modify: `src/build_index.py`

**Interfaces:**
- `build(docs_dir: Path | None = None, index_dir: Path | None = None) -> dict` — omitted arguments keep today’s directories.
- `rebuild_paths(paths: list[str], docs_dir: Path | None = None, index_dir: Path | None = None, encode=None) -> dict`
- `encode(texts: list[str]) -> numpy.ndarray` of dtype `float32`, shape `(n, dim)`, L2-normalized. Default loads `SentenceTransformer` the way `build()` does.

- [ ] **Step 1: Write the failing check**

Save as `/tmp/rebuild_one_check.py`. It must not import the user’s index. Use `tempfile.mkdtemp()` for both the docs dir and the index dir.

Construct two `Chunk` values, `keep.md` and `old.md`, and a `float32` matrix of shape `(2, 4)` that is L2-normalized. Write them with `faiss.IndexFlatIP`, `pickle`, and a `config.json` that includes `digests` from `library.file_digest`, `files`, `embedding_model` equal to `build_index.EMBEDDING_MODEL`, and an `indexed_at` in the past.

```python
def fake_encode(texts):
    import numpy as np
    rows = []
    for i, _text in enumerate(texts):
        vec = np.zeros(4, dtype="float32")
        vec[i % 4] = 1.0
        rows.append(vec)
    return np.vstack(rows)
```

Assertions:

- After `rebuild_paths(["old.md"], docs_dir=docs, index_dir=index, encode=fake_encode)` with `old.md` rewritten to a new `# Title\n\n## Section body\n`, the reloaded pickle contains `keep.md` and `old.md`, the `keep.md` vector equals the original row, and `config["digests"]["keep.md"]` equals the digest written at setup.
- `config["digests"]["old.md"]` equals `file_digest` of the new bytes. `indexed_at` differs from the setup value.
- `rebuild_paths(["keep.md", "old.md"], ...)` after deleting both files raises `IndexBuildError` and the SHA-256 of `chunks.faiss` is unchanged. Compute the hash before the call.
- Use a second temp index for the mismatch case. A pickle of one chunk beside an index with `ntotal == 2` causes `rebuild_paths` to call `build`. Monkeypatch `build_index.build` with a lambda that records the arguments and returns `{"files": [], "indexed_at": "x", "num_chunks": 0, "digests": {}}`. Assert it was called and that `fake_encode` was not.
- Use a third temp index for the traversal case. `rebuild_paths(["../x.md"], ...)` raises `UnsafeNameError` and does not call `encode`.

Run: `./.venv/bin/python /tmp/rebuild_one_check.py`
Expected: FAIL because `rebuild_paths` is missing.

- [ ] **Step 2: Implement**

Give `build` the two optional path arguments and use them in place of the globals inside the function. Extract the `.tmp` write and replace into `_publish(index_dir, index, chunks, config)` and call it from `build` and from `rebuild_paths`.

`rebuild_paths`:

1. `rels = sorted({safe_md_relpath(raw) for raw in paths})`. Empty raises `IndexBuildError("No files to re-index.")`.
2. If `chunks.faiss` is missing, `return build(docs_dir, index_dir)`.
3. Load the index, the pickle, and `config.json`. If `index.ntotal != len(chunks)` or the chunks are not a list, `return build(docs_dir, index_dir)`.
4. If `config["embedding_model"]` is present and is not `EMBEDDING_MODEL`, raise `IndexBuildError` with the same wording `reload()` uses for a model mismatch. Do not splice.
5. Split kept vs dropped. Reconstruct each kept row. Read each requested path that exists and extend `chunk_document(path, source_file=rel)`.
6. If the kept list plus the new list is empty, raise `IndexBuildError("No chunks to index.")` before creating `.tmp`.
7. Encode only the new texts with `encode or _default_encode`. If that array’s second dimension is not `index.d`, raise `IndexBuildError` before `.tmp`.
8. Stack kept rows under the new rows, `IndexFlatIP.add`, then `_publish`.
9. Digests: start from the previous dict. Delete every requested rel. Set a new digest for each requested rel that still exists and contributed no special case — set it whenever the file exists. Leave digests of unrequested files as they were. `files` is `sorted({c.source_file for c in all_chunks})`.

`__main__` still calls `build()` with no arguments.

- [ ] **Step 3: Run the check**

Run: `./.venv/bin/python /tmp/rebuild_one_check.py`
Expected: a final line `rebuild ok` that the script prints after the assertions.

- [ ] **Step 4: Commit**

```bash
git add src/build_index.py
git commit -m "feat: re-index one file without embedding the rest"
```

### Task 4: Re-index route and the file button

**Files:**
- Modify: `src/server.py`
- Modify: `src/static/library.html`
- Modify: `src/static/sw.js`

**Interfaces:**
- Consumes: `rebuild_paths(paths) -> dict` and `build() -> dict`
- `POST /api/reindex` with no JSON body, or with `{}`, calls `build()`.
- `POST /api/reindex` with `{"paths": ["hr/pto.md"]}` calls `rebuild_paths`.

- [ ] **Step 1: Write the failing check**

Use FastAPI `TestClient` only if you can patch `server._rebuild_sync` before the request so the real model and the real index are never touched. The check:

```python
import server

seen = {}

def fake_rebuild(paths=None):
    seen["paths"] = paths
    return {"num_chunks": 1, "indexed_at": "t", "files": ["a.md"]}

server._rebuild_sync = fake_rebuild
```

If the route calls `_rebuild_sync` via the global name from inside the module, this patch works. Assert:

- A builder that posts no body records `paths is None`.
- A builder that posts `{"paths": ["a.md"]}` with `Content-Type: application/json` records `["a.md"]`.
- `{"paths": ["../a.md"]}` never calls the fake if validation runs first; it is HTTP 400. Do that validation in the route with `safe_md_relpath` before starting the thread, so a bad path does not set `_rebuilding` and then fail inside the worker. If you instead let `rebuild_paths` raise, the existing `IndexBuildError` / `UnsafeNameError` handlers must still return 400 and clear `_rebuilding`.
- A list of 21 strings is HTTP 400.
- The no-body response stays `{"ok": True, "num_chunks", "indexed_at", "files"}`.

The session middleware will 401 a bare `TestClient`. Call the parsing helper directly if you factor one, and assert the helper:

```python
def reindex_paths(payload):
    """None means full build. A list is the file set. Bad shapes raise ValueError."""
```

Test that helper without the lock: `None` for missing body, the list for a valid body, `ValueError` for 21 paths and for a non-string entry. Then test `safe_md_relpath("../a.md")` raises. That covers the route’s decisions without the password.

- [ ] **Step 2: Implement the route**

Change `_rebuild_sync` to take `paths: list[str] | None = None` and call `rebuild_paths(paths)` when `paths` is not None. `api_reindex` reads a JSON body only when the content type starts with `application/json`. No body means full `build()`. `paths` longer than 20, or a non-list, or a non-string entry, is HTTP 400 `"Send at most 20 files."` for the length and `"No files to re-index."` for an empty list. Run `safe_md_relpath` on each entry before setting `_rebuilding`; `UnsafeNameError` is HTTP 400. The worker still runs under `asyncio.to_thread`, and `reload()` still runs after a successful return. Failures still clear `_rebuilding` in `finally`.

Add a file-row button `Re-index file`. Confirm with `Re-index this file? Search updates for this file only.` POST `{"paths": [name]}` as JSON. On 409, use the same rebuild lock and poll the page button uses. On 400, show `detail`. The page-level button keeps posting with no body.

Set `CACHE` to `ask-northwind-v16`, or one higher than the current integer if `v15` is already gone.

- [ ] **Step 3: Run the check**

Run the helper check from `src/`. Expected: `reindex ok`

- [ ] **Step 4: Commit**

```bash
git add src/server.py src/static/library.html src/static/sw.js
git commit -m "feat: re-index a single library file"
```

### Task 5: Docs

**Files:**
- Modify: `AGENTS.md`, `src/README.md`, `src/LEARNING.md`

- [ ] **Step 1: Record the three behaviors**

Say an upload while a folder pill is selected is staged at `folder/basename.md`, and Approve still does not rebuild. Say `[[target]]` and `[[target|label]]` open a resolved note in the preview, ambiguous stems stay text, and `![[...]]` stays text. Say `POST /api/reindex` with `{"paths": [...]}` embeds those files only, copies the other vectors and the other digests, refuses an empty corpus without replacing the index, and falls back to a full `build()` when the faiss row count disagrees with the pickle. Say the page button still rebuilds everything. Cache name `ask-northwind-v16` wherever the current name is written.

- [ ] **Step 2: Commit**

```bash
git add AGENTS.md src/README.md src/LEARNING.md
git commit -m "docs: describe folder upload, wikilinks, and one-file re-index"
```

Delete `/tmp/folder_upload_check.py`, `/tmp/wikilink_check.py`, and `/tmp/rebuild_one_check.py` after they pass.

# Retrieval Trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show how an answer was retrieved, badge a source whose file is not the indexed copy, offer heading questions on an empty thread, and let Library open Ask with a draft.

**Architecture:** `retrieve_explained` performs today’s vector-then-rerank gate and also returns a short score trace with no chunk text. `answer_stream` puts that object on the existing sources event. The browser paints it, paints per-file index state from `GET /api/docs`, and lists `heading_questions` without calling a model. Library links into `/?scope=&q=`, which Ask applies once and does not send.

**Tech Stack:** Existing `retrieve.py`, `generate.py`, `library.py`, FastAPI, vanilla JS. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-02-ask-northwind-next-design.md` (sections Retrieval trace, Changed source, Heading suggestions, Library to Ask)

## Global Constraints

- Run from `src/` with `./.venv/bin/python`. No pytest, no new HTML route, no new dependency.
- Do not write the user’s `DOCS_DIR`, `src/review/`, `src/index/`, `src/sessions.json`, or `src/.env`.
- `retrieve()` still returns `list[RetrievedChunk]`. With `hybrid=False`, that list matches today’s `retrieve()`.
- Do not call the LLM from `heading_questions` or from applying `?q=`.
- `RERANK_TRUST` stays `0.0`. Briefs contain no chunk text. At most 5 briefs per list.
- Ignore `q` longer than 400 characters. Ignore a scope that contains `..`, a hidden segment, `\`, NUL, or an absolute path.
- Service worker: `ask-northwind-v14` becomes `ask-northwind-v15`. If `CACHE` is already newer, add 1.
- Citation highlighting, `Follow-up searched as`, and Ask’s existing **Ask about this file** stay.

## Review Focus

- A stored turn with no `trace` still shows its excerpts and does not show `How this was retrieved`.
- A file whose digest matches the index gets no badge, including when its mtime is newer. The badge uses `docs[].state`.
- `/?q=` longer than 400 characters does not fill the textarea and does not send a question.
- `/?scope=../secret.md` does not change Search in.
- Two files with headings produce suggestions from both files before a second heading from the first, and the buttons do not submit.

---

### Task 1: Explained retrieval

**Files:**
- Modify: `src/retrieve.py`

**Interfaces:**
- Produces: `retrieve_explained(query: str, top_k: int = 4, candidate_pool: int = 20, use_reranker: bool = True, trust_gate: bool = True, scope: str | None = None, hybrid: bool = False) -> tuple[list[RetrievedChunk], dict]`
- Produces: `retrieve(...)` unchanged for callers. It calls `retrieve_explained` with `hybrid=False` and returns the chunk list.
- Trace keys: `best_logit: float | None`, `trusted: bool`, `rank_source: str`, `vector: list[dict]`, `rerank: list[dict]`, `lexical: list[dict]`.
- Brief keys: `source_file: str`, `heading: str`, `vector_score: float`, `rerank_score: float | None`, `lexical_score: float | None`.

- [ ] **Step 1: Write the failing check**

Save as `/tmp/trace_check.py` and run from `src/`:

```python
import retrieve
from retrieve import RetrievedChunk, retrieve, retrieve_explained

def hit(i, source, vec):
    return RetrievedChunk(f"id{i}", source, f"text {i}", vec, heading=f"H{i}")

def fake_vector(query, top_k=20, scope=None):
    return [hit(1, "a.md", 0.9), hit(2, "b.md", 0.2), hit(3, "c.md", 0.1)]

def fake_rerank(query, candidates):
    # a.md -6, b.md -1, c.md -2. Best logit is -1, so the gate is not trusted.
    for chunk, score in zip(candidates, [-6.0, -1.0, -2.0]):
        chunk.rerank_score = score
    return sorted(candidates, key=lambda c: c.rerank_score, reverse=True)

retrieve.vector_search = fake_vector
retrieve.rerank = fake_rerank

chunks, trace = retrieve_explained("q", top_k=2, trust_gate=True)
assert [c.source_file for c in chunks] == ["a.md", "b.md"]
assert chunks[0].rank_source == "vector"
assert trace["trusted"] is False
assert trace["best_logit"] == -1.0
assert trace["rank_source"] == "vector"
assert trace["lexical"] == []
assert len(trace["vector"]) <= 5 and "text" not in trace["vector"][0]
assert trace["vector"][0]["source_file"] == "a.md"
assert trace["rerank"][0]["source_file"] == "b.md"

same = retrieve("q", top_k=2, trust_gate=True)
assert [c.source_file for c in same] == [c.source_file for c in chunks]

def positive_rerank(query, candidates):
    for chunk, score in zip(candidates, [-6.0, 1.5, -1.0]):
        chunk.rerank_score = score
    return sorted(candidates, key=lambda c: c.rerank_score, reverse=True)

retrieve.rerank = positive_rerank
ungated, ungated_trace = retrieve_explained("q", top_k=2, trust_gate=False)
assert [c.source_file for c in ungated] == ["b.md", "c.md"]
assert ungated_trace["trusted"] is True
assert ungated_trace["best_logit"] == 1.5
assert ungated_trace["rank_source"] == "rerank"

plain = retrieve_explained("q", top_k=2, use_reranker=False)
assert plain[1]["best_logit"] is None
assert plain[1]["rerank"] == []
assert [c.source_file for c in plain[0]] == ["a.md", "b.md"]
print("trace ok")
```

Run: `./.venv/bin/python /tmp/trace_check.py`
Expected: FAIL because `retrieve_explained` is missing.

- [ ] **Step 2: Implement**

Add this helper and this function to `src/retrieve.py`. Keep `vector_search`, `rerank`, `prompt_excerpts`, and the constants. Replace the body of `retrieve` so it is a one-line wrapper.

```python
def _brief(chunks: list[RetrievedChunk], limit: int = 5) -> list[dict]:
    rows = []
    for chunk in chunks[:limit]:
        rows.append(
            {
                "source_file": chunk.source_file,
                "heading": chunk.heading,
                "vector_score": float(chunk.vector_score),
                "rerank_score": None if chunk.rerank_score is None else float(chunk.rerank_score),
                "lexical_score": None,
            }
        )
    return rows


def _empty_trace(source: str = "vector") -> dict:
    return {
        "best_logit": None,
        "trusted": False,
        "rank_source": source,
        "vector": [],
        "rerank": [],
        "lexical": [],
    }


def retrieve_explained(
    query: str,
    top_k: int = 4,
    candidate_pool: int = 20,
    use_reranker: bool = True,
    trust_gate: bool = True,
    scope: str | None = None,
    hybrid: bool = False,
) -> tuple[list[RetrievedChunk], dict]:
    """Same selection as retrieve(), plus a score trace that holds no chunk text.

    `hybrid` is accepted and ignored until lexical fusion lands. False keeps
    the vector candidate pool, which is the pool eval measures.
    """
    del hybrid  # reserved for the hybrid-search plan
    vector_hits = vector_search(query, top_k=candidate_pool, scope=scope)
    if not use_reranker:
        chosen = vector_hits[:top_k]
        trace = _empty_trace("vector")
        trace["vector"] = _brief(vector_hits)
        return chosen, trace
    ranked = rerank(query, list(vector_hits))
    best = ranked[0].rerank_score if ranked else None
    trusted = best is not None and best >= RERANK_TRUST
    if trust_gate and not trusted:
        shipped = sorted(ranked, key=lambda c: c.vector_score, reverse=True)
        source = "vector"
    else:
        shipped = ranked
        source = "rerank"
    for chunk in shipped:
        chunk.rank_source = source
    trace = {
        "best_logit": None if best is None else float(best),
        "trusted": trusted,
        "rank_source": source,
        "vector": _brief(vector_hits),
        "rerank": _brief(ranked),
        "lexical": [],
    }
    return shipped[:top_k], trace


def retrieve(
    query: str,
    top_k: int = 4,
    candidate_pool: int = 20,
    use_reranker: bool = True,
    trust_gate: bool = True,
    scope: str | None = None,
    hybrid: bool = False,
) -> list[RetrievedChunk]:
    chunks, _trace = retrieve_explained(
        query,
        top_k=top_k,
        candidate_pool=candidate_pool,
        use_reranker=use_reranker,
        trust_gate=trust_gate,
        scope=scope,
        hybrid=hybrid,
    )
    return chunks
```

`vector_hits` must stay in vector order for the trace. `rerank()` returns a new sorted list and writes scores onto the same objects. Pass `list(vector_hits)` into it. The vector brief is then still `a.md`, `b.md`, `c.md`, while the rerank brief starts with `b.md` (logit `-1`).

`best_logit` is the max rerank score on that pool, including when the gate then restores vector order. In the failing check that max is `-1.0`, not the logit of the chunk that ends up first (`-6.0` on `a.md`).

- [ ] **Step 3: Run the check**

Run: `./.venv/bin/python /tmp/trace_check.py`
Expected: `trace ok`

- [ ] **Step 4: Commit**

```bash
git add src/retrieve.py
git commit -m "feat: return a retrieval trace without changing retrieve()"
```

### Task 2: Sources event carries the trace

**Files:**
- Modify: `src/generate.py` (import and `answer` / `answer_stream`)

**Interfaces:**
- Consumes: `retrieve_explained(...) -> tuple[list[RetrievedChunk], dict]`
- Produces: sources event `{"type", "ranking", "scope", "trace", "chunks"}`. `answer()` ignores the trace and still returns `(text, chunks)` after `prompt_excerpts`.

- [ ] **Step 1: Write the failing check**

```python
import generate

seen = {}

def fake_explained(query, top_k=4, scope=None, hybrid=False, **kwargs):
    from retrieve import RetrievedChunk
    chunk = RetrievedChunk("id", "pto_policy.md", "window", 0.4, rerank_score=1.2, heading="PTO > Accrual", rank_source="rerank")
    seen["query"] = query
    seen["hybrid"] = hybrid
    return [chunk], {"best_logit": 1.2, "trusted": True, "rank_source": "rerank", "vector": [], "rerank": [], "lexical": []}

generate.retrieve_explained = fake_explained
generate.llm_stream = lambda *args, **kwargs: iter(["Because [1]."])
generate.load_settings = lambda: None

events = list(generate.answer_stream("How many PTO days?", history=[]))
sources = next(event for event in events if event["type"] == "sources")
assert sources["trace"]["best_logit"] == 1.2
assert sources["chunks"][0]["source_file"] == "pto_policy.md"
assert seen["hybrid"] is False
assert seen["query"] == "How many PTO days?"
print("stream ok")
```

Change the import in `generate.py` from `retrieve` to `retrieve_explained` before expecting this to pass. Run it first and confirm it fails on the missing name.

Run: `./.venv/bin/python /tmp/stream_trace_check.py` from `src/` after writing the script to `/tmp`.
Expected: FAIL with `retrieve_explained` missing on `generate`.

- [ ] **Step 2: Implement**

In `src/generate.py`, import `retrieve_explained` beside `prompt_excerpts`. Remove the `retrieve` import if nothing else in the file uses it.

In `answer` and `answer_stream`, replace `retrieve(...)` with:

```python
chunks, trace = retrieve_explained(query, top_k=top_k, scope=scope, hybrid=False)
chunks = prompt_excerpts(chunks)
```

`answer()` keeps returning `(text, chunks)` and does not return `trace`. Add `"trace": trace` to the sources yield. Do not put chunk text into `trace`.

- [ ] **Step 3: Run the check**

Run: `./.venv/bin/python /tmp/stream_trace_check.py`
Expected: `stream ok`

- [ ] **Step 4: Commit**

```bash
git add src/generate.py
git commit -m "feat: send the retrieval trace with the sources event"
```

### Task 3: Paint the trace

**Files:**
- Modify: `src/static/index.html`
- Modify: `src/static/app.css`
- Modify: `src/static/sw.js`

**Interfaces:**
- Consumes: `event.trace` as specified in Task 1. Absent `trace` means an older stored turn.

- [ ] **Step 1: Implement the panel**

In `showSources`, after the scope and follow-up lines and before the excerpt cards, append a details element only when `turn.trace` is an object:

- `<details class="trace">` with `<summary>How this was retrieved</summary>`
- One paragraph. When `best_logit` is a number and `trusted` is true: `Best rerank score 1.20. Reranker order shipped.` When it is a number and `trusted` is false: `Best rerank score -6.21. Below 0, so vector order shipped.` When it is null: `No rerank score. Vector order shipped.` Format the logit with two digits.
- A list labeled `Nearest vectors` from `trace.vector`. Each item is `source_file — heading — vector_score` with the score fixed to two digits. Use `textContent`.
- When `trace.rerank` is non-empty, a list labeled `Reranker order` using `rerank_score`.
- When `trace.lexical` is non-empty, a list labeled `Lexical matches` using `lexical_score`.

Store `event.trace` on the turn in the sources handler. Add `trace` to `snapshotTurn` when it is a plain object whose lists are arrays; drop unknown keys. Cap each list at 5 briefs and keep only the five brief fields. A turn loaded from storage without `trace` leaves `turn.trace` null.

CSS, scoped so it does not restyle Library:

```css
.trace { margin: 0 0 0.75rem; }
.trace summary { cursor: pointer; color: var(--muted); }
.trace ol { margin: 0.25rem 0 0.6rem; padding-left: 1.2rem; }
.trace li { font-size: 0.85rem; }
```

Set `CACHE` to `ask-northwind-v15`.

- [ ] **Step 2: Check the snapshot helper**

The snapshot logic lives in the page. Extract nothing. Confirm by reading `snapshotTurn` that a trace without `vector` becomes `{best_logit, trusted, rank_source, vector: [], rerank: [], lexical: []}` and that a brief’s extra `text` field is not copied. Confirm `showSources` uses `textContent` for file names.

- [ ] **Step 3: Commit**

```bash
git add src/static/index.html src/static/app.css src/static/sw.js
git commit -m "feat: show how an answer was retrieved"
```

### Task 4: Badge a source that is not the indexed file

**Files:**
- Modify: `src/static/index.html`
- Modify: `src/static/app.css`

**Interfaces:**
- Consumes: `GET /api/docs` rows `{name, state}` and the status payload’s `stale` boolean.

- [ ] **Step 1: Implement**

Keep a `Map` from doc name to `state`, filled in `loadVault`. When `setIndexNote(true)` runs, call `loadVault()` again so a change made after the page opened updates the map. After `showSources` builds a card, and from a new `paintFileState()` walked over `#thread .chunk`, set the badge from the map:

| state | text |
|---|---|
| `changed` | Changed since Re-index |
| `not_indexed` | Not in the index |
| `missing_on_disk` | Missing from the library |

The badge is an anchor, `class="file-state"`, `href` `/library?doc=` plus `encodeURIComponent(name)`. No badge for `indexed` or a name the map does not hold. Repaint after `loadVault` resolves.

```css
a.file-state { color: var(--amber); font-size: 0.8rem; margin-left: 0.4rem; }
```

- [ ] **Step 2: Check the state rule**

In the browser console is not required. Re-read the branch: digest-matched files arrive as `indexed` from `list_docs` and get no badge. The page does not compare mtimes itself.

- [ ] **Step 3: Commit**

```bash
git add src/static/index.html src/static/app.css
git commit -m "feat: badge a source that differs from the index"
```

### Task 5: Heading suggestions

**Files:**
- Modify: `src/library.py`
- Modify: `src/server.py` (`api_docs`)
- Modify: `src/static/index.html`
- Modify: `src/static/app.css`

**Interfaces:**
- Produces: `heading_questions(scope: str | None = None, docs_dir: Path | None = None, limit: int = 5) -> list[dict]`
- Each dict is `{"question": str, "scope": str}`.
- `GET /api/docs?suggest=` adds `suggestions`. Omitted `suggest` does not add the key. Invalid `suggest` is HTTP 400 via `clean_scope`.

- [ ] **Step 1: Write the failing check**

```python
import tempfile
from pathlib import Path
import library

root = Path(tempfile.mkdtemp())
(root / "a.md").write_text("# A\n\n## First heading here\n\n## Second heading here\n", encoding="utf-8")
(root / "b.md").write_text("# B\n\n## Other heading here\n", encoding="utf-8")
(root / "skip.md").write_text("# S\n\n## Tiny\n", encoding="utf-8")
got = library.heading_questions(docs_dir=root, limit=5)
assert got[0] == {"question": "First heading here", "scope": "a.md"}
assert got[1] == {"question": "Other heading here", "scope": "b.md"}
assert got[2]["question"] == "Second heading here"
assert all(item["question"] != "Tiny" for item in got)
assert library.heading_questions(scope="b.md", docs_dir=root) == [
    {"question": "Other heading here", "scope": "b.md"}
]
print("headings ok")
```

Run from `src/`: `./.venv/bin/python /tmp/headings_check.py`
Expected: FAIL because `heading_questions` is missing.

- [ ] **Step 2: Implement `heading_questions`**

Put it in `src/library.py` next to `search_phrases`. Use `markdown_paths`. A private `_in_scope(source_file, scope)` copies the rule in `retrieve.in_scope`: empty scope matches everything, a `.md` scope is an exact path, anything else is that folder prefix. Do not import `retrieve`.

Heading rule: the line starts with `## `, then strip backticks and `*`. Length 8 through 120 inclusive. At most 8 headings kept per file. Round-robin across files in path order, one heading per file per pass, stop at `limit`.

- [ ] **Step 3: Wire the query param**

`api_docs` already takes `q: str = ""`. Add `suggest: str | None = None`. When the argument is `None` (the client omitted it), do not set `suggestions`. When it is present, including `""`, call `heading_questions(scope or None)` after `clean_scope` on a non-empty value. `UnsafeNameError` becomes HTTP 400 with the existing message `Choose a folder or a file.`

FastAPI treats a missing query param as the default. Use default `None` so `?suggest=` arrives as `""` and a missing param stays `None`. Confirm that with one `TestClient` call in the check below.

- [ ] **Step 4: Run both checks**

Repeat the library check. Then, from `src/`, a `TestClient` against `server.app` after patching `server.list_docs` if the import path requires the app not to touch a real vault. `GET /api/docs` has no `suggestions` key. `GET /api/docs?suggest=../x` is 400. Do not log in if the test client is behind the session middleware: either call `heading_questions` only in the first check and call the route function’s branching with a direct test of a tiny wrapper, or unlock with the test app’s password from the environment the TestClient process sees. Prefer calling `library.heading_questions` and a direct unit of the suggest-branch if the middleware blocks anonymous `TestClient`. The branch itself:

```python
def suggestions_for(suggest):
    if suggest is None:
        return None
    scope = None if suggest == "" else clean_scope(suggest)
    return heading_questions(scope)
```

Put that branch inline in `api_docs` rather than as a new public wrapper, and test `clean_scope("../x")` raises `UnsafeNameError` plus the heading check. That pins the two failure modes without opening the lock.

- [ ] **Step 5: Paint the buttons**

Under `#empty-ask`, add `<div id="suggestions" hidden></div>`. `loadVault` fetches `/api/docs?suggest=` plus `encodeURIComponent(scopeEl.value)`. On `change` of `#scope`, refetch only the suggestions (a second request is fine). Render up to 5 buttons, `type="button"`, `class="ghost suggest"`. `textContent` is the question. The click sets the textarea, calls the same pin path as `askAboutFile` when `scope` ends with `.md`, focuses the textarea, and does not call `form.requestSubmit`. Hide the row when the thread is non-empty or the list is empty. Show it again when New chat clears the thread.

```css
#suggestions { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.75rem 0 0; }
```

- [ ] **Step 6: Commit**

```bash
git add src/library.py src/server.py src/static/index.html src/static/app.css
git commit -m "feat: suggest headings from the mounted notes"
```

### Task 6: Library opens Ask with a draft

**Files:**
- Modify: `src/static/library.html`
- Modify: `src/static/index.html`

**Interfaces:**
- Consumes: file row `doc.name`. Preview body text for lines that start with `## `.
- Produces: navigation to `/?scope=` and `/?scope=&q=`. Ask applies each at most once.

- [ ] **Step 1: Library controls**

In the file-row actions, add an anchor `Ask` with `class="ghost"`, href `/?scope=` + `encodeURIComponent(doc.name)`.

When the library preview finishes rendering a note, build a list from that response’s `body` lines that start with `## `. Each item is an anchor `Ask: ` plus the heading text, href `/?scope=` + `encodeURIComponent(name)` + `&q=` + `encodeURIComponent(heading)`. Heading text is the line with the `## ` prefix removed and trimmed. Skip headings longer than 400 characters. Put the list in an element under the preview meta, replaced on every open. Use `textContent` for the heading.

- [ ] **Step 2: Ask applies the query once**

On `startAsk`, read `URLSearchParams`. A scope is rejected when it fails any of: contains `\\` or `\0`, `Path`-style parts include `.`, `..`, or a part starting with `.`, or it starts with `/`. A rejected scope is ignored. An accepted `.md` scope is pinned the way `askAboutFile` pins `previewName`, after `loadVault` fills folders. An accepted folder scope is selected when that option exists. `q` is applied only when its length is 1 through 400, into the textarea, with no submit. Then `history.replaceState(null, "", "/")`.

A stored empty `q` and a 401-character `q` leave the textarea empty. Confirm that by reading the branch, and by this node-free extract copied into the check script as a Python mirror of the same predicate so the predicate cannot drift:

```python
def accept_scope(value: str) -> bool:
    if not value or "\x00" in value or "\\" in value:
        return False
    if value.startswith("/"):
        return False
    parts = value.split("/")
    return all(part and part not in (".", "..") and not part.startswith(".") for part in parts)

def accept_q(value: str) -> bool:
    return 1 <= len(value) <= 400

assert accept_scope("../secret.md") is False
assert accept_scope("hr/pto.md") is True
assert accept_q("x" * 401) is False
assert accept_q("How many PTO days do I get per year?") is True
```

Implement the JS with those two predicates, not a looser check. Run the Python mirror from `src/` and expect `ok` printed by the script.

- [ ] **Step 3: Commit**

```bash
git add src/static/library.html src/static/index.html
git commit -m "feat: open Ask from a library note or heading"
```

### Task 7: Docs

**Files:**
- Modify: `AGENTS.md`, `src/README.md`, `src/LEARNING.md`

- [ ] **Step 1: Document the visible behavior**

In the Ask UI section of each file, state that a turn can show `How this was retrieved` (best logit, vector briefs, rerank briefs), that a source badge reads `Changed since Re-index`, `Not in the index`, or `Missing from the library`, that an empty thread lists up to five `##` headings and does not send them, and that Library’s Ask link and heading links fill Search in and the draft. State that `GET /api/docs?suggest=` returns `suggestions` and that a bad `suggest` is HTTP 400. State the cache name `ask-northwind-v15` wherever the current cache name is written. Do not describe hybrid search; that plan has not shipped.

- [ ] **Step 2: Commit**

```bash
git add AGENTS.md src/README.md src/LEARNING.md
git commit -m "docs: describe the retrieval trace and heading drafts"
```

Delete `/tmp/trace_check.py`, `/tmp/stream_trace_check.py`, and `/tmp/headings_check.py` when the checks have passed.

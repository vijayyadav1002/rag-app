# Self-Updating RAG Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stage Library uploads, edits, and deletes for human approval, and send a relevant hit's `##` section to the answer prompt instead of only its 800-character window.

**Architecture:** Proposals are JSON files in `src/review/`, outside the corpus. Approve writes the live Markdown atomically. Re-index is still the only search update. `prompt_excerpts()` runs after rerank, inside `generate.py`, so `eval.py` still measures the same top-k windows.

**Tech Stack:** Python 3.12, FastAPI, existing `llm.complete`, FAISS, no new dependencies, no pytest.

**Spec:** `docs/superpowers/specs/2026-09-23-self-updating-rag-design.md`

## Global Constraints

- Run from `src/` with `./.venv/bin/python`. Python 3.12.
- No pytest, no `tests/` package, no new HTML route, no auth, no auto-reindex, no Pydantic, no new dependency.
- Upload stays basename-only, 1 MB, 20 files, field name `files`. Nested list/delete paths stay posix-relative.
- `CHUNK_SIZE` stays 800. `RERANK_FLOOR = 0.0`, `RERANK_MARGIN = 1.0`, `SECTION_PROMPT_MAX = 4000`, `FORMAT_MAX_TOKENS = 4096`, answer `MAX_TOKENS` stays 500.
- Recall at 3 stays 100% for vector search and rerank.
- Service worker cache name becomes `ask-northwind-v7`.
- Do not write the real `DOCS_DIR` or commit `src/review/` or `index/`.
- Offline Library copy stays `You're offline.`

---

### Task 1: Section text on chunks

**Files:**
- Modify: `src/chunk.py`

**Interfaces:**
- Consumes: existing `Chunk` and `_sliding_window` behavior
- Produces: `SECTION_PROMPT_MAX = 4000`; `Chunk.section_text: str = ""`; window `Chunk.text` unchanged for the same input

- [ ] **Step 1: Write the failing check**

Run from `src/`:

```bash
./.venv/bin/python -c "
from chunk import Chunk, chunk_document
import tempfile, pathlib
p = pathlib.Path(tempfile.mkdtemp()) / 'long.md'
body = 'word ' * 500
p.write_text('# Long\n\n## Topic\n\n' + body)
chunks = chunk_document(p, source_file='long.md')
assert len(chunks) > 1, len(chunks)
assert all(c.section_text.startswith('Long > Topic\n') for c in chunks)
assert len(chunks[0].text) <= 800 + len('Long > Topic\n')
assert 'word' in chunks[0].section_text
print('ok', len(chunks), len(chunks[0].section_text))
"
```

Expected: FAIL because `Chunk` has no `section_text`.

- [ ] **Step 2: Implement**

Add `SECTION_PROMPT_MAX = 4000`. Add `section_text: str = ""` to `Chunk`. Split windows with spans so each chunk can store a 4000-character body slice that contains its window. Prefix `section_text` with `{doc title} > {heading}`. Keep `text` as the heading plus `piece.strip()`.

- [ ] **Step 3: Re-run the check**

Expected: `ok` and a section longer than the window.

### Task 2: Prompt excerpts

**Files:**
- Modify: `src/retrieve.py`
- Modify: `src/generate.py`
- Modify: `src/static/index.html`
- Modify: `src/static/sw.js`

**Interfaces:**
- Consumes: `Chunk.heading`, `Chunk.section_text` via `getattr` for old pickles
- Produces: `RERANK_FLOOR`, `RERANK_MARGIN`, `RetrievedChunk.heading`, `RetrievedChunk.section_text`, `RetrievedChunk.expanded`, `prompt_excerpts(chunks) -> list[RetrievedChunk]`

- [ ] **Step 1: Failing score-table check**

```bash
./.venv/bin/python -c "
from retrieve import RetrievedChunk, prompt_excerpts
def c(cid, score, heading='H', section='SEC', text='WIN'):
    return RetrievedChunk(cid, 'a.md', text, 0.5, score, heading, section, False)
top = c('1', 2.5)
near = c('2', 1.6, heading='H2', section='SEC2')
far = c('3', 0.5, heading='H3', section='SEC3')
weak = c('4', -0.4, heading='H4', section='SEC4')
out = prompt_excerpts([top, near, far, weak])
assert [x.expanded for x in out] == [True, True, False, False]
assert out[0].text == 'SEC' and out[2].text == 'WIN'
same = prompt_excerpts([c('1', 2.5, section='FULL'), c('2', 1.6, section='FULL')])
assert len(same) == 1 and same[0].expanded
both = prompt_excerpts([c('1', -0.2), c('2', -0.4)])
assert len(both) == 2 and not any(x.expanded for x in both)
plain = prompt_excerpts([RetrievedChunk('1', 'a.md', 'WIN', 0.2)])
assert plain[0].text == 'WIN' and not plain[0].expanded
print('ok')
"
```

Expected: FAIL on missing `prompt_excerpts` or fields.

- [ ] **Step 2: Implement `prompt_excerpts` and thread it through `answer` / `answer_stream`**

`sources` chunks gain `"expanded"`. Ask meta line: `` `[${chunk.n}] ${chunk.source_file} · ${chunk.expanded ? "section" : "excerpt"}` ``. Cache name `ask-northwind-v7`.

- [ ] **Step 3: Re-run the check, then `./.venv/bin/python eval.py`**

Expected: check prints `ok`. Eval recall at 3 is 100% for both columns.

### Task 3: Formatter

**Files:**
- Modify: `src/llm.py`
- Create: `src/format_md.py`

**Interfaces:**
- Consumes: `complete(system, user)`
- Produces: `complete(system, user, max_tokens=None)`; `LLMTruncatedError`; `format_md.validate_rag_markdown(text) -> str`; `format_md.format_markdown(text) -> str`; `format_md.FormatError`; `FORMAT_MAX_TOKENS = 4096`

- [ ] **Step 1: Failing validation check**

```bash
./.venv/bin/python -c "
from format_md import FormatError, validate_rag_markdown
ok = validate_rag_markdown('\`\`\`markdown\n# T\n\n## A\n\nHi\n\`\`\`')
assert ok.startswith('# T') and '##' in ok and ok.endswith('\n')
for bad in ['', '## Only\n', '# A\n\n# B\n', '# A\n\n### C\n']:
    try:
        validate_rag_markdown(bad)
    except FormatError:
        continue
    raise SystemExit('accepted ' + bad)
print('ok')
"
```

- [ ] **Step 2: Implement validation and `complete(..., max_tokens=)`**

Answer calls omit `max_tokens` and stay at 500. A finish reason of `max_tokens` or `length` raises `LLMTruncatedError`. `format_markdown` maps that to `FormatError("Formatting hit the token limit.")`. System prompt is the one in the spec.

- [ ] **Step 3: Re-run the check**

Expected: `ok`.

### Task 4: Review records and live-file helpers

**Files:**
- Modify: `src/library.py`
- Create: `src/review.py`
- Modify: `.gitignore` (`src/review/`)

**Interfaces:**
- Consumes: `safe_md_name`, `safe_md_relpath`, `MAX_UPLOAD_BYTES`, `format_md.format_markdown`, `llm.LLMConfigError`
- Produces: `library.read_doc(name) -> dict`; `library.write_doc(name, data) -> str` (atomic `{name}.tmp` then replace); `review.Proposal`; `review.list_proposals() -> tuple[list[Proposal], int]`; `review.stage_upload`; `review.stage_delete`; `review.save_draft`; `review.format_proposal`; `review.approve`; `review.reject`; `review.ConflictError`

- [ ] **Step 1: Failing queue check against temp dirs**

Patch `review.DOCS_DIR`, `review.REVIEW_DIR`, and `library.DOCS_DIR` to temp directories. Patch `format_md.format_markdown` to return `"# T\n\n## S\n\nok\n"`. Assert upload does not create the live file, approve does, a changed `base_mtime_ns` raises `ConflictError` without writing, and a formatter `FormatError` stores `origin == "manual"`.

- [ ] **Step 2: Implement**

`stage_upload` calls `format_md.format_markdown` via the module attribute so tests can patch it. `LLMConfigError` stores `format_error == "No model is configured."`. Approve compares `st_mtime_ns`. Delete of a missing live file drops the record. Save draft creates a manual upsert when absent and refreshes `base_mtime_ns`.

- [ ] **Step 3: Re-run the check**

Expected: live file absent before approve, present after, conflict does not clobber.

### Task 5: HTTP API

**Files:**
- Modify: `src/server.py`

**Interfaces:**
- Consumes: Task 4 functions
- Produces: the HTTP table in the spec. `GET /api/docs` rows include `pending`.

- [ ] **Step 1: TestClient check**

Use temp dirs and a patched formatter. Assert upload status 200 leaves the live path missing, PUT then approve writes the body, reject leaves an existing file, `../x.md` is 400, and `server._rebuilding = True` makes `POST /api/docs` return 409. Restore `_rebuilding` in `finally`.

- [ ] **Step 2: Implement routes**

Map `ConflictError` to 409 with `The live file changed since this draft was staged.` Map `FormatError` and `LLMConfigError` on the format route to 422. Keep 207 for a partial upload batch.

- [ ] **Step 3: Re-run TestClient**

Expected: all assertions pass. No files written under the real corpus.

### Task 6: Library and Ask UI

**Files:**
- Modify: `src/static/library.html`
- Modify: `src/static/app.css`
- Modify: `src/README.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: the HTTP API
- Produces: the Review panel and copy in the spec

- [ ] **Step 1: Implement the page**

Review panel above the file list. Editor textarea. Format and Approve save the textarea first. Pending brass label. Confirm copy: `Stage deletion of {name}? It stays on disk and searchable until you approve it and re-index.` Upload status: `Staged for review.` Encode each path segment separately so nested names keep their slashes.

- [ ] **Step 2: Update README and AGENTS.md**

Describe staging, Approve, and Re-index. Name `format_md.py`, `review.py`, and the expansion constants.

- [ ] **Step 3: Fetch `/library` from a running server**

Expected HTML contains `Review`, `Save draft`, and `Staged` is not required in the static file; the static file contains `No pending changes.` and `Stage deletion of`.

### Task 7: Eval gate

- [ ] **Step 1: Run `./.venv/bin/python eval.py` from `src/`**

Expected: recall at 3 is 100% with and without the reranker. Do not lower the gate if a query moves.

# Confident rerank and stale-index plan

**Goal:** Keep rerank order only when the cross-encoder’s best score is at least 0, and show on Ask and Library when the corpus is ahead of the index.

**Architecture:** `retrieve()` still does vector top 20, then MiniLM. `RERANK_TRUST = 0.0` decides whether that order ships. `list_docs` compares each indexed file’s mtime with `indexed_at`. `GET /api/status` reports `stale`. No new dependency and no auto-reindex.

**Spec:** `docs/superpowers/specs/2026-09-25-confident-rerank-and-stale-index-design.md`

## Constraints

- Run from `src/` with `./.venv/bin/python`. No pytest, no new route, no new dependency.
- Recall at 3 stays 100% for vector search, ungated rerank, and the shipped gate. Shipped recall at 1 on the current 20 questions is 100%.
- Do not write the real `DOCS_DIR`, `src/review/`, or `index/`.
- Service worker cache name becomes `ask-northwind-v8`.
- `RERANK_FLOOR`, `RERANK_MARGIN`, and `SECTION_PROMPT_MAX` stay 0.0, 1.0, and 4000.

### Task 1: Confidence gate

Modify `src/retrieve.py`, `src/eval.py`, `src/generate.py`, `src/cli.py`.

- Add `RERANK_TRUST = 0.0` and `RetrievedChunk.rank_source`.
- `retrieve(..., trust_gate=True)` restores vector order when the best logit is below the trust line. `trust_gate=False` keeps today’s rerank order for the eval column.
- `prompt_excerpts` takes the best logit from the returned chunks, not “whichever chunk is first.”
- Sources events include `ranking`. The CLI header names which order shipped.
- `eval.py` prints vector only, rerank order, and confident, plus the @1 misses for each.

### Task 2: Stale library

Modify `src/library.py`, `src/server.py`, `src/static/index.html`, `src/static/library.html`, `src/static/app.css`, `src/static/sw.js`.

- `changed` when an indexed file’s mtime is after `indexed_at`.
- `/api/status` includes `stale`.
- Ask shows the note and refreshes it when the tab becomes visible. Library shows the badge and a Re-index hint.
- Bump the service worker cache to `ask-northwind-v8`.

### Task 3: Docs and checks

Update `src/README.md`, `src/LEARNING.md`, and `AGENTS.md` so the 95% story describes ungated rerank, and the shipped path is the gate.

Check with `eval.py`, a temp-directory `list_docs` script, and `TestClient` against `GET /api/status` without writing the real corpus. Load Ask and Library and confirm the new copy is in the HTML the server returns.

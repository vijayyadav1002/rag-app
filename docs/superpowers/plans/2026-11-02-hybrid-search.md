# Hybrid Search Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let exact tokens compete with vectors for a place in the rerank pool, and point Ask at that pool only after the eval bar holds.

**Architecture:** `lexical.py` builds an in-memory Okapi BM25 index over the chunks `reload()` just unpickled. `fuse_rrf` unions the vector top 20 with the BM25 top 20. The cross-encoder and the `RERANK_TRUST` gate then run on that union. `hybrid=False` skips both steps, so the three existing eval columns stay on today’s vector pool.

Depends on `retrieve_explained` from `docs/superpowers/plans/2026-11-02-retrieval-trace.md`. Do not start this plan until that function is in `src/retrieve.py`.

**Tech Stack:** Stdlib plus the existing NumPy, FAISS, and `sentence-transformers` stack. No new dependency.

**Spec:** `docs/superpowers/specs/2026-11-02-ask-northwind-next-design.md` (section Hybrid search)

## Global Constraints

- Run from `src/` with `./.venv/bin/python`. No pytest and no new dependency.
- `k1 = 1.5`, `b = 0.75`, RRF `k = 60`. Tokens match `[a-z0-9]+` after lowercasing. Each query token counts once.
- `retrieve(..., hybrid=False)` stays the default. Eval’s vector, rerank-order, and confident fetches pass `hybrid=False` or omit it.
- Do not edit `TEST_SET`. Do not retune `k1`, `b`, or `k` to chase a miss.
- A query with no tokens, or a scope with no chunks, adds nothing to the pool.
- Scope uses `in_scope`. A chunk outside the scope is not scored.
- Ask and `answer()` pass `hybrid=True` only in Task 4, and only after Task 3’s `eval.py` run meets the bar.
- The bar: existing columns unchanged at the README numbers (95/95/100 at k=1, 100 at k=3 and k=5), confident misses still empty, ungated rerank still misses the laptop-return question, vector still misses the new-hire enrollment question, and the hybrid column is 100% at k=1, k=3, and k=5.
- Do not write `src/index/` during the unit checks. `eval.py` reads the local index and is the exception.

## Review Focus

- A punctuation-only query returns no lexical hits, and the fused pool equals the vector hits.
- A file outside the requested scope never appears in `lexical_search`.
- `hybrid=False` does not call `lexical_search`.
- When two chunks share an RRF score, the higher `vector_score` comes first.
- The laptop-return question still misses in the ungated column and still hits `remote_work_equipment_policy.md` in the confident column.

---

### Task 1: BM25 index

**Files:**
- Create: `src/lexical.py`

**Interfaces:**
- Produces: `tokenize(text: str) -> list[str]`
- Produces: `LexicalIndex(chunks: list)` with `search(self, query: str, top_k: int, allowed: set[int] | None) -> list[tuple[int, float]]`
- Constants: `K1 = 1.5`, `B = 0.75`

- [ ] **Step 1: Write the failing check**

Save as `/tmp/bm25_check.py`:

```python
from lexical import LexicalIndex, tokenize

class Doc:
    def __init__(self, text):
        self.text = text

assert tokenize("E3 error, E3!") == ["e3", "error", "e3"]
docs = [
    Doc("the thermostat shows an e3 error on the panel"),
    Doc("the thermostat shows a code on the panel"),
    Doc("pto accrual is fifteen days"),
]
index = LexicalIndex(docs)
hits = index.search("E3 error", top_k=3, allowed=None)
assert hits[0][0] == 0
assert hits[0][1] > hits[1][1]
assert all(i != 2 for i, _score in hits)
assert index.search("...", top_k=3, allowed=None) == []
scoped = index.search("thermostat", top_k=3, allowed={2})
assert scoped == []
print("bm25 ok")
```

Run from `src/`: `./.venv/bin/python /tmp/bm25_check.py`
Expected: FAIL because `src/lexical.py` does not exist.

- [ ] **Step 2: Implement `src/lexical.py`**

```python
"""Okapi BM25 over the chunk texts already stored in the FAISS metadata.

Dense search misses exact tokens — an error code, a note title, a product
name — that a count of the same tokens catches. This module only scores.
retrieve.py decides whether those hits enter the rerank pool.
"""
from __future__ import annotations

import math
import re

K1 = 1.5
B = 0.75
_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


class LexicalIndex:
    def __init__(self, chunks: list):
        self.tf: list[dict[str, int]] = []
        self.length: list[int] = []
        self.df: dict[str, int] = {}
        for chunk in chunks:
            counts: dict[str, int] = {}
            tokens = tokenize(getattr(chunk, "text", "") or "")
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self.tf.append(counts)
            self.length.append(len(tokens))
            for token in counts:
                self.df[token] = self.df.get(token, 0) + 1
        self.n = len(chunks)
        self.avgdl = (sum(self.length) / self.n) if self.n else 0.0

    def search(self, query: str, top_k: int, allowed: set[int] | None) -> list[tuple[int, float]]:
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms or not self.n or self.avgdl <= 0 or top_k <= 0:
            return []
        scored: list[tuple[float, int]] = []
        for i, counts in enumerate(self.tf):
            if allowed is not None and i not in allowed:
                continue
            score = 0.0
            dl = self.length[i]
            for token in terms:
                freq = counts.get(token, 0)
                if not freq:
                    continue
                n_qi = self.df[token]
                idf = math.log(1.0 + (self.n - n_qi + 0.5) / (n_qi + 0.5))
                denom = freq + K1 * (1.0 - B + B * dl / self.avgdl)
                score += idf * (freq * (K1 + 1.0)) / denom
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [(i, score) for score, i in scored[:top_k]]
```

The check’s third document has no `thermostat` token, so a scope of `{2}` is empty. The first document contains `e3` and the second does not, so `E3 error` ranks document 0 first.

- [ ] **Step 3: Run the check**

Run: `./.venv/bin/python /tmp/bm25_check.py`
Expected: `bm25 ok`

- [ ] **Step 4: Commit**

```bash
git add src/lexical.py
git commit -m "feat: score chunk text with BM25"
```

### Task 2: Fuse into the candidate pool

**Files:**
- Modify: `src/retrieve.py`

**Interfaces:**
- Consumes: `LexicalIndex.search` and `tokenize` from `lexical.py`.
- Produces: `RetrievedChunk.lexical_score: float | None = None`
- Produces: `lexical_search(query: str, top_k: int = 20, scope: str | None = None) -> list[RetrievedChunk]`
- Produces: `fuse_rrf(vector: list[RetrievedChunk], lexical: list[RetrievedChunk], limit: int, k: int = 60) -> list[RetrievedChunk]`
- `retrieve_explained(..., hybrid=True)` uses the fused pool. `hybrid=False` does not call `lexical_search`. The trace’s `lexical` list is empty unless `hybrid` is true and the lexical search returned hits.
- `_brief` copies `chunk.lexical_score` instead of writing `None`.

- [ ] **Step 1: Write the failing check**

Save as `/tmp/fuse_check.py`:

```python
import retrieve
from retrieve import RetrievedChunk, fuse_rrf, retrieve_explained

def chunk(i, source, vec, lex=None):
    row = RetrievedChunk(f"id{i}", source, "t", vec, heading=f"H{i}")
    row.lexical_score = lex
    return row

vector = [chunk(1, "a.md", 0.4), chunk(2, "b.md", 0.9)]
lexical = [chunk(2, "b.md", 0.9, 3.0), chunk(3, "c.md", 0.1, 2.0)]
fused = fuse_rrf(vector, lexical, limit=3, k=60)
assert [c.source_file for c in fused] == ["b.md", "a.md", "c.md"]
assert fused[0].lexical_score == 3.0
# Same RRF contribution (each appears once, at rank 1 of its own list is not
# this case). Force a tie: two chunks each rank 1 in one list only.
left = [chunk(1, "low.md", 0.2)]
right = [chunk(2, "high.md", 0.8)]
tied = fuse_rrf(left, right, limit=2, k=60)
assert [c.source_file for c in tied] == ["high.md", "low.md"]

def boom(*args, **kwargs):
    raise AssertionError("lexical_search should not run")

retrieve.lexical_search = boom
retrieve.vector_search = lambda query, top_k=20, scope=None: [chunk(1, "a.md", 0.5)]
retrieve.rerank = lambda query, candidates: candidates
off, trace = retrieve_explained("q", hybrid=False, use_reranker=False)
assert [c.source_file for c in off] == ["a.md"]
assert trace["lexical"] == []
print("fuse ok")
```

Run: `./.venv/bin/python /tmp/fuse_check.py`
Expected: FAIL because `fuse_rrf` does not exist yet. `RetrievedChunk` also has no `lexical_score` until Step 2; the check may fail there first. Either failure is the right red.

- [ ] **Step 2: Implement the field, the search, and the fuse**

Add `lexical_score: float | None = None` to `RetrievedChunk`.

Build the lexical index at the end of `reload()`, after the pickle load and before the lock swap:

```python
from lexical import LexicalIndex

# inside reload, next to new_chunks:
new_lexical = LexicalIndex(new_chunks)
with _state_lock:
    _index = new_index
    _chunks = new_chunks
    _lexical = new_lexical
```

Initialize `_lexical = None` next to `_chunks`.

```python
RRF_K = 60


def lexical_search(query: str, top_k: int = 20, scope: str | None = None) -> list[RetrievedChunk]:
    _load()
    q_emb = _embedder.encode([QUERY_PREFIX + query], normalize_embeddings=True)
    q_emb = np.asarray(q_emb, dtype="float32")
    with _state_lock:
        lexical = _lexical
        chunks = _chunks
        index = _index
        if lexical is None or not chunks or index is None:
            return []
        allowed = None
        if scope:
            allowed = {
                i
                for i, chunk in enumerate(chunks)
                if in_scope(getattr(chunk, "source_file", ""), scope)
            }
            if not allowed:
                return []
        hits = lexical.search(query, top_k, allowed)
        rows = []
        for idx, score in hits:
            vec = float(np.dot(index.reconstruct(int(idx)), q_emb[0]))
            chunk = chunks[idx]
            rows.append(
                RetrievedChunk(
                    chunk_id=chunk.chunk_id,
                    source_file=chunk.source_file,
                    text=chunk.text,
                    vector_score=vec,
                    heading=getattr(chunk, "heading", "") or "",
                    section_text=getattr(chunk, "section_text", "") or "",
                    lexical_score=float(score),
                )
            )
        return rows


def fuse_rrf(vector, lexical, limit, k: int = RRF_K):
    by_id: dict[str, RetrievedChunk] = {}
    for chunk in vector:
        by_id[chunk.chunk_id] = chunk
    for chunk in lexical:
        existing = by_id.get(chunk.chunk_id)
        if existing is None:
            by_id[chunk.chunk_id] = chunk
        else:
            existing.lexical_score = chunk.lexical_score
    rank_v = {chunk.chunk_id: i + 1 for i, chunk in enumerate(vector)}
    rank_l = {chunk.chunk_id: i + 1 for i, chunk in enumerate(lexical)}
    scored = []
    for cid, chunk in by_id.items():
        score = 0.0
        if cid in rank_v:
            score += 1.0 / (k + rank_v[cid])
        if cid in rank_l:
            score += 1.0 / (k + rank_l[cid])
        scored.append((score, chunk.vector_score, chunk))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [item[2] for item in scored[:limit]]
```

Replace the `del hybrid` body of `retrieve_explained`. Keep the `use_reranker=False` early return, and have it set `trace["lexical"]` from the lexical briefs only when `hybrid` is true. For the rerank path:

```python
vector_hits = vector_search(query, top_k=candidate_pool, scope=scope)
lexical_hits = lexical_search(query, top_k=candidate_pool, scope=scope) if hybrid else []
pool = fuse_rrf(vector_hits, lexical_hits, candidate_pool) if lexical_hits else list(vector_hits)
```

Pass `list(pool)` into `rerank` so the pre-rerank order is still available if you brief it. Vector briefs still come from `vector_hits` (vector order). Lexical briefs come from `lexical_hits`. Change `_brief` so `lexical_score` is `None` when the attribute is missing or None, else `float(chunk.lexical_score)`.

`prompt_excerpts` builds new `RetrievedChunk` values when it expands. Copy `lexical_score` onto those copies so a later trace rebuild would not drop it. The sources event does not need the field on each shipped chunk; the trace already carries the lexical briefs.

- [ ] **Step 3: Run the fuse check, then the hybrid path**

Run: `./.venv/bin/python /tmp/fuse_check.py`
Expected: `fuse ok`

Save as `/tmp/hybrid_pool_check.py` and run it from `src/`:

```python
import retrieve
from retrieve import RetrievedChunk, retrieve_explained

def chunk(i, source, vec, lex=None):
    row = RetrievedChunk(f"id{i}", source, "t", vec, heading="H")
    row.lexical_score = lex
    return row

calls = {"lexical": 0}

def fake_vector(query, top_k=20, scope=None):
    return [chunk(1, "a.md", 0.9), chunk(2, "b.md", 0.2)]

def fake_lexical(query, top_k=20, scope=None):
    calls["lexical"] += 1
    return [chunk(3, "c.md", 0.05, 4.0)]

def fake_rerank(query, candidates):
    for row in candidates:
        row.rerank_score = 1.0
    return candidates

retrieve.vector_search = fake_vector
retrieve.lexical_search = fake_lexical
retrieve.rerank = fake_rerank

on, trace = retrieve_explained("E3", hybrid=True, trust_gate=True, top_k=3)
assert "c.md" in [row.source_file for row in on]
assert trace["lexical"][0]["source_file"] == "c.md"
assert calls["lexical"] == 1

off, off_trace = retrieve_explained("E3", hybrid=False, use_reranker=False)
assert [row.source_file for row in off] == ["a.md", "b.md"]
assert calls["lexical"] == 1
assert off_trace["lexical"] == []
print("pool ok")
```

Expected: `pool ok`

- [ ] **Step 4: Commit**

```bash
git add src/retrieve.py
git commit -m "feat: fuse BM25 hits into the rerank pool"
```

### Task 3: Fourth eval column

**Files:**
- Modify: `src/eval.py`
- Modify: `src/retrieve.py` (`__main__` printout only)

**Interfaces:**
- Consumes: `retrieve(..., hybrid=True, trust_gate=True)`
- The three existing fetchers stay on the default `hybrid=False`.

- [ ] **Step 1: Add the column**

```python
def _hybrid(query: str, top_k: int):
    return retrieve(
        query,
        top_k=top_k,
        candidate_pool=20,
        use_reranker=True,
        trust_gate=True,
        hybrid=True,
    )
```

Add `evaluate(_hybrid, "hybrid")` after the confident column. Widen the header so four labels fit: `vector only`, `rerank order`, `confident`, `hybrid`. Print hybrid misses the same way as the others.

In `retrieve.py` `__main__`, after the shipped block, print a `hybrid` block from `retrieve(query, top_k=4, hybrid=True)` and its `rank_source`. Do not change the shipped block, which stays `hybrid=False` until Task 4.

- [ ] **Step 2: Run eval**

Run: `./.venv/bin/python eval.py`
Expected on the current 20-question set:

| k | vector only | rerank order | confident | hybrid |
|---|---|---|---|---|
| 1 | 95% | 95% | 100% | 100% |
| 3 | 100% | 100% | 100% | 100% |
| 5 | 100% | 100% | 100% | 100% |

Misses: vector still lists the new-hire enrollment question. Rerank order still lists the laptop-return question. Confident lists none. Hybrid lists none.

If hybrid is below 100% at k=1 or any column is below 100% at k=3, stop this plan. Commit Task 3 only if the three original columns still match this table. Leave Ask on `hybrid=False`. Do not edit `TEST_SET` or the BM25 constants. Report the hybrid misses and wait.

- [ ] **Step 3: Commit**

```bash
git add src/eval.py src/retrieve.py
git commit -m "feat: score hybrid retrieval beside the existing columns"
```

### Task 4: Point Ask at the fused pool

Do this step only when Task 3 matched the table, including hybrid at 100% for k=1.

**Files:**
- Modify: `src/generate.py`

**Interfaces:**
- `answer` and `answer_stream` call `retrieve_explained(..., hybrid=True)`.

- [ ] **Step 1: Flip the flag**

Both call sites pass `hybrid=True`. No other prompt or history change.

- [ ] **Step 2: Confirm the flag**

A one-line check from `src/`:

```python
import generate
seen = {}
generate.retrieve_explained = lambda *a, **k: seen.update(hybrid=k.get("hybrid")) or ([], {"best_logit": None, "trusted": False, "rank_source": "vector", "vector": [], "rerank": [], "lexical": []})
generate.answer("q")
assert seen["hybrid"] is True
print("ask hybrid ok")
```

`answer` will also call `load_settings` / `complete` when chunks are empty it returns `NO_INFO` first. The fake returns an empty chunk list, so `answer` returns before `complete`. That is the path to assert.

- [ ] **Step 3: Commit**

```bash
git add src/generate.py
git commit -m "feat: search Ask with the hybrid candidate pool"
```

### Task 5: Docs

**Files:**
- Modify: `AGENTS.md`, `src/README.md`, `src/LEARNING.md`

- [ ] **Step 1: Describe the column**

Add the hybrid column to the recall table with the numbers Task 3 printed. Say Ask’s candidate pool is the RRF union of the vector top 20 and the BM25 top 20, then the same cross-encoder and the same gate. Say the confident column is still the vector pool plus the gate, so the laptop-return miss stays visible in the ungated column. Name `src/lexical.py`, `k1`, `b`, and RRF `k`. Say a tokenless query leaves the pool unchanged.

If Task 4 did not run, write that Ask still uses `hybrid=False` and point at the hybrid misses instead of claiming the feature shipped.

- [ ] **Step 2: Commit**

```bash
git add AGENTS.md src/README.md src/LEARNING.md
git commit -m "docs: record hybrid retrieval and the fourth eval column"
```

Delete `/tmp/bm25_check.py`, `/tmp/fuse_check.py`, and `/tmp/hybrid_pool_check.py` after they pass.

# RAG Demo — Internal Support Assistant

A minimal, framework-free RAG (Retrieval-Augmented Generation) pipeline over
a synthetic set of company documents (HR policies, IT policy, a product
FAQ, and a support runbook). Built to actually understand and be able to
explain every stage of a RAG system, not just call a library.

## Pipeline

```
docs/*.md
   │  chunk.py        — heading-aware chunking with overlap
   ▼
build_index.py         — embed chunks (sentence-transformers), index (FAISS)
   ▼
index/                  — persisted vectors + metadata
   │
   │  query
   ▼
retrieve.py             — vector search (top-20) → cross-encoder rerank (top-4)
   ▼
generate.py             — grounded prompt + citations → Claude
   ▼
cli.py                  — "python cli.py '<question>'"
```

## Setup

```bash
cd rag-demo
python3.12 -m venv .venv        # 3.12, not 3.14 — wheel availability for
                                 # faiss-cpu/sentence-transformers lags on
                                 # brand-new Python versions
./.venv/bin/pip install -r requirements.txt

./.venv/bin/python build_index.py     # run once, and again whenever docs/ changes

export ANTHROPIC_API_KEY=sk-ant-...
./.venv/bin/python cli.py "How many PTO days do I get per year?"
```

## Retrieval accuracy: what's actually implemented

1. **Structure-aware chunking** (`chunk.py`), not blind fixed-size
   splitting. Splitting every N characters regardless of content routinely
   cuts a sentence or a Q&A pair in half, which is one of the most common,
   avoidable causes of bad retrieval. This splits on markdown headings
   first, then applies a sliding window with overlap only within a
   section, and prefixes each chunk with its section heading so the chunk
   is self-describing out of context.

2. **Two-stage retrieve-then-rerank** (`retrieve.py`). Stage 1 is a fast
   bi-encoder vector search (query and document embedded independently,
   compared by dot product) over the whole corpus, pulling the top 20
   candidates. Stage 2 runs a cross-encoder over just those 20 — it looks
   at the query and each candidate *together* in one forward pass, which
   captures interactions a bi-encoder structurally can't, at a cost too
   expensive to run over the full corpus. This retrieve-then-rerank
   pattern is the standard accuracy lever in production RAG.

3. **Grounded generation with forced citations** (`generate.py`). The
   prompt requires every claim to cite a source chunk number and
   explicitly instructs the model to say it doesn't know rather than fall
   back on outside knowledge. This doesn't prevent hallucination outright,
   but it makes it visible — an uncited claim, or a citation that doesn't
   support the sentence next to it, is a reviewable signal.

4. **A measured accuracy number, not a vibe** (`eval.py`). 20 hand-labeled
   (question, correct source doc) pairs, scored as recall@k — did the
   correct document appear in the top-k retrieved chunks. Run it:

   ```bash
   ./.venv/bin/python eval.py
   ```

   Result on this corpus:

   | k | vector search only | with reranker |
   |---|---------------------|----------------|
   | 1 | 95% | 95% |
   | 3 | 100% | 100% |
   | 5 | 100% | 100% |

   **Why the reranker doesn't move the headline number here, and why
   that's an honest result worth understanding:** this corpus is small
   (11 docs, 52 chunks) and topically well-separated, so a bi-encoder
   already resolves most queries correctly — reranking's advantage grows
   with corpus size and topical overlap between documents. More
   interestingly, at k=1 the two methods get *different* queries wrong:

   - "What happens if I don't return my laptop when I leave the company?"
     → vector search gets it right (`remote_work_equipment_policy.md`);
     reranking flips it to `it_security_policy.md`, which mentions
     "company laptops" in a different context (disk encryption) — a
     lexical false-positive.
   - "When do I need to enroll in health insurance as a new hire?" →
     vector search gets it wrong (`onboarding_guide.md`, which mentions
     benefits enrollment only in passing); reranking correctly picks
     `benefits_enrollment_guide.md`.

   Net effect: zero change in the aggregate number, but a real change in
   *which* queries fail. This is exactly why you evaluate retrieval
   changes on a labeled set instead of eyeballing one or two example
   queries — a single anecdote can point either direction.

## What I'd add for a production version (interview talking points)

These are deliberately **not** implemented here — this is a 3-day learning
build, not a shipped system — but they're the right next steps and worth
being able to discuss:

- **Hybrid search**: combine dense vector search with sparse lexical
  search (BM25) and fuse the results (e.g. Reciprocal Rank Fusion). Dense
  retrieval is weak on exact terms — product codes, error codes like
  "E3", proper nouns — that BM25 catches directly.
- **Query rewriting / expansion**: use the LLM to rewrite a vague or
  colloquial user query into one or more retrieval-friendly queries
  before searching (also handles multi-turn conversational context —
  "what about the international rate?" needs the prior turn's topic
  folded in).
- **Metadata filtering**: tag chunks with department, doc type, or
  recency, and let retrieval filter/boost on that — critical once you
  have hundreds of documents and multiple doc versions where an
  outdated policy shouldn't outrank the current one.
- **Chunk size/overlap tuning as an actual experiment**: sweep chunk
  size and overlap against the eval set rather than picking constants by
  feel, the way `CHUNK_SIZE`/`CHUNK_OVERLAP` are set here.
- **A bigger, sourced eval set**: pull real user queries or support
  tickets instead of hand-writing them, and track precision and
  answer-level correctness (e.g. LLM-as-judge, or a framework like
  RAGAS) in addition to retrieval recall.
- **Groundedness / citation verification**: a post-generation check
  (rule-based or a second LLM call) that every cited chunk actually
  supports the sentence it's attached to, catching cases where the model
  cites a source but drifts from what it says.
- **Caching and cost**: cache embeddings and, for repeated/similar
  queries, full answers — semantic caching (cache hit on embedding
  similarity, not exact string match) is common in high-traffic support
  bots.
- **Guardrails**: PII redaction on both ingestion and output, and
  explicit scoping so the assistant can't be steered into answering
  questions outside the document set (prompt injection via a malicious
  document is a real concern once documents come from less-trusted
  sources).

## Files

| File | Role |
|---|---|
| `docs/` | Synthetic source documents (the "company knowledge base") |
| `chunk.py` | Heading-aware chunking with overlap |
| `build_index.py` | Embed chunks, build/persist the FAISS index |
| `retrieve.py` | Vector search + cross-encoder rerank |
| `generate.py` | Prompt assembly + Claude call, with citations |
| `cli.py` | Command-line entrypoint |
| `eval.py` | Retrieval recall@k evaluation harness |

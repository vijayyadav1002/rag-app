# RAG Demo — Internal Support Assistant

A minimal, framework-free RAG (Retrieval-Augmented Generation) pipeline over
a synthetic set of company documents (HR policies, IT policy, a product
FAQ, and a support runbook). Built to actually understand and be able to
explain every stage of a RAG system, not just call a library.

## Pipeline

```
docs/*.md               — Library can write these; Re-index calls build()
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
generate.py             — grounded prompt + citations
   │
llm.py                  — Anthropic or OpenAI-compatible (Ollama, etc.)
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

# Generation: copy .env.example → .env and uncomment ONE provider block
cp .env.example .env
# then edit .env (see “LLM providers” below)

./.venv/bin/python cli.py "How many PTO days do I get per year?"

# Browser UI (same pipeline, streamed over a WebSocket)
./.venv/bin/python server.py
# open http://127.0.0.1:8000
# Installable (Add to Home Screen). The page has a Library panel;
# Re-index rebuilds search from docs/. Upload does not change
# answers until Re-index.
```

## LLM providers

Retrieval (Nomic + FAISS + rerank) always runs on your machine. Only the
final write-the-answer step calls an LLM. `llm.py` supports two HTTP
shapes: Anthropic Messages, and OpenAI Chat Completions (used by Ollama,
LM Studio, vLLM, Groq, OpenRouter, xAI, OpenAI itself).

| `LLM_PROVIDER` | Driver | Default model | Key | Default base URL |
|----------------|--------|---------------|-----|------------------|
| `anthropic` (or unset + `ANTHROPIC_API_KEY`) | Anthropic | `claude-sonnet-4-5` | `ANTHROPIC_API_KEY` or `LLM_API_KEY` | SDK default |
| `openai` | Chat Completions | `gpt-4o-mini` | `OPENAI_API_KEY` or `LLM_API_KEY` | `https://api.openai.com/v1` |
| `ollama` | Chat Completions | `llama3.2` | optional (`ollama` dummy) | `http://127.0.0.1:11434/v1` |
| `xai` | Chat Completions | `grok-4.5` | `XAI_API_KEY` or `LLM_API_KEY` | `https://api.x.ai/v1` |

Copy `.env.example` to `.env` and uncomment one block. `.env` is gitignored;
`llm.py` loads it automatically (exported shell variables still win).

Override model with `LLM_MODEL` and endpoint with `LLM_BASE_URL`. Check
what `llm.py` resolved:

```bash
./.venv/bin/python llm.py
```

A small local model may ignore citation rules more often than Claude.
That is a model limit; the prompt is the same.

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
| `build_index.py` | Embed chunks, atomically persist the FAISS index (`files` / `indexed_at`) |
| `retrieve.py` | Vector search + cross-encoder rerank; `reload()` hot-swaps FAISS |
| `generate.py` | Prompt assembly + citations (`answer_stream` for the UI) |
| `llm.py` | Anthropic or OpenAI-compatible generation (`complete` / `stream`) |
| `cli.py` | Command-line entrypoint |
| `eval.py` | Retrieval recall@k evaluation harness |
| `library.py` | Safe names, list/save/delete markdown in `docs/` (no rebuild) |
| `server.py` | FastAPI WebSocket shell + library REST + Re-index lock (`python-multipart` for uploads) |
| `static/index.html` | Ask UI + Library panel + PWA registration |
| `static/manifest.webmanifest` | Install metadata (Add to Home Screen) |
| `static/sw.js` | Cache the UI shell only (not `/ws` or `/api/*`) |

## Web UI

`server.py` serves `static/index.html` at `/` and a WebSocket at `/ws`. The page sends `{ "question": "..." }` and renders events in order: retrieving → source excerpts → tokens → done. The RAG path is still retrieve-then-rerank-then-generate; the socket is only transport. The page is a PWA: Add to Home Screen installs it; the service worker caches the shell, not answers.

```bash
./.venv/bin/python server.py
```

Then open `http://127.0.0.1:8000`. `cli.py` is unchanged.

## Library and re-index

The Library panel lists `docs/*.md`. Upload is markdown only (same filename overwrites). Delete removes the file from disk. There is no login — anyone who can open the app can change the corpus. Upload and delete do not change answers until you click **Re-index**, which rebuilds FAISS from every `docs/*.md` and hot-reloads search. While it rebuilds, Ask, upload, and delete are locked.

Auth, PDF/Word, chat history, auto-reindex on upload, and offline Q&A are out of scope.

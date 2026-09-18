# Ask the Company

Minimal, framework-free RAG demo over synthetic Northwind Retail Co. documents (HR, IT, product FAQ, support runbook). Goal is to understand every pipeline stage, not wrap a library.

All code lives in `rag-demo/`. Run commands from that directory.

## Commands

| Command | Description |
|---------|-------------|
| `python3.12 -m venv .venv` | Create venv. Use 3.12, not 3.14 — `faiss-cpu` / `sentence-transformers` wheels lag on brand-new Python. |
| `./.venv/bin/pip install -r requirements.txt` | Install deps |
| `./.venv/bin/python build_index.py` | Chunk `docs/`, embed, persist FAISS index. Run once, and again whenever `docs/` changes. |
| `./.venv/bin/python cli.py "your question"` | End-to-end Q&A (needs an LLM provider; see Environment) |
| `./.venv/bin/python server.py` | Browser UI at http://127.0.0.1:8000 (installable PWA, Library panel, Re-index; WebSocket stream; same LLM env as CLI) |
| `./.venv/bin/python eval.py` | Retrieval recall@k on 20 hand-labeled (question, source doc) pairs |
| `./.venv/bin/python chunk.py` | Print chunk count and a sample of chunks |
| `./.venv/bin/python retrieve.py "query"` | Vector search vs rerank, no LLM |

There is no lint, format, test-runner, or deploy command.

## Tech stack

- Python 3.12, stdlib-style scripts (FastAPI only for the PWA/WebSocket UI; no LangChain/LlamaIndex)
- `sentence-transformers` — `nomic-ai/nomic-embed-text-v1.5` bi-encoder (`search_document:` / `search_query:` prefixes); `cross-encoder/ms-marco-MiniLM-L-6-v2` reranker
- `faiss-cpu` — `IndexFlatIP` on L2-normalized vectors (cosine via inner product)
- `anthropic` / `openai` — generation via `llm.py` (Anthropic Messages or OpenAI-compatible Chat Completions, including Ollama)
- `numpy` — embedding arrays as `float32`
- FastAPI + uvicorn + `python-multipart` — PWA/WebSocket UI and library uploads only

## Architecture

```
docs/*.md
   │  chunk.py          heading-aware chunking + overlap
   ▼
build_index.py          embed → FAISS + pickle metadata
   ▼
index/                  chunks.faiss, metadata.pkl, config.json
   │
retrieve.py             top-20 vector search → cross-encoder rerank → top-4
   ▼
generate.py             grounded prompt + citations
   ▼
llm.py                  Anthropic or OpenAI-compatible LLM
   ▼
cli.py                  argv question → printed answer + sources
eval.py                 recall@k, vector-only vs reranked
server.py               PWA library writes docs/; Re-index → build() + retrieve.reload()
```

Pipeline data flow: markdown docs → `Chunk` dataclasses → normalized embeddings → FAISS → `RetrievedChunk` → numbered excerpts in the LLM prompt.

## Key files

- `rag-demo/chunk.py` — `CHUNK_SIZE=800`, `CHUNK_OVERLAP=150`; split on `## ` first, then sliding window within a section; prefix each chunk with `{doc_title} > {heading}`
- `rag-demo/build_index.py` — atomic write via `index/.tmp/` then replace; `config.json` includes `files` and `indexed_at`
- `rag-demo/retrieve.py` — lazy-loads embedder, reranker, index, and pickled chunks into module globals; `reload()` re-reads FAISS without restarting
- `rag-demo/library.py` — safe markdown names; list/save/delete `docs/` (does not rebuild the index)
- `rag-demo/static/manifest.webmanifest` — PWA install metadata
- `rag-demo/static/sw.js` — caches the UI shell (`ask-northwind-v3`); never `/ws` or `/api/*`
- `rag-demo/generate.py` — `SYSTEM_PROMPT` forces citations and “I don’t know”
- `rag-demo/llm.py` — vendor boundary: `complete()` / `stream()`; `LLM_PROVIDER` + `LLM_MODEL` + `LLM_BASE_URL` + `LLM_API_KEY`
- `rag-demo/eval.py` — `TEST_SET` of 20 labeled queries; metric is source-doc recall@k, not answer correctness
- `rag-demo/docs/` — 11 synthetic `.md` files; company name is Northwind Retail Co.

## Environment

- LLM generation (`cli.py` / `generate.py` / `server.py`): `LLM_PROVIDER` = `anthropic` \| `openai` \| `ollama` \| `xai`. Keys: `LLM_API_KEY`, or `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `XAI_API_KEY`. Unset provider + `ANTHROPIC_API_KEY` keeps the old Anthropic default. Copy `rag-demo/.env.example` → `rag-demo/.env` (gitignored; loaded by `llm.py`). Retrieval and eval run fully offline after models are cached.
- First retrieve/eval/index build downloads Hugging Face models; subsequent runs use the local cache.
- `index/` and `.venv/` are gitignored. A committed tree has no index; rebuild locally.

## Coding conventions

- Flat modules, not a package. Imports are `from chunk import …` / `from retrieve import …`. Run from `rag-demo/` (or put it on `PYTHONPATH`).
- Each file is both an importable module and a `__main__` CLI.
- Resolve paths from `Path(__file__).parent`, never from cwd.
- Dataclasses + modern type hints (`list[Chunk]`, `float | None`). No Pydantic.
- Module constants for tunables (`CHUNK_SIZE`, `EMBEDDING_MODEL`, `MODEL`).
- Module-level docstring explains *why* (what the naive alternative gets wrong), not just what the file does.
- Keep it framework-free. Do not add LangChain, an API server, or extra deps unless the task needs them.
- Synthetic docs: `# Title` then `##` sections. Chunking only splits on `## ` headings.

## Testing

- No unit tests. Accuracy is `eval.py` recall@k (did the expected `source_file` appear in the top-k chunks).
- On this corpus (~52 chunks, 11 topically separated docs) vector search and rerank both report ~95% @1 and 100% @3/@5, but they fail *different* queries. Treat eval-set changes as the source of truth, not a single example query.
- `eval.py` does not call the LLM.

## Gotchas

- Rebuild the index after any `docs/` edit (CLI `build_index.py` or the UI **Re-index**); retrieval reads whatever is on disk. A running server does not pick up a CLI rebuild until Re-index or restart.
- Do not treat rerank as strictly better here. README documents a laptop-return query that vector search gets right and the reranker flips to `it_security_policy.md`.
- Generation must not use outside knowledge; if chunks are empty or insufficient, say so.
- Production follow-ups (hybrid BM25, query rewrite, metadata filters, RAGAS, citation verification) are intentionally unimplemented — see README. Don’t silently “upgrade” the demo into that unless asked.

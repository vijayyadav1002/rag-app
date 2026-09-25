# Rag-app

Minimal, framework-light RAG demo over synthetic Northwind Retail Co. documents (HR, IT, product FAQ, support runbook). Goal is to understand every pipeline stage, not wrap a library.

All code lives in `src/`. Run commands from that directory.

## Commands

| Command | Description |
|---------|-------------|
| `python3.12 -m venv .venv` | Create venv. Use 3.12, not 3.14 — `faiss-cpu` / `sentence-transformers` wheels lag on brand-new Python. |
| `./.venv/bin/pip install -r requirements.txt` | Install deps (`python-multipart` is required for library uploads) |
| `./.venv/bin/python build_index.py` | Chunk `DOCS_DIR` (default `docs/`), embed, persist FAISS index. Run once, and again whenever the corpus changes. |
| `./.venv/bin/python cli.py "your question"` | End-to-end Q&A (needs an LLM provider; see Environment) |
| `./.venv/bin/python server.py` | Installable PWA at http://127.0.0.1:8000: Ask at `/` (WebSocket), Library at `/library` (upload/delete under `DOCS_DIR`), Re-index on the library page (`build()` + `retrieve.reload()`). Same LLM env as CLI. |
| `./.venv/bin/python eval.py` | Retrieval recall@k: vector, ungated rerank, and the shipped confidence gate |
| `./.venv/bin/python chunk.py` | Print chunk count and a sample of chunks |
| `./.venv/bin/python retrieve.py "query"` | Vector search, ungated rerank, and shipped order; no LLM |

There is no lint, format, test-runner, or deploy command. Do not add pytest.

## Tech stack

- Python 3.12, stdlib-style scripts (FastAPI only for the PWA/WebSocket UI; no LangChain/LlamaIndex)
- `sentence-transformers` — `nomic-ai/nomic-embed-text-v1.5` bi-encoder (`search_document:` / `search_query:` prefixes); `cross-encoder/ms-marco-MiniLM-L-6-v2` reranker
- `faiss-cpu` — `IndexFlatIP` on L2-normalized vectors (cosine via inner product)
- `anthropic` / `openai` — generation via `llm.py` (Anthropic Messages or OpenAI-compatible Chat Completions, including Ollama and xAI)
- `numpy` — embedding arrays as `float32`
- FastAPI + uvicorn + `python-multipart` — PWA/WebSocket UI and library uploads only

## Architecture

```
DOCS_DIR (default docs/*.md, nested *.md included)
   │  chunk.py          heading-aware chunking + overlap
   ▼
build_index.py          embed → FAISS + pickle metadata (atomic index/.tmp)
   ▼
index/                  chunks.faiss, metadata.pkl, config.json
   │
retrieve.py             top-20 vector search → cross-encoder rerank;
                        keep that order only when the best logit is ≥ 0, else vector order → top-4
   ▼
generate.py             follow-up → standalone search question; grounded prompt
                        includes the recent turns; first question is searched as typed
   ▼
llm.py                  Anthropic or OpenAI-compatible LLM
   ▼
cli.py                  argv question → printed answer + sources
eval.py                 recall@k: vector, ungated rerank, confident rerank
server.py               PWA review queue writes DOCS_DIR on Approve; Re-index → build() + retrieve.reload()
```

Pipeline data flow: markdown docs → `Chunk` dataclasses → normalized embeddings → FAISS → `RetrievedChunk` → numbered excerpts in the LLM prompt.

The RAG brain is `chunk.py`, `build_index.build()`, `retrieve.py`, `generate.py`, `llm.py`, and `cli.py`. `server.py` transports, serves static files, locks, and calls `build()` / `reload()`. It does not retrieve or prompt itself.

## Library and Re-index

Two-stage ingest: **Approve** writes `DOCS_DIR` (default `docs/`). Search changes only after **Re-index** (`POST /api/reindex` → `build()` then `retrieve.reload()`). Upload, edit, and delete stage a proposal in `src/review/` (gitignored, outside the corpus) until Approve. Markdown only (`*.md`); no PDF, Word, or `.txt`. No auth. The Approve button is the human gate.

- Upload filenames are basenames; reject `..`, `/`, `\`, NUL, non-`.md`, non-UTF-8. List/delete/review use posix-relative paths (`hr/pto.md`) so nested files already on disk can be shown and removed; still reject `..`, absolute paths, and escapes outside `DOCS_DIR` or `src/review/`.
- Max **1 MB** per file (`MAX_UPLOAD_BYTES = 1_000_000`), max **20 files** per request (`MAX_UPLOAD_FILES = 20`). Multipart field name is `files`. One open proposal per path; a later submit replaces it.
- Upload runs `format_md.format_markdown` (`FORMAT_MAX_TOKENS = 4096`). A failure stores the raw text as a hand edit. **Save draft** is the override and does not call the model.
- While rebuilding: Ask, upload, delete, and review mutations are locked (HTTP 409; WebSocket `{ "type": "error", "message": "Index is rebuilding. Try again when it finishes." }`). GET `/api/docs`, GET `/api/review`, and `/api/status` stay allowed.
- Relevant prompt text: after retrieval, a hit expands when `rerank_score >= RERANK_FLOOR` (`0.0`) and `>= best - RERANK_MARGIN` (`1.0`). The prompt then gets `section_text` (heading plus up to `SECTION_PROMPT_MAX = 4000` body characters). Embedding and rerank still use the 800-character window. The best logit is the max score on the returned chunks, so a vector-ordered result still expands off the rerank scores. `eval.py` calls `retrieve()` directly and does not expand.
- `retrieve()` keeps rerank order only when the best logit is `>= RERANK_TRUST` (`0.0`). Below that it restores vector order and sets `rank_source` to `vector`. Scores stay attached. Neighbors are not dropped.
- A live file already in `config.json` `files` whose mtime is after `indexed_at` is `changed`. `GET /api/status` sets `stale` when any row is not `indexed`. Ask shows that note. Search still updates only on Re-index.
- Empty corpus → `IndexBuildError("No chunks to index.")` → HTTP 400; live `index/` untouched.
- `build()` succeeds and `reload()` fails → HTTP 500 `"Index rebuilt on disk but failed to load. Restart the server."`

Library is `GET /library` (`library.html`), not a panel on Ask. The only extra HTML route is `/library`. Shell layout is `docs/superpowers/specs/2026-09-18-library-page-design.md` (it supersedes the same-page UI in `2026-09-18-pwa-library-reindex-design.md`; do not rewrite that file). Ask keeps one thread in the browser tab (`sessionStorage` key `ask-northwind-chat`). New chat clears it. The server does not store transcripts. Do not add auto-reindex on upload, a server-side chat log, a list of past chats, login, incremental FAISS, filesystem watchers, or further HTML routes.

## Key files

- `src/chunk.py` — `CHUNK_SIZE=800`, `CHUNK_OVERLAP=150`, `SECTION_PROMPT_MAX=4000`; split on `## ` first, then sliding window within a section; prefix each chunk with `{doc_title} > {heading}`; `section_text` holds the prompt expansion; `resolve_docs_dir()` / recursive `*.md`
- `src/build_index.py` — atomic write via `index/.tmp/` then replace; `config.json` includes `files` and `indexed_at`; `build()` returns that config; reads `DOCS_DIR`
- `src/retrieve.py` — lazy-loads embedder, reranker, index, and pickled chunks into module globals; `reload()` re-reads FAISS without restarting; `_state_lock` around `_index`/`_chunks`; `RERANK_TRUST = 0.0` gates whether rerank order ships; `prompt_excerpts()` applies `RERANK_FLOOR` / `RERANK_MARGIN` against the best logit
- `src/library.py` — safe markdown names; list/read/atomic write/delete under `DOCS_DIR` (does not rebuild the index); list state `changed` when mtime is after `indexed_at`
- `src/review.py` — proposal JSON under `src/review/`; stage, save draft, approve, reject
- `src/format_md.py` — LLM rewrite to one `#` title and `##` sections; rejects a bad result
- `src/server.py` — FastAPI: `GET /`, `GET /library`, `GET /app.css`, `WS /ws`, `GET/POST/DELETE /api/docs`, `GET/PUT/POST/DELETE /api/review`, `POST /api/reindex`, `GET /api/status`, PWA static routes
- `src/static/index.html` — Ask UI + top nav + service worker register (no library panel). One thread of turns; each keeps its excerpts. A follow-up sends the committed questions and answers. New chat clears the tab session. Source meta says `section` or `excerpt`, plus whether the reranker ordered the list. A stale index shows a note linking to Library.
- `src/static/library.html` — Library UI: review queue, editor, file list, upload, delete, Re-index
- `src/static/app.css` — shared theme, header, nav
- `src/static/manifest.webmanifest` — PWA install metadata (name “Ask Northwind”, standalone, `start_url` `/`)
- `src/static/sw.js` — caches the UI shell (`ask-northwind-v9`); precaches `/`, `/library`, `/app.css`; never `/ws` or `/api/*`. Bump the cache name when the shell changes.
- `src/generate.py` — `SYSTEM_PROMPT` forces citations and “I don’t know”. A follow-up is rewritten into one standalone search question (`REWRITE_MAX_TOKENS = 80`); the answer prompt then includes at most 8 messages and 6000 characters of earlier turns. Empty history skips the rewrite.
- `src/llm.py` — vendor boundary: `complete()` / `stream()`; `LLM_PROVIDER` + `LLM_MODEL` + `LLM_BASE_URL` + `LLM_API_KEY`
- `src/eval.py` — `TEST_SET` of 20 labeled queries; metric is source-doc recall@k for vector, ungated rerank, and confident rerank; not answer correctness
- `src/docs/` — 11 synthetic `.md` files; company name is Northwind Retail Co.

## Environment

- LLM generation (`cli.py` / `generate.py` / `server.py`): `LLM_PROVIDER` = `anthropic` \| `openai` \| `ollama` \| `xai`. Keys: `LLM_API_KEY`, or `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `XAI_API_KEY`. Unset provider + `ANTHROPIC_API_KEY` keeps the old Anthropic default. Copy `src/.env.example` → `src/.env` (gitignored; loaded by `llm.py` and `chunk.py`). Retrieval and eval run fully offline after models are cached.
- Corpus path: `DOCS_DIR` (relative to `src/`, `~` expands, absolute allowed). Unset keeps `src/docs`. Restart and Re-index after changing. `index/` is not configurable.
- First retrieve/eval/index build downloads Hugging Face models; subsequent runs use the local cache.
- `index/` and `.venv/` are gitignored. A committed tree has no index; rebuild locally.

## Coding conventions

- Flat modules, not a package. Imports are `from chunk import …` / `from retrieve import …` / `from library import …`. Run from `src/` (or put it on `PYTHONPATH`).
- Each file is both an importable module and a `__main__` CLI.
- Resolve paths from `Path(__file__).parent`, never from cwd.
- Dataclasses + modern type hints (`list[Chunk]`, `float | None`). No Pydantic.
- Module constants for tunables (`CHUNK_SIZE`, `EMBEDDING_MODEL`, `MODEL`).
- Module-level docstring explains *why* (what the naive alternative gets wrong), not just what the file does.
- Keep it framework-light. FastAPI is the existing PWA/WebSocket shell only. Do not add LangChain, Flask, extra HTML routes beyond `/library`, or extra deps unless the task needs them.
- Synthetic docs: `# Title` then `##` sections. Chunking only splits on `## ` headings.
- Do not commit uploaded probe files or `index/`.

## Testing

- No unit tests and no pytest. Accuracy is `eval.py` recall@k (did the expected `source_file` appear in the top-k chunks).
- Verify library/API changes with `python -c` or FastAPI `TestClient` against `server.py`, plus `eval.py` when retrieval or `build()` changes. Do not add a test runner.
- On this corpus vector search and ungated rerank each report 95% @1 and 100% @3/@5, and they fail *different* queries (laptop return vs benefits enrollment). Shipped `retrieve()` (the confident column) is 100% @1/@3/@5. Treat eval-set changes as the source of truth, not a single example query. Fail a retrieval change if any column’s @3 drops below 100%, or if the confident column’s @1 drops below 100%.
- `eval.py` does not call the LLM.

## Gotchas

- Rebuild the index after any corpus edit (CLI `build_index.py` or the UI **Re-index**); retrieval reads whatever is on disk. A running server does not pick up a CLI rebuild until Re-index or restart.
- Approve writes the live file. Ask can still cite the previous index until Re-index; the Ask note and the Library `changed` badge are the signal, not a rebuild. A staged upload is invisible to search until Approve and then Re-index.
- Do not treat ungated rerank as strictly better. The laptop-return query is right in vector search and wrong in rerank order (`it_security_policy.md`, logit below 0). The gate keeps vector order for that question. `eval.py` must keep printing the ungated column.
- Generation must not use outside knowledge; if chunks are empty or insufficient, say so.
- PWA caches the shell only (`/`, `/library`, `/app.css`, manifest, sw, icons). Ask, upload, and re-index still need the server. Offline Library copy is “You're offline.”; Ask still uses “Not connected. Use Reconnect.”
- Production follow-ups (hybrid BM25, multi-query expansion, metadata filters, RAGAS, citation verification, auth, PDF, auto-reindex, a stored list of past chats) are intentionally unimplemented — see README. Don’t silently “upgrade” the demo into that unless asked. Multi-turn Ask is in scope: one tab session, a standalone search question, and the recent turns in the answer prompt.

# Rag-app

Minimal, framework-light RAG demo over synthetic Northwind documents (HR, IT, product FAQ, support runbook). Goal is to understand every pipeline stage, not wrap a library.

All code lives in `src/`. Run commands from that directory.

## Commands

| Command | Description |
|---------|-------------|
| `python3.12 -m venv .venv` | Create venv. Use 3.12, not 3.14 — `faiss-cpu` / `sentence-transformers` wheels lag on brand-new Python. |
| `./.venv/bin/pip install -r requirements.txt` | Install deps (`python-multipart` is required for library uploads) |
| `./.venv/bin/python build_index.py` | Chunk `DOCS_DIR` (default `docs/`), embed, persist FAISS index. Run once, and again whenever the corpus changes. |
| `./.venv/bin/python cli.py "your question"` | End-to-end Q&A (needs an LLM provider; see Environment) |
| `./.venv/bin/python server.py` | Installable PWA. Requires `LOCK_PASSWORD` or it exits before bind. Default http://127.0.0.1:8000 (`HOST` / `PORT` in `.env`). Ask at `/` (WebSocket), Library at `/library` (upload/delete under `DOCS_DIR`), Re-index on the library page (`build()` + `retrieve.reload()`). Same LLM env as CLI. |
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

The RAG brain is `chunk.py`, `build_index.build()`, `retrieve.py`, `generate.py`, `llm.py`, and `cli.py`. `server.py` transports, serves static files, holds the rebuild lock, and calls `build()` / `reload()`. It does not retrieve or prompt itself.

## Library and Re-index

Two-stage ingest: **Approve** writes `DOCS_DIR` (default `docs/`). Search changes only after **Re-index** (`POST /api/reindex` → `build()` then `retrieve.reload()`). Upload and delete stage a proposal in `src/review/` immediately (gitignored, outside the corpus). Edit opens the live file and writes that proposal on Save draft, Format, or Approve. Format saves the textarea first, then calls the model. Markdown only (`*.md`); no PDF, Word, or `.txt`. The website lock is one shared password in front of Ask and Library. The Approve button remains the human gate.

- Upload filenames are basenames; reject `..`, `/`, `\`, NUL, non-`.md`, non-UTF-8. List/delete/review use posix-relative paths (`hr/pto.md`) so nested files already on disk can be shown and removed; still reject `..`, absolute paths, and escapes outside `DOCS_DIR` or `src/review/`.
- Max **1 MB** per file (`MAX_UPLOAD_BYTES = 1_000_000`), max **20 files** per request (`MAX_UPLOAD_FILES = 20`). Multipart field name is `files`. One open proposal per path; a later submit replaces it.
- Upload runs `format_md.format_markdown` (`FORMAT_MAX_TOKENS = 4096`). A failure stores the raw text as a hand edit. **Save draft** is the override and does not call the model.
- While rebuilding: Ask, upload, delete, and review mutations are locked (HTTP 409; WebSocket `{ "type": "error", "message": "Index is rebuilding. Try again when it finishes." }`). GET `/api/docs`, GET `/api/review`, and `/api/status` stay allowed.
- Relevant prompt text: after the top 4 are chosen, `prompt_excerpts()` expands a hit when `rerank_score >= RERANK_FLOOR` (`0.0`) and `>= max(returned scores) - RERANK_MARGIN` (`1.0`). The prompt then gets `section_text` (heading plus up to `SECTION_PROMPT_MAX = 4000` body characters). The margin uses the best score in the returned list, not whichever chunk is first. The trust gate restores vector order only when that best score is below 0, so a gated result expands nothing. Chunks with no rerank score stay windows. Two windows of the same file and heading collapse to one excerpt when they expand. Embedding and rerank still use the 800-character window. `eval.py` calls `retrieve()` directly and does not expand.
- `retrieve()` keeps rerank order only when the best logit is `>= RERANK_TRUST` (`0.0`). Below that it restores vector order and sets `rank_source` to `vector`. Scores stay attached. Neighbors are not dropped.
- A live file already in `config.json` `files` whose mtime is after `indexed_at` is `changed`. `GET /api/status` sets `stale` when any row is not `indexed`. Ask shows that note. Search still updates only on Re-index.
- Empty corpus → `IndexBuildError("No chunks to index.")` → HTTP 400; live `index/` untouched.
- `build()` succeeds and `reload()` fails → HTTP 500 `"Index rebuilt on disk but failed to load. Restart the server."`

Library is `GET /library` (`library.html`), not a panel on Ask. The only extra HTML route is `/library`. Shell layout is `docs/superpowers/specs/2026-09-18-library-page-design.md` (it supersedes the same-page UI in `2026-09-18-pwa-library-reindex-design.md`; do not rewrite that file). Ask keeps conversations in this browser (`localStorage` key `ask-northwind-threads`, at most 40). The sidebar shows the current thread and recent ones. New chat starts another thread and leaves the previous one in that list. A one-time import reads the old `sessionStorage` key `ask-northwind-chat`. The server does not store transcripts. Do not add auto-reindex on upload, a server-side chat log, per-user accounts, OAuth, incremental FAISS, filesystem watchers, or further HTML routes. The master lock is the website auth. Retrieved context starts collapsed. The file name opens a read-only view (`GET /api/docs/{path}`). Library folder pills are the real top-level folders under `DOCS_DIR`.

## Key files

- `src/chunk.py` — `CHUNK_SIZE=800`, `CHUNK_OVERLAP=150`, `SECTION_PROMPT_MAX=4000`; split on `## ` first, then sliding window within a section; prefix each chunk with `{doc_title} > {heading}`; `section_text` holds the prompt expansion; `resolve_docs_dir()` / recursive `*.md`
- `src/build_index.py` — atomic write via `index/.tmp/` then replace; `config.json` includes `files` and `indexed_at`; `build()` returns that config; reads `DOCS_DIR`
- `src/retrieve.py` — lazy-loads embedder, reranker, index, and pickled chunks into module globals; `reload()` re-reads FAISS without restarting; `_state_lock` around `_index`/`_chunks`; `RERANK_TRUST = 0.0` gates whether rerank order ships; `prompt_excerpts()` applies `RERANK_FLOOR` / `RERANK_MARGIN` against the max returned logit and collapses same-heading windows when they expand
- `src/library.py` — safe markdown names; list/read/atomic write/delete under `DOCS_DIR` (does not rebuild the index); list state `changed` when mtime is after `indexed_at`; `chunk_count` from `metadata.pkl` when that file can be read
- `src/review.py` — proposal JSON under `src/review/`; stage, save draft, approve, reject
- `src/format_md.py` — LLM rewrite to one `#` title and `##` sections; rejects a bad result
- `src/auth.py` — website password, session file (`src/sessions.json`), and login throttle. CLI scripts do not import it.
- `src/server.py` — FastAPI: `GET /`, `GET /library`, `GET /app.css`, `WS /ws`, `GET/POST/DELETE /api/docs`, `GET/PUT/POST/DELETE /api/review`, `POST /api/reindex`, `GET /api/status`, PWA static routes. HTTP middleware checks the session cookie. Session routes are `POST /api/session`, `GET /api/session`, and `POST /api/session/logout`. The WebSocket reads that cookie. `drop_sockets` closes that token’s open sockets. `_listen_address()` reads `HOST` and `PORT` (default `127.0.0.1:8000`)
- `src/static/index.html` — Ask UI + top nav + service worker register (no library panel). Browser-local threads in the sidebar. Each turn keeps its excerpts, collapsed until expanded. A follow-up sends the committed questions and answers of the active thread. New chat starts a new thread. Source meta says `section` or `excerpt`, the heading, and the rerank logit when there is one. A stale index shows a note linking to Library. Dark/light choice is `localStorage` `northwind-theme`.
- `src/static/library.html` — Library UI: review queue, editor, file list, folder pills, preview, upload, delete, Re-index
- `src/static/app.css` — shared dark/light theme, header, nav
- `src/static/manifest.webmanifest` — PWA install metadata (name “Ask Northwind”, standalone, `start_url` `/`)
- `src/static/sw.js` — caches the UI shell (`ask-northwind-v13`); precaches `/`, `/library`, `/app.css`; never `/ws` or `/api/*`. Bump the cache name when the shell changes.
- `src/generate.py` — `SYSTEM_PROMPT` forces citations and “I don’t know”. Empty history skips the rewrite and searches the typed question. A follow-up calls `complete()` (`REWRITE_MAX_TOKENS = 80`) for one standalone question. `clean_rewrite` keeps the first line, strips one pair of wrapping quotes, and rejects empty text or anything over `RETRIEVAL_QUERY_MAX` (400). Failure falls back to the previous user question plus the new one, capped at 400 characters, with the new question kept intact. `normalize_history` keeps completed pairs only: at most 8 messages, 1500 characters each, 6000 characters total; a trailing user item is dropped and the oldest pairs go first. Citation numbers apply only to this turn’s excerpts. `cli.py` does not pass history.
- `src/llm.py` — vendor boundary: `complete()` / `stream()`; `LLM_PROVIDER` + `LLM_MODEL` + `LLM_BASE_URL` + `LLM_API_KEY`
- `src/eval.py` — `TEST_SET` of 20 labeled queries; metric is source-doc recall@k for vector, ungated rerank, and confident rerank; not answer correctness
- `src/docs/` — 11 synthetic `.md` files; company name is Northwind

## Environment

- LLM generation (`cli.py` / `generate.py` / `server.py`): `LLM_PROVIDER` = `anthropic` \| `openai` \| `ollama` \| `xai`. Keys: `LLM_API_KEY`, or `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` / `XAI_API_KEY`. Unset provider + `ANTHROPIC_API_KEY` keeps the old Anthropic default. Copy `src/.env.example` → `src/.env` (gitignored; loaded by `llm.py`, `chunk.py`, `server.py`, and `auth.py`; a shell export wins). Retrieval and eval run fully offline after models are cached.
- Corpus path: `DOCS_DIR` (relative to `src/`, `~` expands, absolute allowed). Unset keeps `src/docs`. Restart and Re-index after changing. `index/` is not configurable.
- Web UI bind: `HOST` and `PORT` in `.env`. Unset keeps `127.0.0.1` port `8000`. `HOST=0.0.0.0` accepts other machines on the same network; they open `http://<lan-ip>:<PORT>`. `PORT` must be an integer from 1 through 65535. Restart `server.py` after changing. The port requires the master password. The cookie idle window is 10 days (`864000` seconds) and slides on use. Logout kills that one token and its open sockets. Idle expiry closes that token’s sockets too. A new login does not revoke older tokens. CLI stays unlocked. Restart after changing `LOCK_PASSWORD`. Existing rows die because the stamp no longer matches. Their sockets die because the old process exits, not because the new process sends `1008`.
- First retrieve/eval/index build downloads Hugging Face models; subsequent runs use the local cache.
- `index/`, `.venv/`, and `src/sessions.json` are gitignored. A committed tree has no index; rebuild locally. Do not commit the password.

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
- Production follow-ups (hybrid BM25, multi-query expansion, metadata filters, RAGAS, citation verification, per-user accounts, PDF, auto-reindex, a server-side chat log) are intentionally unimplemented — see README. Don’t silently “upgrade” the demo into that unless asked. Multi-turn Ask is in scope: browser-local threads, a standalone search question, and the recent turns of the active thread in the answer prompt.
- `src/sessions.json` is gitignored. Do not commit the password. A copied cookie dies at logout of that token, not at the next login.

# RAG Demo — Internal Support Assistant

A minimal, framework-free RAG (Retrieval-Augmented Generation) pipeline over
a synthetic set of company documents (HR policies, IT policy, a product
FAQ, and a support runbook). Built to actually understand and be able to
explain every stage of a RAG system, not just call a library.

## Pipeline

```
DOCS_DIR (default docs/*.md) — Library can write these; Re-index calls build()
   │  chunk.py        — heading-aware chunking with overlap
   ▼
build_index.py         — embed chunks (sentence-transformers), index (FAISS)
   ▼
index/                  — persisted vectors + metadata
   │
   │  query
   ▼
retrieve.py             — vector search (top-20) → cross-encoder rerank;
                          keep that order only when the best logit is ≥ 0
   ▼
generate.py             — a follow-up becomes one standalone search question;
                          a strong hit may include its ## section;
                          grounded prompt + citations (recent turns included)
   │
llm.py                  — Anthropic or OpenAI-compatible (Ollama, etc.)
   ▼
cli.py                  — "python cli.py '<question>'"
```

## Setup

```bash
cd src
python3.12 -m venv .venv        # 3.12, not 3.14 — wheel availability for
                                 # faiss-cpu/sentence-transformers lags on
                                 # brand-new Python versions
./.venv/bin/pip install -r requirements.txt

./.venv/bin/python build_index.py     # run once, and again whenever the corpus changes

# Generation: copy .env.example → .env and uncomment ONE provider block
cp .env.example .env
# then edit .env (see “LLM providers” below)

./.venv/bin/python cli.py "How many PTO days do I get per year?"

# Browser UI (same pipeline, streamed over a WebSocket)
./.venv/bin/python server.py
# open http://127.0.0.1:8000          Ask (this computer only)
# open http://127.0.0.1:8000/library  Library (review queue, upload/delete/Re-index)
# another machine on the same network: set HOST=0.0.0.0 in .env (see “Web UI”)
# Installable (Add to Home Screen). Re-index lives on the Library
# page and rebuilds search from DOCS_DIR (default docs/). Upload stages
# a proposal; Approve writes the file; Re-index updates answers.
```

## LLM providers

Retrieval (Nomic + FAISS + rerank) always runs on your machine. Only the
final write-the-answer step calls an LLM. `llm.py` supports two HTTP
shapes: Anthropic Messages, and OpenAI Chat Completions (used by Ollama,
LM Studio, vLLM, Groq, OpenRouter, xAI, OpenAI itself).

| `LLM_PROVIDER` | Driver | Default model | Key | Default base URL |
|----------------|--------|---------------|-----|------------------|
| `anthropic` (or unset + `ANTHROPIC_API_KEY`) | Anthropic | `claude-haiku-4-5` | `ANTHROPIC_API_KEY` or `LLM_API_KEY` | SDK default |
| `openai` | Chat Completions | `gpt-4o-mini` | `OPENAI_API_KEY` or `LLM_API_KEY` | `https://api.openai.com/v1` |
| `ollama` | Chat Completions | `llama3.2` | optional (`ollama` dummy) | `http://127.0.0.1:11434/v1` |
| `xai` | Chat Completions | `grok-4.5` | `XAI_API_KEY` or `LLM_API_KEY` | `https://api.x.ai/v1` |

Copy `.env.example` to `.env` and uncomment one provider block. `.env` is
gitignored. `llm.py`, `chunk.py`, `server.py`, and `auth.py` load it
(exported shell variables still win). `server.py` requires `LOCK_PASSWORD`.
CLI scripts do not read `LOCK_PASSWORD`.

Optional `DOCS_DIR` points the corpus (chunking, Library upload/delete, Re-index)
at another markdown folder. Relative paths are from `src/`. Unset keeps
`docs/`. Nested `*.md` files are indexed; Upload still writes a basename into
that folder's root. Restart and Re-index after changing it. `index/` stays here.

Optional `HOST` and `PORT` are the Web UI bind address. Unset keeps
`127.0.0.1` port `8000`. See “Web UI”.

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

2. **Two-stage retrieve-then-rerank, with a confidence gate**
   (`retrieve.py`). Stage 1 is a fast bi-encoder vector search (query and
   document embedded independently, compared by dot product) over the
   whole corpus, pulling the top 20 candidates. Stage 2 runs a
   cross-encoder over just those 20 — it looks at the query and each
   candidate *together* in one forward pass, which captures interactions
   a bi-encoder structurally can't, at a cost too expensive to run over
   the full corpus. The MiniLM model emits an MS MARCO logit.
   `sigmoid(0) = 0.5`, so a best score below `RERANK_TRUST` (`0.0`) means
   the reranker thinks even its top passage is probably not relevant.
   Shipped retrieval then keeps vector order and still records the
   scores. It does not drop the neighbors. A confident positive score
   still replaces vector order. That is the standard retrieve-then-rerank
   pattern, plus the refusal to let an unconfident reorder win.

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

   | k | vector search only | rerank order (gate off) | shipped (confident rerank) |
   |---|---------------------|--------------------------|----------------------------|
   | 1 | 95% | 95% | 100% |
   | 3 | 100% | 100% | 100% |
   | 5 | 100% | 100% | 100% |

   **Why ungated rerank does not move the headline number, and why the
   gate does:** this corpus is small (11 policy docs, plus whatever else
   is in `DOCS_DIR`) and topically well-separated, so a bi-encoder
   already resolves most queries. At k=1 the two ungated methods get
   *different* queries wrong:

   - "What happens if I don't return my laptop when I leave the company?"
     → vector search gets it right (`remote_work_equipment_policy.md`);
     ungated rerank flips it to `it_security_policy.md`, which mentions
     "company laptops" in a different context (disk encryption). The best
     logit on that question is about -6, so the shipped gate keeps the
     vector order.
   - "When do I need to enroll in health insurance as a new hire?" →
     vector search gets it wrong (`onboarding_guide.md`, which mentions
     benefits enrollment only in passing); rerank correctly picks
     `benefits_enrollment_guide.md` with a positive logit, so the gate
     keeps the rerank order.

   Ungated, the aggregate @1 number does not move. The gate picks the
   list whose model is willing to stand behind its top hit, and on this
   set that is 20/20 at rank 1. `eval.py` still prints the ungated misses
   so a single anecdote cannot hide the disagreement. Fail a retrieval
   change if recall at 3 drops below 100%, or if the shipped column drops
   below 100% at rank 1 on this set.

5. **The prompt can see the section, the index still stores the window**
   (`prompt_excerpts` in `retrieve.py`, called from `generate.py`). After
   the top 4 are chosen, a hit expands when its rerank score is at least
   `0` and within `1.0` of the best score in that list. The model then
   reads `section_text`: the heading plus up to 4,000 characters of the
   `##` body. Embedding and rerank still use the 800-character window, and
   `eval.py` does not expand. The margin is taken from the best score in
   the returned list. The trust gate only restores vector order when that
   best score is below 0, so a gated list does not expand. Two windows of
   the same heading become one excerpt when they do. The 11 shipped files
   are already one chunk per section, so this matters once a longer file
   is in the index.

6. **A follow-up is one search question** (`generate.py`). The first
   question is embedded as typed. A later one is rewritten into a single
   standalone question (`REWRITE_MAX_TOKENS = 80`) before retrieve. The
   answer prompt still includes the recent turns (at most 8 messages and
   6,000 characters). If the rewrite fails, search uses the previous
   question plus the new one, capped at 400 characters. `eval.py` and
   `cli.py` do not send history.

## What I'd add for a production version (interview talking points)

These are deliberately **not** implemented here — this is a 3-day learning
build, not a shipped system — but they're the right next steps and worth
being able to discuss:

- **Hybrid search**: combine dense vector search with sparse lexical
  search (BM25) and fuse the results (e.g. Reciprocal Rank Fusion). Dense
  retrieval is weak on exact terms — product codes, error codes like
  "E3", proper nouns — that BM25 catches directly.
- **Multi-query expansion**: Ask already rewrites one follow-up into a
  single standalone search question. A production system often searches
  several phrasings, or a hypothetical answer, and fuses those lists.
  That extra fan-out is not here.
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
| `docs/` | Default corpus (`DOCS_DIR`); synthetic Northwind markdown |
| `chunk.py` | Heading-aware chunking with overlap |
| `build_index.py` | Embed chunks, atomically persist the FAISS index (`files` / `indexed_at`) |
| `retrieve.py` | Vector search + cross-encoder rerank; keep rerank order only when the best logit is ≥ 0; expand a strong hit to its `##` section after ranking; `reload()` hot-swaps FAISS |
| `generate.py` | Prompt assembly + citations; follow-ups become one standalone search question; recent turns stay in the answer prompt (`answer_stream` for the UI) |
| `llm.py` | Anthropic or OpenAI-compatible generation (`complete` / `stream`) |
| `cli.py` | Command-line entrypoint |
| `eval.py` | Retrieval recall@k: vector, ungated rerank, and the shipped gate |
| `library.py` | Safe names, list/read/atomic write/delete under `DOCS_DIR` (no rebuild) |
| `review.py` | Proposal JSON in `review/` until Approve |
| `format_md.py` | LLM rewrite into `#` / `##` Markdown for the chunker |
| `auth.py` | Website password, session file, and throttle |
| `server.py` | FastAPI WebSocket shell + library REST + review queue + Re-index lock + website lock + `GET /library`; bind address is `HOST` and `PORT` from `.env` |
| `.env.example` | Commented sample. Copy to `.env`: one LLM provider, optional `DOCS_DIR`, optional `HOST` and `PORT`, commented `LOCK_PASSWORD` |
| `static/index.html` | Ask UI + top nav + PWA registration |
| `static/library.html` | Library UI: review queue, editor, folder pills, preview, list, upload, delete, Re-index |
| `static/app.css` | Shared dark/light theme and nav |
| `static/manifest.webmanifest` | Install metadata (Add to Home Screen) |
| `static/sw.js` | Cache the UI shell only (`ask-northwind-v12`; not `/ws` or `/api/*`) |

## Web UI

`server.py` serves Ask at `/` (`static/index.html`) and Library at `/library` (`static/library.html`), plus a WebSocket at `/ws`. Ask keeps conversations in this browser (`localStorage`, key `ask-northwind-threads`). The sidebar lists the current thread and recent ones. A follow-up sends `{ "question", "history" }` (the earlier questions and answers of that thread). Search rewrites a follow-up into one standalone question, then retrieves, reranks, and generates with the conversation in the prompt. The first question is searched as typed. **New chat** starts another thread and leaves the previous one in the list. `{ "cancel": true }` stops an answer that is still streaming. The server does not store the transcript. Retrieved context starts collapsed. Opening a source file shows that markdown read-only. Both pages share a top nav, a dark/light control, and `static/app.css`. The app is a PWA: Add to Home Screen installs it (`start_url` `/`); the service worker caches the shell, not answers.

```bash
./.venv/bin/python server.py
```

Then open `http://127.0.0.1:8000`. `cli.py` still asks one question and does not send history. It prints whether the reranker ordered the sources or the scores were below 0, so vector order stayed.

`server.py` reads `HOST` and `PORT` from `.env` (a shell export wins). The same commented pair is in `.env.example`. Unset, the server listens on `127.0.0.1` port `8000`, so only this computer can open it. To reach Ask or Library from another machine on the same network, set this in `.env` and start the server again:

```bash
HOST=0.0.0.0
PORT=8000
```

`PORT` must be an integer from 1 through 65535. On the other machine, open `http://<this-mac-lan-ip>:8000` (use the port you set). On this Mac, `ipconfig getifaddr en0` prints the Wi-Fi address (`en1` on some Macs). The page shell is still reachable, but Ask, the socket, and Library actions need the master password. One password, no accounts. The cookie lasts 10 days from last use (the idle window slides) and dies on logout. An open socket for that token closes then, and also when the idle window runs out. A new login does not revoke older tokens. Other machines on `HOST=0.0.0.0` use that password. `build_index.py`, `cli.py`, `eval.py`, `chunk.py`, and `retrieve.py` stay unlocked. The first inbound connection may need Python allowed under System Settings → Network → Firewall.

Each turn labels a source `section` or `excerpt`, shows the heading, and shows the rerank logit when there is one. The order line says which ranking shipped. When the library is ahead of the index, a note links to Library. The page keeps the active thread. The model sees at most four earlier exchanges (8 messages, 6,000 characters, 1,500 per message). If the rewrite fails, search uses the previous question plus the new one.

## Library and re-index

Library is a separate page at `/library`. It lists markdown under `DOCS_DIR` (default `docs/`, including subfolders). Category pills are the folder names directly under that corpus. Upload is markdown only (basename, 1 MB, up to 20 files). Preview opens the file read-only. An upload or a delete does not change the live file: it stages a proposal under `review/` (gitignored, not searchable). Edit opens the live Markdown in that queue; it is stored when you save. The same model Ask uses can restack a draft into a `#` title and `##` sections without changing the facts. **Save draft** keeps a hand edit instead. **Format** saves first, then calls the model. **Approve** writes or removes the live file. **Reject** drops the proposal. The lock is in front of the page. Approve is still the human gate that writes the corpus. Unlocking does not write files. Approving does not replace the password. Search still changes only when you click **Re-index**, which rebuilds FAISS from every `*.md` under `DOCS_DIR` and hot-reloads search. While it rebuilds, Ask and the review actions are locked. A strong rerank hit — score at least 0 and within 1.0 of the best score in that list — sends its `##` section (up to 4,000 characters of body) into the prompt. Embedding and rerank still use the 800-character window. A best score below 0 keeps vector order and those short windows. Two windows of the same heading become one excerpt when they expand. The 11 shipped files are already one chunk per section. A file edited after `indexed_at` is badged **changed**, and Ask says the index is behind, until you Re-index. A long file list scrolls inside the list; Upload and Re-index stay on screen.

Per-user accounts and OAuth stay out, along with PDF/Word, a server-side chat log, auto-reindex on upload, and offline Q&A. Conversation threads stay in the browser.

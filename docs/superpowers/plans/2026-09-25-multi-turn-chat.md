# Multi-turn Ask Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let Ask answer a follow-up with the prior questions and answers in the prompt, search that follow-up as a standalone question, and clear the thread with New chat.

**Architecture:** The browser keeps one tab session and sends completed pairs with each question. `generate.py` normalizes that history, rewrites a follow-up before `retrieve()`, and puts the same turns in the answer prompt. `server.py` forwards history and accepts cancel while a turn is streaming. The first question still searches the typed text, so `eval.py` is untouched.

**Tech Stack:** Existing FastAPI WebSocket, `generate.py` / `llm.complete`, vanilla JS. No new dependency.

**Spec:** `docs/superpowers/specs/2026-09-25-multi-turn-chat-design.md`

## Global Constraints

- Run from `src/` with `./.venv/bin/python`. No pytest, no new route, no new dependency.
- Do not write the real `DOCS_DIR`, `src/review/`, or `index/`. Do not change `retrieve()`.
- `HISTORY_MAX_MESSAGES = 8`, `HISTORY_MESSAGE_CHARS = 1500`, `HISTORY_MAX_CHARS = 6000`, `REWRITE_MAX_TOKENS = 80`, `RETRIEVAL_QUERY_MAX = 400`.
- Service worker cache name becomes `ask-northwind-v9`.
- Empty history must not call the rewriter. The single-turn user message stays `Source excerpts:` then `Question:`.
- Library page markup and behavior stay as they are. Scope new composer CSS to `#ask-form`.

---

### Task 1: History and the answer prompt

**Files:** Modify `src/generate.py`

**Interfaces:**
- Produces: `normalize_history(raw: object) -> list[dict[str, str]]`, `build_prompt(question, chunks, history=None) -> str`, constants above.

- [ ] Add the constants, `normalize_history`, and the conversation block in `build_prompt`.
- [ ] Extend `SYSTEM_PROMPT` with the two rules from the spec (earlier citations do not apply; this turn’s excerpts are the only facts).
- [ ] Check with `./.venv/bin/python -c` from `src/`: empty and non-list input return `[]`; a trailing user item is dropped; a leading assistant item is dropped; a broken pair is skipped; 6 pairs become the last 4; a 2000-character message is clipped to 1500 ending in `…`; total over 6000 drops oldest pairs. `build_prompt("Q", [], None)` equals `Source excerpts:\n\n\n\nQuestion: Q`. With one pair, the prompt starts with `Conversation so far:` and still ends with `Question: Q`.

### Task 2: Standalone search question

**Files:** Modify `src/generate.py`

**Interfaces:**
- Consumes: `normalize_history`, `complete`.
- Produces: `clean_rewrite(text: str) -> str | None`, `fallback_query(question: str, history: list[dict[str, str]]) -> str`, `retrieval_query(question: str, history: list[dict[str, str]]) -> str`. `answer(question, top_k=4, history=None)` and `answer_stream(..., history=None)`.

- [ ] `clean_rewrite` returns the first stripped line, removing one matching quote pair. Return `None` when empty or longer than 400.
- [ ] `fallback_query` joins the latest user turn, a space, and the current question. If that exceeds 400, keep the current question whole and a prefix of the prior turn. If the current question alone exceeds 400, clip the current question.
- [ ] `retrieval_query` returns `question` unchanged when history is empty, and does not call `complete`. Otherwise call `complete(REWRITE_PROMPT, rewrite_input(...), max_tokens=80)` and fall back on any exception or a rejected rewrite.
- [ ] `answer` / `answer_stream` normalize once. Follow-ups yield `status/rewriting`, then `status/retrieving` with `query` only when it differs, then the existing sources, tokens, and done events. Search uses the rewritten text. The prompt uses the original question plus history. Cancel returns without a token. No chunks still yields the existing no-information error.
- [ ] Check by monkeypatching `generate.complete` and `generate.retrieve`: empty history calls `retrieve` with the typed question and never `complete`; a follow-up retrieves the rewriter’s line; a rewriter that raises retrieves the fallback string; `build_prompt` on that path contains both the prior answer and the typed follow-up.

### Task 3: Socket

**Files:** Modify `src/server.py`

**Interfaces:**
- Consumes: `answer_stream(question, cancel=..., history=...)`.
- Produces: cancel while a turn is in flight; `{"type":"cancelled"}` after the worker stops; history forwarded as received.

- [ ] Read the next frame while `_pump` runs as a task. `{ "cancel": true }` sets the thread event and does not start a question. A non-dict frame gets the empty-question error. Rebuild and `already answering` stay.
- [ ] One `asyncio.Lock` around `send_json`. `_active_answers` still brackets the pump task. Disconnect sets the cancel event.
- [ ] The worker, if the cancel event is set when the generator finishes or breaks, queues `{"type":"cancelled"}` and does not queue a later token.
- [ ] Check with `TestClient` after patching `server._load` to a no-op and `server.answer_stream` to a fake that records `history` and blocks until `cancel` is set. Send a follow-up with one pair, read the status event, send `{ "cancel": true }`, and expect `cancelled` with no token. A second connection sends `{ "question": "  " }` and expects the empty-question error. Do not call the real models.

### Task 4: Ask UI

**Files:** Modify `src/static/index.html`, `src/static/app.css`, `src/static/sw.js`

**Interfaces:**
- Consumes: events `rewriting`, `retrieving` (optional `query`), `sources`, `token`, `done`, `cancelled`, `error`.
- Produces: `sessionStorage["ask-northwind-chat"]` as `{ "turns": [{ "question", "answer", "ranking", "query", "chunks" }] }`.

- [ ] Replace the Pipeline and Answer panels with `#thread` and a sticky `#ask-form`: `#status`, `#question` textarea (`aria-label="Question"`), Ask, `#new-chat` (“New chat”), `#conn`, `#reconnect`.
- [ ] Enter sends, Shift+Enter does not. Each send appends a turn, closes older `details`, and sends `history` from committed pairs only. Commit on `done` when the markdown buffer is non-empty, and on the exact no-information error. Other errors stay visible and uncommitted.
- [ ] Render excerpts with `textContent`. Show `Follow-up searched as: …` when `query` is set. Move the `#answer` markdown rules onto `.turn .answer`, keeping heading `text-transform: none` and `color: var(--ink)`.
- [ ] New chat removes the storage key and the thread. If an answer is running, send `{ "cancel": true }` and leave Ask disabled until `cancelled` or the socket closes. Restore turns on load; open only the last disclosure. Placeholder switches to `Ask a follow-up…` after the first committed turn.
- [ ] Bump `CACHE` in `sw.js` to `ask-northwind-v9`.
- [ ] Lede: `Internal assistant for Northwind Retail Co. Answers come only from company docs — you will see which excerpts were retrieved before the model writes. A follow-up keeps this conversation. New chat starts a fresh session.`

### Task 5: Docs and checks

**Files:** Modify `AGENTS.md`, `src/README.md`, `src/LEARNING.md`

- [ ] Describe the in-browser thread, the rewrite stage, and New chat. State that server-side logs, a list of past chats, and general multi-query expansion stay out. Point the service worker at `ask-northwind-v9`. Leave older specs as they were.
- [ ] Re-run the Task 1–3 checks. Confirm `GET /` contains `New chat`, `ask-northwind-chat`, and `Follow-up searched as`, and `GET /sw.js` contains `ask-northwind-v9`. Load Ask at desktop and 390×844, send a follow-up, confirm the first question is still on screen and the second turn names a standalone search, then New chat empties the thread. Reload before New chat and confirm the thread returns. Open Library and confirm upload, the file list, and Re-index still render.

---

## Self-review

- Spec coverage: window limits, rewrite, fallback, prompt, cancel, sessionStorage, New chat, citations, CLI/eval unchanged, cache bump, docs. Each has a task.
- No pytest. Checks are `python -c` and `TestClient`, matching the repo.
- Names match across tasks: `normalize_history`, `retrieval_query`, `cancelled`, `ask-northwind-chat`, `ask-northwind-v9`.

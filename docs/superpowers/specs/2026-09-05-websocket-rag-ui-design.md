# WebSocket RAG UI

Single-shot, streamed Q&A over the existing Northwind RAG pipeline, with the retrieve → sources → generate stages visible in a browser.

## Goal

Keep `chunk.py` / `retrieve.py` / `generate.py` as the RAG brain. Add a thin FastAPI WebSocket shell and one HTML page so a learner can watch retrieval, then tokens, then citations — the same contract as `cli.py`, live.

## Architecture

```
docs/*.md → chunk.py → build_index.py → index/     (unchanged)
                                  │
question → retrieve.py → generate.py
               │              │
               │       answer()          cli.py (blocking)
               │       answer_stream()   yields JSON-able event dicts
               │              │
               │       server.py         FastAPI: GET / and WS /ws
               └────── static/index.html
```

`retrieve.py` does not change. `cli.py` keeps calling `answer()`. The server contains no extra RAG logic.

## Events

One WebSocket per page load. Client sends one message per question:

```json
{ "question": "How many PTO days do I get per year?" }
```

Server → client, in order on the happy path:

`status(retrieving)` → `sources` → `status(generating)` → `token*` → `done`

| type | payload |
|------|---------|
| `status` | `{ "stage": "retrieving" \| "generating" }` |
| `sources` | `{ "chunks": [ { "n", "source_file", "preview" } ] }` |
| `token` | `{ "text" }` |
| `done` | `{ "question" }` |
| `error` | `{ "message" }` |

`preview` is ~240 characters, collapsed whitespace. No `token` before `sources`. Empty retrieve: `status(retrieving)` → `sources: []` → `error` with the existing “I don’t have information…” copy. No Claude call.

One in-flight question per socket. Empty/whitespace questions get `error` and do not retrieve. Disconnect cancels the generator.

## UI

One `static/index.html`: title, question input + Ask, pipeline panel (status + numbered chunk previews), answer panel (`textContent` + `pre-wrap`, not Markdown), connection footer with Reconnect. No chat log — a new question replaces the previous turn. Disable input while answering.

## Runtime

- Index missing: process exits before accepting sockets; tell the user to run `build_index.py`.
- `ANTHROPIC_API_KEY` missing/invalid: still retrieve and emit `sources`, then `error`.
- Mid-stream API failure: `error`; partial tokens stay on screen.
- Deps: `fastapi`, `uvicorn[standard]`.
- Run: `python server.py` → `http://127.0.0.1:8000`

## Out of scope

Chat history, LangChain/Chroma, index rebuild from the UI, stop button, scores, auth, auto-reconnect loops, eval-through-socket.

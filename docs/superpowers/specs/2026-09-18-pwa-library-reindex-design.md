# PWA library and re-index

Installable one-page Northwind UI: ask questions as today, manage markdown in `docs/`, and rebuild the FAISS index from a Re-index button. Upload and delete do not search until someone re-indexes.

This supersedes the “index rebuild from the UI” line in `2026-09-05-websocket-rag-ui-design.md`. Chat history, auth, and auto-reindex stay out of scope.

## Goal

Keep `chunk.py` / `build_index.build()` / `retrieve.py` / `generate.py` as the RAG brain. Make the existing FastAPI page a PWA shell, and add an unauthenticated library so anyone who can open the app can add or remove `.md` files and trigger a full rebuild that hot-reloads search without restarting the server.

## Decisions

- Markdown only (`.md`). Same heading-aware chunker.
- Upload/delete write `docs/` only. Search changes only after **Re-index**.
- PWA is an installable shell: cache UI assets, not `/ws` or `/api/*`. No offline answers.
- No login. Anyone with the URL can upload, delete, and re-index (local/LAN demo).
- Library on the same page: list, upload (same name overwrites), delete, Re-index.
- While rebuilding: lock Ask, upload, and delete until rebuild + reload finish.
- Full rebuild of every `docs/*.md`, not incremental FAISS adds.

## Architecture

```
docs/*.md  ──►  chunk.py  ──►  build_index.build()  ──►  index/
                                      ▲
PWA (static/)                         │
  Ask  ──────── WebSocket /ws ──────► generate.answer_stream()
  Library  ──── REST /api/docs* ────► read/write docs/
  Re-index  ─── POST /api/reindex ───► build() then retrieve.reload()
```

`cli.py` still calls `answer()`. `python build_index.py` still calls `build()`. The server still does not retrieve or prompt; it transports, serves files, locks, and calls `build()` / `reload()`.

| Unit | Does | Depends on |
|------|------|------------|
| `docs/` | Source of truth for markdown | nothing |
| `build_index.build()` | Full rebuild of `index/` from `docs/` | `chunk.py` |
| `retrieve.reload()` | Hot-swap FAISS + chunk metadata from `index/` | artifacts `build()` just wrote |
| `server.py` | HTTP + WebSocket + lock + `docs/` I/O | `build()`, `reload()`, `answer_stream()` |
| PWA static files | Ask UI + library + Re-index | `/ws` and `/api/*` |

If the server is running and someone rebuilds from the CLI, the process will not notice until Re-index (or restart). No filesystem watcher.

## Index write and reload

Today `build()` writes `chunks.faiss`, `metadata.pkl`, and `config.json` in place. A crash mid-write could leave a truncated index that `reload()` would load.

`build()` writes those three artifacts into `index/.tmp/`, then replaces the live files only after a successful write. If chunking/embedding fails, live `index/` is untouched.

`config.json` after a successful build:

```json
{
  "embedding_model": "nomic-ai/nomic-embed-text-v1.5",
  "num_chunks": 52,
  "indexed_at": "2026-09-18T15:04:05+00:00",
  "files": ["pto_policy.md", "product_faq.md"]
}
```

`files` is the sorted unique `source_file` list from the chunks just indexed. `indexed_at` is UTC ISO-8601. Older configs without these keys are valid until the next Re-index; the library then treats every on-disk file as **not indexed**.

`retrieve.reload()`:

- Keep the loaded embedder and reranker (same models).
- Read the new FAISS index and pickle into locals first. If that fails, raise and leave the old globals in place.
- Queries (`vector_search` / rerank) and `reload()` share a `threading.Lock`. Under the lock, assign `_index` and `_chunks` together so a worker never reads a mismatched pair.

`_load()` becomes “load if unset, else return.” `reload()` always re-reads disk.

## HTTP API

Unauthenticated. Filenames are basenames only (`pto_policy.md`). Reject `..`, `/`, `\`, NUL, and any name that is not `*.md` (case-insensitive suffix, stored as the uploaded basename).

| Method | Path | Behavior |
|--------|------|----------|
| `GET` | `/api/status` | `{ "rebuilding": bool, "indexed_at": string \| null, "num_chunks": int \| null }` |
| `GET` | `/api/docs` | Library snapshot (below). Allowed during rebuild. |
| `POST` | `/api/docs` | Multipart field `files`: one or more `.md` files. Same name overwrites. Does not rebuild. Returns the same body as `GET /api/docs`. |
| `DELETE` | `/api/docs/{name}` | Delete that file from `docs/`. 404 if missing. Does not rebuild. Returns `GET /api/docs` body. |
| `POST` | `/api/reindex` | If already rebuilding: **409**. Else set `rebuilding`, wait for in-flight answers, `build()`, `reload()`, clear flag. Success: `{ "ok": true, "num_chunks": int, "indexed_at": string, "files": [string] }`. |

`GET /api/docs` body:

```json
{
  "rebuilding": false,
  "indexed_at": "2026-09-18T15:04:05+00:00",
  "num_chunks": 52,
  "docs": [
    { "name": "pto_policy.md", "size": 4120, "mtime": "2026-09-01T12:00:00+00:00", "state": "indexed" }
  ]
}
```

`state`:

- `indexed` — file is in `docs/` and in `config.files`
- `not_indexed` — file is in `docs/` but not in `config.files` (or config has no `files`)
- `missing_on_disk` — name is in `config.files` but the file is gone (shown so the user knows to Re-index)

Upload limits: empty file → 400; not `.md` → 400; larger than **1 MB** → 400; more than **20 files** in one request → 400 for the request. Per-file failures in a mixed batch: save the good files, return **207** with `{ "docs": [...], "errors": [{ "name", "message" }] }`. All-fail → 400 with `errors`. Unsafe names never written.

`python-multipart` is required for FastAPI uploads; add it to `requirements.txt`.

Existing `GET /` and `WS /ws` stay. Mount or add routes so `static/manifest.webmanifest`, `static/sw.js`, and `static/icons/*` are reachable at `/manifest.webmanifest`, `/sw.js`, and `/icons/*` (the service worker caches those URLs). `GET /` still returns `index.html`.

## Lock

One process-wide rebuilding flag, shared by HTTP and WebSocket.

1. `POST /api/reindex` sets `rebuilding` immediately (new Ask/upload/delete fail).
2. Wait until every in-flight `answer_stream` finishes. Do not load a new FAISS under a running generate.
3. Run `build()` then `retrieve.reload()` in a worker thread (embedding is blocking).
4. Clear `rebuilding`.

While `rebuilding`:

- New WebSocket questions: `{ "type": "error", "message": "Index is rebuilding. Try again when it finishes." }` — no retrieve.
- `POST /api/docs` and `DELETE`: **409** `{ "detail": "Index is rebuilding. Try again when it finishes." }`
- `GET /api/docs` and `GET /api/status`: allowed (`rebuilding: true`).
- Second `POST /api/reindex`: **409**.

A page that loads mid-rebuild calls `GET /api/status` and shows the locked UI.

## Errors

| Situation | Behavior |
|-----------|----------|
| Offline (shell cached) | UI: “You’re offline.” Ask still uses “Not connected. Use Reconnect.” |
| Reindex in progress | UI disabled; HTTP 409 / socket error as above |
| Empty `docs/` (no chunks) | `build()` fails, live `index/` unchanged, in-memory retriever unchanged, lock released. HTTP 400: “No chunks to index.” |
| Embed/FAISS failure in `build()` | Same as empty: live artifacts untouched, 500 with a short message |
| `build()` succeeds, `reload()` fails | Disk has the new index; memory may still be old or unset. HTTP 500: “Index rebuilt on disk but failed to load. Restart the server.” UI treats search as unsafe (keep controls unlocked so they can retry Re-index / be told to restart). |
| Delete missing name | 404 |
| Index missing at process start | Unchanged: exit and tell the user to run `build_index.py` |

## PWA

Files:

- `static/manifest.webmanifest` — name “Ask Northwind”, `display: standalone`, `start_url: "/"`, theme/background matching the current page, icons 192 and 512 PNG.
- `static/sw.js` — cache-first for `/`, `/manifest.webmanifest`, `/sw.js`, icons, and the HTML document. Network-only for `/ws` and `/api/*`. Cache name includes a version string; bump it when the shell changes. `skipWaiting` + `clients.claim`.
- `static/index.html` — `rel="manifest"`, `theme-color`, Apple web-app meta tags. Register the service worker on load.
- `static/icons/icon-192.png`, `icon-512.png`

No push, no background sync, no cached answers.

Localhost is a secure origin; install from `http://127.0.0.1:8000` is enough for this demo. HTTPS is not required in-repo.

## UI

One `static/index.html`. Existing Ask form, pipeline panel, answer panel, connection footer stay. No chat log; a new question still replaces the previous turn.

Library panel (above the footer):

- Rows: filename + `state` (`indexed` / `not indexed` / `missing on disk`).
- File picker: `.md`, multiple.
- Delete per row, confirm: “Delete {name} from disk? Re-index afterward to update search.”
- **Re-index** button. While rebuilding: label “Rebuilding…”, disable Ask, upload, delete, and Re-index. Success: refresh list, status “Indexed N chunks.” Failure: error on the library panel, lock released.

On load: `GET /api/status` and `GET /api/docs`. If `rebuilding`, show the locked state.

## Verification

No new test runner.

- `python build_index.py` then `python eval.py` — recall@k unchanged after temp-dir writes and `config.json` extra keys.
- Upload `docs/extra.md` with a unique fact → Ask does not cite it → Re-index → Ask cites `extra.md`.
- Delete `pto_policy.md` → Ask can still cite it → Re-index → Ask does not.
- Double Re-index: second request 409 until the first finishes.
- Re-index during an answer: answer completes, then rebuild starts; new Ask is rejected in between.
- Chrome or Safari on localhost: install, open standalone, Ask still streams. Airplane mode: cached shell, Ask/library/Re-index blocked.

## Out of scope

Auth, PDF/Word, `.txt`, auto-reindex on upload/delete, chat history, incremental FAISS updates, offline Q&A, filesystem watchers, a second Library route, job queues, HTTPS/certs.

# Library page route

Move the PWA library off the Ask screen onto its own URL. Ask stays Q&A. Library is `/library`: file list, upload, delete, Re-index. Both pages share a top nav so you can go either way.

This supersedes the “Library on the same page” / “no second HTML route” decisions in `2026-09-18-pwa-library-reindex-design.md`. Two-stage ingest, the `/api/*` contract, the rebuild lock, and markdown-only uploads stay as that spec defined them. Chat history, auth, and auto-reindex stay out of scope.

## Goal

Stop putting upload and the document list on the main Ask page. Give the PWA two real routes with the same FastAPI static-file style as today: `/` is Ask, `/library` is the library. A long file list scrolls inside the list so Upload and Re-index stay on screen.

## Decisions

- Two HTML pages, real URLs: `GET /` → `index.html`, `GET /library` → `library.html`.
- Shared top nav on both pages: **Ask** (`/`) and **Library** (`/library`). Current page is marked. **Ask** is how you return to the main page. No extra back button.
- Re-index lives on the library page only. Ask has no upload, delete, or Re-index controls.
- Shared stylesheet `static/app.css` for theme and nav. Inline scripts stay in each HTML file. No Jinja, no JS router, no extra deps.
- File list max-height `16rem` with `overflow-y: auto`. Upload and Re-index sit below the list, not inside the scroller.
- PWA `start_url` stays `/`. Service worker precaches both pages and the shared CSS. Still never cache `/ws` or `/api/*`.
- Rebuild lock is still process-wide. Ask disables itself from `GET /api/status` (and from the existing WebSocket rebuild error) even though Re-index is on the other page.

## Architecture

```
docs/*.md  ──►  chunk.py  ──►  build_index.build()  ──►  index/
                                      ▲
PWA                                   │
  GET /         Ask (index.html)  ── /ws ──► generate.answer_stream()
  GET /library  Library           ── /api/docs* ──► docs/ I/O
                Re-index          ── POST /api/reindex ──► build() then retrieve.reload()
```

`cli.py` still calls `answer()`. `python build_index.py` still calls `build()`. `server.py` still does not retrieve or prompt; it transports, serves files, locks, and calls `build()` / `reload()`.

| Unit | Does | Depends on |
|------|------|------------|
| `static/app.css` | Shared theme, header, nav, panels, buttons | nothing |
| `static/index.html` | Ask UI + nav; WebSocket client; rebuild lock from `/api/status` | `/ws`, `GET /api/status`, `app.css` |
| `static/library.html` | Library UI + nav; upload/delete/Re-index | `/api/docs*`, `POST /api/reindex`, `GET /api/status`, `app.css` |
| `server.py` | Existing APIs + `GET /library` + `GET /app.css` | `build()`, `reload()`, `answer_stream()`, `library.py` |
| `static/sw.js` | Cache-first shell including `/library` and `/app.css` | those URLs |

No change to `chunk.py`, `build_index.py`, `retrieve.py`, `generate.py`, `llm.py`, `library.py`, or the `/api/*` handlers beyond serving two new static paths.

## HTTP

Unauthenticated. Existing `/api/status`, `/api/docs`, `/api/reindex`, and `/ws` are unchanged.

| Method | Path | Behavior |
|--------|------|----------|
| `GET` | `/` | `index.html` (Ask). Unchanged path; content no longer includes the library panel. |
| `GET` | `/library` | `library.html`. Exact path; unknown paths stay 404. |
| `GET` | `/app.css` | Shared stylesheet, `text/css`. |

`GET /manifest.webmanifest`, `/sw.js`, and `/icons/{name}` stay as they are.

## UI

Both pages start with the same header inside `main`:

- Left: product name **Ask Northwind** (existing h1 serif styling), linking to `/`. It is not a third nav item.
- Right: `<nav>` with two links, **Ask** → `/` and **Library** → `/library`.
- The current page’s link has class `active` and a brass underline. The other link is muted.
- On a narrow viewport the header wraps; nav stays visible.

No separate “Back to Ask” control. Clicking **Ask** or the product name returns to `/`.

### Ask (`/`)

Keep the lede, question form, pipeline panel, answer panel, and connection footer. No second page title under the header. Remove the `#library` panel and all library DOM/JS (upload, file list, Re-index).

A new question still replaces the previous turn. Register the service worker on load. Open `/ws` as today.

On load: `GET /api/status`. If `rebuilding` is true, disable the question input and Ask button, set pipeline status to “Index is rebuilding. Try again when it finishes.”, and poll `GET /api/status` every 1s until `rebuilding` is false, then unlock. If the page loads idle, do not poll. If a WebSocket question returns the existing rebuild error, lock and start the same poll.

### Library (`/library`)

Same header, then the current library block: status line, file list, **Upload .md**, **Re-index**. No WebSocket, no Ask form, no connection footer.

Rows stay filename + `state` (`indexed` / `not indexed` / `missing on disk`) + Delete. Delete confirm copy is unchanged: “Delete {name} from disk? Re-index afterward to update search.” Hide Delete on `missing_on_disk` rows. Same-name upload still overwrites and does not rebuild. Status copy after save/delete still tells the user to Re-index.

`#doc-list` is `max-height: 16rem; overflow-y: auto`. Upload and Re-index stay **below** that scroller so they remain visible when the list is long. Empty list: “No markdown files in docs/.”

On load: `GET /api/status` and `GET /api/docs`. If `rebuilding`, show the locked library UI and poll as today.

Both pages keep `rel="manifest"`, `theme-color`, Apple web-app meta tags, and `/sw.js` registration.

## Rebuild lock

Server lock is unchanged (process-wide flag, 409 on upload/delete/second reindex, WebSocket error on new questions, GET status/docs allowed).

| Page | While `rebuilding` | After success | After failure |
|------|--------------------|---------------|---------------|
| Library | Upload, delete, Re-index disabled; button label “Rebuilding…”; status “Rebuilding…” | Unlock; refresh list; “Indexed N chunks.” | Unlock; error on the library status line so they can retry |
| Ask | Input and Ask disabled; pipeline status “Index is rebuilding. Try again when it finishes.” | Unlock; status back to Ready (or idle) | Unlock (search may be stale/unsafe). Ask has no library status line |

Navigating from Library to Ask mid-rebuild is a full page load; Ask reads `/api/status` and locks. Two browser tabs: Library keeps its existing poll; Ask only polls on load-when-rebuilding or after a WebSocket rebuild error.

## PWA

- `static/manifest.webmanifest`: `start_url` remains `/`. Name, display, theme, icons unchanged.
- `static/sw.js`: bump cache name from `ask-northwind-v3` to `ask-northwind-v4`. Precache `/`, `/library`, `/app.css`, `/manifest.webmanifest`, `/sw.js`, `/icons/icon-192.png`, `/icons/icon-512.png`. Network-only for `/ws` and `/api/*`. `skipWaiting` + `clients.claim` stay.
- Airplane mode: both cached pages open. Ask: “Not connected. Use Reconnect.” Library: “You're offline.” Upload, delete, Re-index, and questions still need the server.

No push, no background sync, no cached answers.

## Errors

API status codes and messages are unchanged (400/207 uploads, 404 missing delete, 409 rebuild, 400 “No chunks to index.”, 500 embed/FAISS, 500 “Index rebuilt on disk but failed to load. Restart the server.”).

| Situation | Ask (`/`) | Library (`/library`) |
|-----------|-----------|----------------------|
| Offline (shell cached) | “Not connected. Use Reconnect.” | “You're offline.” |
| Rebuild in progress | Form locked; pipeline message above | Controls locked; “Rebuilding…” |
| Re-index HTTP error | Unlocked; no library status | Unlocked; error on the library status line |
| Delete missing name | n/a | 404, message on the library status line |

Unknown paths stay 404. Only `/` and `/library` are HTML pages.

## Verification

No new test runner. Do not add pytest.

- FastAPI `TestClient`: `GET /` is 200, body includes Ask nav and the question form, and does not include `id="library"` or a Re-index button. `GET /library` is 200, body includes the file list, Upload, and Re-index, and does not include the Ask form or `/ws`. `GET /app.css` is 200 `text/css`. Existing `/api/docs` and `/api/reindex` behavior is unchanged.
- Browser: header **Library** goes to `/library`; **Ask** returns to `/`. A long file list scrolls inside `#doc-list`; Upload and Re-index stay visible.
- Start Re-index on Library, navigate to Ask before it finishes: Ask is locked. When `/api/status` reports `rebuilding: false`, Ask unlocks.
- Upload a unique fact → Ask does not cite it → Re-index on Library → Ask cites it. Delete a policy → Ask can still cite it → Re-index → Ask does not.
- `eval.py`: recall @3 stays 100% (this change is UI-only; run it to confirm nothing else drifted).
- Airplane mode: both shells open from cache; `/api/*` and `/ws` are not served from the cache.

## Docs to update during implementation

- `AGENTS.md`: Library is `/library`, not a panel on Ask. Drop “do not add a second HTML route”; the only extra HTML route is `/library`.
- `src/README.md`: describe Ask at `/` and Library at `/library`; Re-index is on the library page.
- This spec replaces the same-page UI section of `2026-09-18-pwa-library-reindex-design.md`. Do not rewrite that file in this work; point readers here for the shell layout.

## Out of scope

Auth, PDF/Word, `.txt`, auto-reindex on upload/delete, chat history, incremental FAISS, offline Q&A, filesystem watchers, extra HTML routes beyond `/library`, a JS router or SPA framework, Jinja/templates, Re-index on the Ask page, always-on status polling while Ask is idle, job queues, HTTPS/certs.

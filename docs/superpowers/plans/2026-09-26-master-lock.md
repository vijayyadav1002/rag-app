# Master lock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Put one shared master password in front of the Ask Northwind website, with a session that dies 10 days after it was last used and dies immediately on logout.

**Architecture:** `src/auth.py` owns the password, the throttle, and gitignored `src/sessions.json`. `server.py` checks the cookie on every useful HTTP call and on the WebSocket, and keeps a process-memory registry of accepted sockets so logout and the idle boundary can close them without waiting for another frame. `/` and `/library` stay the only HTML pages and paint a lock form until `GET /api/session` succeeds. CLI scripts stay unlocked.

**Tech Stack:** Python 3.12, FastAPI, stdlib `hmac` / `secrets` / `hashlib` / `asyncio`. No new dependency. No pytest. Checks use `./.venv/bin/python` and FastAPI `TestClient`.

**Spec:** `docs/superpowers/specs/2026-09-26-master-lock-design.md`

The spec is the behavior. This plan is the order. If a step and the spec disagree, follow the spec and fix the step.

## Global Constraints

- `LOCK_PASSWORD` is required. Missing, `""`, or whitespace-only exits with exactly `LOCK_PASSWORD is missing or empty. Set it in .env (see .env.example). The website does not start without a password.`
- `SESSION_IDLE_SECONDS = 10 * 24 * 60 * 60` (`864000`). Dead when `now - last_access >= SESSION_IDLE_SECONDS`. No second absolute lifetime.
- Cookie name `northwind_session`. `HttpOnly`, `SameSite=lax`, `Path=/`, `Max-Age=864000`. No `Secure`. No `Domain`. Token is `secrets.token_urlsafe(32)`. Never a query string or a WebSocket field.
- Session file `src/sessions.json`, gitignored, key is SHA-256 hex of the raw token. Stamp is `sha256(b"ask-northwind-lock-v1" + password_utf8).hexdigest()`.
- `LOGIN_MAX_FAILURES = 5`, `LOGIN_COOLDOWN_SECONDS = 60`, `MAX_SESSIONS = 100`.
- Logout deletes one token and closes every accepted socket for that token with `1008`. A second browser that unlocked on its own stays in.
- A new login does not revoke older tokens.
- No new HTML route, no new package, no pytest, no change to `retrieve()`, chunking, prompts, or `eval.py`.
- CLI scripts (`cli.py`, `retrieve.py`, `eval.py`, `build_index.py`, `chunk.py`) must not import `auth.py`.
- Do not write `src/.env`, the real `src/sessions.json`, `DOCS_DIR`, `src/review/`, or `index/`. Rebind `auth.SESSIONS_PATH` to a temp file. Set a fake `LOCK_PASSWORD` in `os.environ` before `import server` (`load_dotenv` will not override it).
- Service worker cache name becomes `ask-northwind-v10`.
- Approve stays the human gate for the corpus. The lock does not replace it.
- Run from `src/` with `./.venv/bin/python`.

---

### Task 1: Session store

**Files:**
- Create: `src/auth.py`
- Modify: `.gitignore`

**Interfaces:**
- Produces: `require_lock_password() -> str`, `passwords_match(supplied: str, expected: str) -> bool`, `password_stamp(password: str) -> str`, `reload_password() -> list[str]`, `login(password: str, client_key: str, now: int | None = None, mono: float | None = None) -> LoginResult`, `logout(token: str) -> LogoutResult`, `touch(token: str, now: int | None = None) -> TouchResult`, `classify(token: str, now: int | None = None) -> TouchResult`, `classify_key(key: str, now: int | None = None) -> TouchResult`, `is_public(method: str, path: str) -> bool`, `reset_throttle() -> None`, `token_key(token: str) -> str`.
- `LoginResult`: `token: str | None`, `throttled: bool`, `store_error: bool`, `evicted_keys: list[str]`.
- `TouchResult`: `state: str` (`"ok"` | `"dead"` | `"store_error"`), `last_access: int | None`.
- `LogoutResult`: `state: str` (`"removed"` | `"absent"` | `"store_error"`).
- Constants: `COOKIE_NAME`, `SESSION_IDLE_SECONDS`, `LOGIN_MAX_FAILURES`, `LOGIN_COOLDOWN_SECONDS`, `MAX_SESSIONS`, `STAMP_PREFIX`, `SESSIONS_PATH`.

- [ ] **Step 1: Write `src/auth.py` to the spec section `src/auth.py`.**

Module docstring is the one in the spec (why a boolean cookie and an in-memory map are the wrong store). `load_dotenv(Path(__file__).parent / ".env", override=False)` at import. `require_lock_password` uses `os.environ.get("LOCK_PASSWORD")`, strips, and raises `SystemExit` with the fixed string. It is not called at import.

`reload_password` caches the stripped password and the stamp together. `login` compares against that cached password and writes that cached stamp. If `login` runs before the first `reload_password`, return `store_error` and write nothing. `login` does not read `os.environ` and does not delete older rows except `MAX_SESSIONS` eviction of the oldest `last_access`. It always mints a new token.

`touch` slides `last_access` on `"ok"`. `classify_key` looks up the file key and does not hash again, and a live row is not rewritten. `classify` is `classify_key(token_key(token))`. Equal to `864000` seconds idle is `"dead"` and the row is removed. `863999` is still live.

`logout` of a missing row is `"absent"`. `OSError` on save is `"store_error"` and the previous file stays. Corrupt JSON is treated as no sessions, with one stderr line `sessions.json could not be read; treating it as no sessions.` A failed save prints `session store could not be written.` Neither line includes a path, token, stamp, or password.

Atomic save matches `library.write_doc`: write `sessions.json.tmp`, then `Path.replace`. One `threading.Lock` around load/modify/save and the throttle map.

Throttle: 5 failures, then 60 seconds on `time.monotonic()`. The fifth failure is still a mismatch. The next attempt inside the window is throttled and does not compare. A right password clears that client. `reset_throttle` clears the map.

`is_public` allowlist is the table in the spec: GET `/`, `/library`, `/app.css`, `/manifest.webmanifest`, `/sw.js`, `/api/session`, GET paths under `/icons/` with no `..`, POST `/api/session` and `/api/session/logout`. Everything else is protected, including `GET /docs`.

`__main__` calls `require_lock_password` and prints `LOCK_PASSWORD is set.` It does not print the password or start Uvicorn.

- [ ] **Step 2: Gitignore the session file.**

Add to `.gitignore`:

```
src/sessions.json
src/sessions.json.tmp
```

- [ ] **Step 3: Check the module without importing `server`.**

From `src/`, with `SESSIONS_PATH` rebound to a temp file and `LOCK_PASSWORD` unset, `""`, and `"   "`, each call raises `SystemExit` with the exact string. No `KeyError`. Then set a fake password, call `reload_password`, and check:

- wrong password: `token is None`, not throttled
- right password: token set, file key equals `token_key(token)`, stamp equals `password_stamp`
- `touch` at `last_access + 863999` returns `"ok"` and the stored `last_access` moves
- `touch` at `now - last_access == 864000` returns `"dead"` and the row is gone
- `classify_key(token_key(token))` on a live row is `"ok"` and does not change `last_access`
- `classify` of that same file hash is `"dead"` (it hashes again) while a direct `classify_key` of the hash is `"ok"`
- a second `login` leaves the first token's row in place
- `logout` of the first token is `"removed"`; `touch` of it is `"dead"`; the second token still `touch`es `"ok"`
- monkeypatch `_save` to raise `OSError`: `logout` returns `"store_error"` and the row is still there
- five mismatches then a sixth `login(..., mono=)` inside the window is `throttled`; `reset_throttle` then the right password issues a token
- change `LOCK_PASSWORD`, call `reload_password`, old rows are in the returned hash list and are gone from the file

`server.py` does not import `auth` yet. `cli.py` still imports without `LOCK_PASSWORD`.

### Task 2: HTTP gate and session routes

**Files:**
- Modify: `src/server.py`
- Modify: `src/.env.example`

**Interfaces:**
- Consumes: Task 1 functions and `COOKIE_NAME`, `SESSION_IDLE_SECONDS`.
- Produces: `POST /api/session`, `POST /api/session/logout`, `GET /api/session`, HTTP middleware, `_set_session_cookie`, `_clear_session_cookie`. Startup calls `require_lock_password` in `main()` before `_require_index` and `uvicorn.run`, and again in lifespan before `_require_index` / `_load`, followed by `await asyncio.to_thread(reload_password)`. Lifespan does not call `drop_sockets` and does not send `1008`.

- [ ] **Step 1: Add the cookie helpers and the three routes from the spec.**

Success login and live `GET /api/session` set the cookie and return `{"ok": true}`. The token is not in the JSON. Wrong password is 401 `Wrong password.` Throttle is 401 `Too many attempts. Try again shortly.` Non-object JSON is 400 `Expected JSON body.` and does not count as a guess. Store failure is 500 `Could not store the session.`

Logout `removed` and `absent`: 200 `{"ok": true}`, clear the cookie. `store_error`: 500, cookie left in place. `GET /api/session` `"dead"`: 401 `Unauthorized.` and clear the cookie. `"store_error"`: 500, cookie kept. Login's 401 does not clear a cookie the browser already held.

A login that returns `evicted_keys` will call `drop_sockets` once Task 3 exists. Until that function exists, keep the list on the result and call it from the route as soon as Task 3 lands. Do not drop other browsers' rows.

- [ ] **Step 2: Add the HTTP middleware from the spec.**

Public paths skip `touch`. Otherwise `await asyncio.to_thread(touch, token)`. `"store_error"` is 500 and does not clear the cookie. `"dead"` is 401 `Unauthorized.`, and a present token clears the cookie. `"ok"` refreshes the cookie on the response via `_set_session_cookie`. Do not raise `HTTPException` inside the middleware. Do not read the body.

Auth runs before the rebuild lock. An unauthenticated `POST /api/reindex` is 401 even when `_rebuilding` is true. An authenticated caller still gets `Index is rebuilding. Try again when it finishes.`

`GET /docs`, `/redoc`, `/openapi.json`, and `/docs/oauth2-redirect` stay enabled and are not public.

- [ ] **Step 3: Comment `LOCK_PASSWORD` in `src/.env.example` next to `HOST` / `PORT`.**

```
# --- Website lock (required for python server.py)
# One shared password for Ask and Library. No usernames.
# The server exits if this is missing or empty. A shell export wins.
# CLI scripts do not read it. Do not commit a real password.
# LOCK_PASSWORD=replace-with-a-long-passphrase
```

The line stays commented so a copied file does not boot with a known password.

- [ ] **Step 4: Check with `TestClient`.**

Patch `server._load` if the temp process would otherwise download models; lifespan must still call `reload_password` against the temp session path. Do not start Uvicorn. Do not call `build()`.

- Unset / `""` / `"   "` still raise the exact `SystemExit` from `require_lock_password`. Put the fake password back before `TestClient`.
- Wrong password: 401, no `northwind_session` cookie.
- Right password: 200 `{"ok": true}`. `Set-Cookie` has `HttpOnly`, `SameSite=lax`, `Path=/`, `Max-Age=864000`, and no `Secure` attribute. The JSON does not contain the token.
- No cookie: `GET /api/status`, `GET /api/docs`, `GET /api/session`, `POST /api/reindex` are 401 `Unauthorized.` `GET /` and `GET /library` are 200.
- `_rebuilding = True`: `POST /api/reindex` without a cookie is 401; with the cookie it is 409 and the existing rebuild sentence. Set `_rebuilding = False` in `finally`.
- A row at `now - SESSION_IDLE_SECONDS + 30` slides on `GET /api/session`. A row at exactly `now - SESSION_IDLE_SECONDS` is 401.
- Logout then the same cookie is 401. A second `TestClient` that logged in on its own still gets 200 after the first logs out.
- A second login on client A does not make a saved copy of A's previous token return 401.
- `reload_password` after changing `LOCK_PASSWORD` makes the old cookie 401.
- Five wrong POSTs, then a sixth right POST, is `Too many attempts. Try again shortly.` `reset_throttle()` then the right password is 200.
- Monkeypatch the save to raise `OSError`: logout is 500 `Could not store the session.` and that response does not clear the cookie. After the patch is removed, `GET /api/session` with the same cookie is 200.
- Logged-out `GET /docs` is 401. Logged-in `GET /docs` is 200.

WebSocket denial and the idle sweeper are Task 3. This task's middleware may 401 HTTP only.

### Task 3: WebSocket registry and idle sweeper

**Files:**
- Modify: `src/server.py` (`ws_ask`, plus the registry and sweeper)

**Interfaces:**
- Consumes: `touch`, `classify_key`, `token_key`, `TouchResult`.
- Produces: `drop_sockets(key: str)`, `note_deadline(key: str, last_access: int)`, `sweep_once(now: int | None = None)`. One `threading.Event` per accepted socket, stored with that socket. The HTTP middleware's `"dead"` branch and logout call `drop_sockets`. `"ok"` calls `note_deadline`.

- [ ] **Step 1: Register the socket and keep today's pump task.**

Before `accept`, `touch` the cookie. `"store_error"` denies with HTTP 500. Anything other than `"ok"` denies with HTTP 401 and an empty body via `send_denial_response`. Do not `close` before accept.

On `"ok"`, `accept`, create one `threading.Event`, register `(websocket, cancel, token_key)` under that hash, and `note_deadline`. Delete the per-question `cancel = threading.Event()` that `ws_ask` currently does just before `_pump`.

After a successful `receive`, `touch` is the first action, before the non-dict, cancel, empty-question, already-answering, and rebuild branches. `"dead"`: `cancel.set()`, `close(1008)`, return, no `send_json`. `"store_error"`: do not close and do not clear the cookie; leave the socket up. `"ok"`: `note_deadline`, then the existing branches.

In the question branch, after that `"ok"` and only if this socket is still registered, `cancel.clear()` on the line immediately before `pump_task = asyncio.create_task(run())`. `run` is today's wrapper: it increments `_active_answers`, clears `_idle()`, awaits `_pump` with that same `Event`, and sets `_idle()` when the count hits zero. Do not `await _pump` on the receive loop. The cancel frame `set()`s the event and starts `_notify_cancelled` without `clear()`. Disconnect still `set()`s the event and cancels `pump_task`.

- [ ] **Step 2: `drop_sockets` and the sweeper.**

`drop_sockets` `set()`s each connection's event, then `close(1008)`, and swallows `RuntimeError` if `close` races `send_json`. It does not `clear()` the event. It removes those sockets from the registry.

`note_deadline` stores `(key, last_access)` and wakes the sweeper. It does not keep the raw token.

`sweep_once` snapshots `list(deadlines)` before the first `await` (`classify_key` runs in `asyncio.to_thread`). `"ok"` replaces `last_access` and does not close. `"dead"` deletes that pair, then `drop_sockets`. `"store_error"` keeps the pair. The wait after a wake is `wake.clear()` first, then the soonest future deadline, or at most 60 seconds on `"store_error"`, or only the wake event when nothing is registered. A past deadline is not a zero timeout. The sweeper calls `classify_key`, never `touch` and never `classify`.

Logout's `removed` and `absent` paths `await drop_sockets(token_key)` after the file update. `store_error` does not. Login calls `drop_sockets` for each `evicted_keys` entry. Lifespan's `reload_password` does not call `drop_sockets`. A later `reload_password` inside a process that still has sockets (the throwaway script) does.

- [ ] **Step 3: Check the socket cases from spec Verification steps 9, 11, and 14.**

Denial, no cookie:

```python
from starlette.testclient import WebSocketDenialResponse

try:
    with client.websocket_connect("/ws") as websocket:
        raise AssertionError("handshake should have been denied")
except WebSocketDenialResponse as exc:
    assert exc.status_code == 401
    assert exc.content == b""
```

The exception comes from `__enter__`, not from `websocket_connect` itself. Do not put the token in the URL.

Same token: client B copies A's cookie, opens `/ws`, and sends no further frame. A logs out. B's `receive_text()` raises `WebSocketDisconnect` with `code == 1008`. B's `GET /api/session` is 401.

Different token: B logs in on its own and opens `/ws`. A logs out. B's `GET /api/session` is 200. B's `send_json({"cancel": true})` still gets `{"type": "cancelled"}`.

Sweeper, no 10-day sleep: after connect, `classify_key(token_key(cookie), now=T)` is `"ok"` and the socket is still open. `await sweep_once(now=T + SESSION_IDLE_SECONDS - 1)` does not close. `await sweep_once(now=T + SESSION_IDLE_SECONDS)` removes the row and B's `receive_text()` raises `WebSocketDisconnect` with `code == 1008`.

Do not call `build()`. Do not write the corpus. Patch the answer stream so a cancel during a fake in-flight answer sets the connection event and the next question on that same live socket still produces a token (`cancel.clear()` ran). A logout during that fake answer does not `clear()`.

### Task 4: Lock screen

**Files:**
- Modify: `src/static/index.html`
- Modify: `src/static/library.html`
- Modify: `src/static/app.css`
- Modify: `src/static/sw.js`

**Interfaces:**
- Consumes: `GET /api/session`, `POST /api/session`, `POST /api/session/logout`, WebSocket close code `1008`.
- Produces: `body.locked` until the session check is 200. `sessionOk()` returns `"store"` | `true` | `false` | `null`. Ask thread key `ask-northwind-chat` is not cleared on logout.

- [ ] **Step 1: Markup and CSS from the spec's Pages section.**

Both pages ship with `class="locked"` on `<body>`. `#logout` is a ghost button after the nav. `#lock` is the last child of `<main>`, not between the header and `#review-panel`. The form is `method="post"` `action="/api/session"` so a no-JS submit cannot put the password in the query string. The script `preventDefault`s and sends JSON. `#lock-reconnect` starts `hidden`. It is not `#reconnect`.

CSS is the block in the spec: hide `#thread`, `#ask-form`, `#index-note`, `.lede`, `#review-panel`, `#library`, and `#logout` while locked; hide `#lock` when not locked; `#lock { margin-top: 1.4rem }`; the lock form is a column; `#lock-password { width: 100% }`. Do not give the password input `flex: 1`. Reuse the text input's colors, padding, font, radius, and focus, not its flex.

- [ ] **Step 2: Duplicate the small script on both pages. No new script URL.**

`sessionOk` handles HTTP 500 before the other results and returns `"store"`. Then `true` on 200, `false` on 401, `null` if the fetch throws. First paint: `"store"` stays locked with `Could not store the session.` and does not connect. `true` removes `locked` and runs today's startup. `false` stays locked with an empty status. `null` stays locked, shows `#lock-reconnect`, and uses `Not connected. Use Reconnect.` on Ask and `You're offline.` on Library.

Unlock POSTs `{"password": input.value}`. 200 clears the input, removes `locked`, and starts the page (Ask focuses `#question`). 401 shows `body.detail`. 500 shows `Could not store the session.` and stays locked.

Logout 200 calls `showLock("")` and does not delete `ask-northwind-chat` or empty `#thread`. Logout 500 does not `showLock`.

Ask `onclose`: always clear the busy flag first. Code `1008` shows the lock with `Session ended. Enter the password.` and does not set Ready. Any other code calls `sessionOk`. `"store"` keeps the composer and sets that status. `false` shows the lock. `null` is the offline line. `true` is today's Ready line when not rebuilding. Do not call `connect` while `body` is locked.

`visibilitychange`, `checkRebuildOnLoad`, and `pollRebuild` return immediately while locked. A 401 is checked before `setIndexNote`. Ask's one-second status poll and Library's poll both follow that rule. Library checks 401 before the 409 branch on every existing fetch. 401 calls `showLock` and does not show "Could not load documents" or the rebuild sentence.

- [ ] **Step 3: Bump `CACHE` in `src/static/sw.js` to `ask-northwind-v10`.**

`SHELL` stays `/`, `/library`, `/app.css`, `/manifest.webmanifest`, `/sw.js`, and the two icons. The fetch handler still skips non-GET, `/ws`, and `/api/`.

- [ ] **Step 4: Check the shell.**

`GET /` and `GET /library` without a cookie are 200 and contain `id="lock"` and `Enter the master password`. The service worker source contains `ask-northwind-v10` and does not contain `ask-northwind-v9`.

Browser check once `LOCK_PASSWORD` is set and the server is restarted: locked first paint on both pages, wrong password shows `Wrong password.`, right password reveals Ask and Library, Log out returns the lock, a second browser stays in, reload keeps the Ask thread, New chat still clears only the thread. Desktop and a narrow viewport. 401 on `/api/status` while logged out does not flash the stale-index note.

### Task 5: Docs

**Files:**
- Modify: `src/README.md`
- Modify: `src/LEARNING.md`
- Modify: `AGENTS.md`

**Interfaces:**
- Consumes: the behavior from Tasks 1–4. No runtime change.

- [ ] **Step 1: Apply the spec's Documentation section.**

Filename is `AGENTS.md`, not `Agents.md`. Cover the leftovers named there: README's loader sentence and the "Auth, PDF/Word…" line; LEARNING's lifespan bullet, the `.env` loader list, and the Part 9 row; `AGENTS.md`'s "No auth.", the "do not add … login" sentence, "No auth on that port.", the loader list, and the bare "auth" in the gotchas list.

State that CLI scripts stay unlocked, Approve is still the human gate, the idle window is 10 days and slides, logout kills one token and its open sockets, a new login does not revoke older tokens, a password change plus restart kills rows because the old process exited (the new process does not send `1008`), and the cache name is `ask-northwind-v10`.

- [ ] **Step 2: Grep `src/README.md`, `src/LEARNING.md`, and `AGENTS.md`.**

Case-sensitive search for `no login`, `No auth`, and a bare `auth` on the unimplemented-list lines. Those claims are gone. Hits on `auth.py` are expected.

Do not edit older files under `docs/superpowers/specs/`.

---

## Self-review

Spec coverage: startup refusal, sliding `864000`, immediate logout including open sockets, second browser, password stamp, session file, cookie flags, throttle, public allowlist, 401-before-409, `classify_key` versus `classify`, one connection `Event` and `clear()` only before the next question, pump stays a task, sweeper wake and deadlines, `sessionOk` `"store"`, lock markup, cache `v10`, docs grep. CLI and retrieval stay out.

No pytest. No new dependency. No third HTML route.

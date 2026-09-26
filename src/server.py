"""
WebSocket transport for the existing RAG pipeline.

The server does not retrieve or prompt. It accepts a question plus earlier
turns, iterates `answer_stream()`, and forwards JSON events. A cancel frame
can arrive while that stream is still running. Retrieval and generation stay
in retrieve.py / generate.py so the CLI and the UI share one brain.

Logout and idle close an accepted socket without waiting for another
frame. That socket's one cancel event stays set so the in-flight answer
stops; only a later question on a socket that is still registered clears it.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, Response
from auth import (
    COOKIE_NAME,
    SESSION_IDLE_SECONDS,
    classify_key,
    is_public,
    login,
    logout,
    reload_password,
    require_lock_password,
    token_key,
    touch,
)
from build_index import IndexBuildError, build
from format_md import FormatError
from generate import answer_stream
from library import (
    MAX_UPLOAD_FILES,
    UnsafeNameError,
    UploadError,
    index_meta,
    list_docs,
    read_doc,
)
from llm import LLMConfigError
from review import (
    ConflictError,
    ReviewError,
    approve as approve_proposal,
    format_proposal,
    list_proposals,
    reject as reject_proposal,
    save_draft,
    stage_delete,
    stage_upload,
)
from retrieve import _load, reload as reload_index

ROOT = Path(__file__).parent
INDEX_PATH = ROOT / "index" / "chunks.faiss"
STATIC_DIR = ROOT / "static"
# Shell exports take precedence (override=False). Missing file is a no-op.
load_dotenv(ROOT / ".env")
HOST = "127.0.0.1"
PORT = 8000


def _listen_address() -> tuple[str, int]:
    """Bind address for the Web UI.

    HOST and PORT come from the environment (.env or the shell). A blank
    value keeps this computer only, on port 8000.
    """
    host = (os.environ.get("HOST") or "").strip() or HOST
    raw = (os.environ.get("PORT") or "").strip()
    if not raw:
        return host, PORT
    try:
        port = int(raw)
    except ValueError:
        raise SystemExit(f"PORT must be an integer, got {raw!r}")
    if not 1 <= port <= 65535:
        raise SystemExit(f"PORT must be between 1 and 65535, got {port}")
    return host, port


def _require_index() -> None:
    if not INDEX_PATH.exists():
        raise SystemExit(
            f"No index at {INDEX_PATH}. Run: python build_index.py"
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    require_lock_password()
    await asyncio.to_thread(reload_password)
    _require_index()
    await asyncio.to_thread(_load)
    # After the index is loaded. Startup does not close sockets.
    sweeper_task = asyncio.create_task(sweeper())
    try:
        yield
    finally:
        sweeper_task.cancel()
        try:
            await sweeper_task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Ask Northwind", lifespan=lifespan)


def _set_session_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=SESSION_IDLE_SECONDS,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(COOKIE_NAME, path="/", samesite="lax")


# Accepted sockets, keyed by the session-file hash. Not written to sessions.json.
# One threading.Event per connection, shared with _pump and drop_sockets.
@dataclass(eq=False)
class LiveSocket:
    websocket: WebSocket
    cancel: threading.Event
    token_key: str


_live: dict[str, set[LiveSocket]] = {}
_deadlines: dict[str, int] = {}
# Keys whose last sweep_once result was store_error. Not a connection event.
_sweep_retry: set[str] = set()
_wake = asyncio.Event()


def register(key: str, websocket: WebSocket, cancel: threading.Event) -> None:
    _live.setdefault(key, set()).add(LiveSocket(websocket, cancel, key))


def unregister(websocket: WebSocket) -> None:
    """Drop this socket. A no-op if drop_sockets already popped it."""
    empty: list[str] = []
    for key, sockets in _live.items():
        gone = {item for item in sockets if item.websocket is websocket}
        if gone:
            sockets.difference_update(gone)
        if not sockets:
            empty.append(key)
    for key in empty:
        del _live[key]


def still_registered(websocket: WebSocket) -> bool:
    for sockets in _live.values():
        for item in sockets:
            if item.websocket is websocket:
                return True
    return False


async def drop_sockets(key: str) -> None:
    """Cancel and close every accepted socket for this hash. Do not clear()."""
    sockets = _live.pop(key, None)
    if not sockets:
        return
    for item in list(sockets):
        item.cancel.set()
        try:
            await item.websocket.close(code=1008)
        except (RuntimeError, WebSocketDisconnect):
            # close races send_json, or this socket is already closed
            pass


def note_deadline(key: str, last_access: int) -> None:
    """Store the file key and last use, and wake the sweeper. No raw token."""
    _deadlines[key] = last_access
    _wake.set()


def _positive_wait() -> float | None:
    """Soonest future wake, or None when there is nothing to wait for.

    Never returns 0. A past deadline is skipped so a deleted row cannot
    busy-loop the process. A key whose last sweep was store_error waits
    at most 60 seconds, including when its remaining time is already past.
    """
    if not _deadlines and not _live:
        return None
    now = time.time()
    delays: list[float] = []
    for key, last_access in _deadlines.items():
        remaining = last_access + SESSION_IDLE_SECONDS - now
        if key in _sweep_retry:
            if remaining <= 0:
                delays.append(60.0)
            else:
                delays.append(min(remaining, 60.0))
        elif remaining > 0:
            delays.append(remaining)
    if not delays:
        return None
    soonest = min(delays)
    if soonest <= 0:
        return None
    return soonest


async def sweep_once(now: int | None = None) -> None:
    """Classify each stored deadline. Does not touch or hash the key again."""
    if now is None:
        now = int(time.time())
    snapshot = list(_deadlines)
    for key in snapshot:
        result = await asyncio.to_thread(classify_key, key, now)
        if result.state == "ok":
            _sweep_retry.discard(key)
            if result.last_access is not None:
                _deadlines[key] = result.last_access
        elif result.state == "dead":
            _deadlines.pop(key, None)
            _sweep_retry.discard(key)
            await drop_sockets(key)
        elif result.state == "store_error":
            _sweep_retry.add(key)


async def sweeper() -> None:
    while True:
        if not _deadlines and not _live:
            await _wake.wait()
            _wake.clear()
            continue
        await sweep_once(int(time.time()))
        timeout = _positive_wait()
        if timeout is None:
            await _wake.wait()
        else:
            try:
                await asyncio.wait_for(_wake.wait(), timeout)
            except asyncio.TimeoutError:
                pass
        _wake.clear()


def _client_key(request: Request) -> str:
    if request.client is None:
        return "unknown"
    return request.client.host.strip()


@app.middleware("http")
async def lock_middleware(request: Request, call_next):
    if is_public(request.method, request.url.path):
        return await call_next(request)
    token = request.cookies.get(COOKIE_NAME, "")
    result = await asyncio.to_thread(touch, token)
    if result.state == "store_error":
        return JSONResponse(
            {"detail": "Could not store the session."}, status_code=500
        )
    if result.state != "ok":
        await drop_sockets(token_key(token))
        response = JSONResponse({"detail": "Unauthorized."}, status_code=401)
        if token:
            _clear_session_cookie(response)
        return response
    note_deadline(token_key(token), result.last_access)
    response = await call_next(request)
    _set_session_cookie(response, token)
    return response


_rebuilding = False
_active_answers = 0
_answers_idle: asyncio.Event | None = None


def _idle() -> asyncio.Event:
    global _answers_idle
    if _answers_idle is None:
        _answers_idle = asyncio.Event()
        _answers_idle.set()
    return _answers_idle


def _status_body() -> dict:
    meta = index_meta()
    docs = list_docs()
    return {
        "rebuilding": _rebuilding,
        "indexed_at": meta.get("indexed_at"),
        "num_chunks": meta.get("num_chunks"),
        "stale": any(row["state"] != "indexed" for row in docs),
    }


def _docs_body() -> dict:
    docs = list_docs()
    proposals, _unreadable = list_proposals()
    pending = {item.target for item in proposals}
    for row in docs:
        row["pending"] = row["name"] in pending
    return {**_status_body(), "docs": docs}


def _review_body() -> dict:
    proposals, unreadable = list_proposals()
    return {
        "proposals": [item.as_dict() for item in proposals],
        "unreadable": unreadable,
    }


def _library_body(errors: list | None = None) -> dict:
    body = {**_docs_body(), **_review_body()}
    if errors:
        body["errors"] = errors
    return body


def _unlocked() -> None:
    if _rebuilding:
        raise HTTPException(409, "Index is rebuilding. Try again when it finishes.")


def _raise_review(exc: Exception) -> None:
    if isinstance(exc, ConflictError):
        raise HTTPException(409, str(exc)) from exc
    if isinstance(exc, FileNotFoundError):
        raise HTTPException(404, "File not found.") from exc
    if isinstance(exc, (ReviewError, UnsafeNameError, UploadError)):
        raise HTTPException(400, str(exc)) from exc
    raise exc


def _rebuild_sync() -> dict:
    config = build()
    try:
        reload_index()
    except Exception as exc:
        raise RuntimeError(
            "Index rebuilt on disk but failed to load. Restart the server."
        ) from exc
    return config


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/library")
def library_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "library.html")


@app.get("/app.css")
def app_css() -> FileResponse:
    return FileResponse(STATIC_DIR / "app.css", media_type="text/css")


@app.post("/api/session")
async def api_login(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"detail": "Expected JSON body."}, status_code=400)
    password = payload.get("password") if isinstance(payload, dict) else None
    if not isinstance(password, str):
        return JSONResponse({"detail": "Expected JSON body."}, status_code=400)
    result = await asyncio.to_thread(login, password, _client_key(request))
    if result.throttled:
        return JSONResponse(
            {"detail": "Too many attempts. Try again shortly."}, status_code=401
        )
    if result.store_error:
        return JSONResponse(
            {"detail": "Could not store the session."}, status_code=500
        )
    if result.token is None:
        return JSONResponse({"detail": "Wrong password."}, status_code=401)
    response = JSONResponse({"ok": True})
    _set_session_cookie(response, result.token)
    for key in result.evicted_keys:
        await drop_sockets(key)
    return response


@app.post("/api/session/logout")
async def api_logout(request: Request) -> JSONResponse:
    token = request.cookies.get(COOKIE_NAME, "")
    result = await asyncio.to_thread(logout, token)
    if result.state == "store_error":
        return JSONResponse(
            {"detail": "Could not store the session."}, status_code=500
        )
    response = JSONResponse({"ok": True})
    _clear_session_cookie(response)
    await drop_sockets(token_key(token))
    return response


@app.get("/api/session")
async def api_session(request: Request) -> JSONResponse:
    token = request.cookies.get(COOKIE_NAME, "")
    result = await asyncio.to_thread(touch, token)
    if result.state == "store_error":
        return JSONResponse(
            {"detail": "Could not store the session."}, status_code=500
        )
    if result.state != "ok":
        response = JSONResponse({"detail": "Unauthorized."}, status_code=401)
        _clear_session_cookie(response)
        await drop_sockets(token_key(token))
        return response
    response = JSONResponse({"ok": True})
    _set_session_cookie(response, token)
    return response


@app.get("/api/status")
def api_status() -> dict:
    return _status_body()


@app.get("/api/docs")
def api_docs() -> dict:
    return _docs_body()


@app.get("/api/docs/{name:path}")
def api_read_doc(name: str) -> dict:
    try:
        return read_doc(name)
    except UnsafeNameError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "File not found.") from exc


@app.post("/api/docs")
async def api_upload(files: list[UploadFile] = File(default=[])):
    _unlocked()
    if not files or len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, "Send between 1 and 20 markdown files.")
    errors = []
    for uf in files:
        data = await uf.read()
        try:
            stage_upload(uf.filename or "", data)
        except UploadError as exc:
            errors.append({"name": uf.filename or "", "message": str(exc)})
    if errors and len(errors) == len(files):
        return JSONResponse(status_code=400, content=_library_body(errors))
    if errors:
        return JSONResponse(status_code=207, content=_library_body(errors))
    return _library_body()


@app.delete("/api/docs/{name:path}")
def api_delete(name: str):
    _unlocked()
    try:
        stage_delete(name)
    except UnsafeNameError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(404, "File not found.") from exc
    return _library_body()


@app.get("/api/review")
def api_review() -> dict:
    return _review_body()


@app.put("/api/review/{name:path}")
async def api_save_draft(name: str, request: Request):
    _unlocked()
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(400, "Expected JSON body.") from exc
    body = payload.get("body") if isinstance(payload, dict) else None
    if not isinstance(body, str):
        raise HTTPException(400, "Expected JSON body.")
    try:
        proposal = save_draft(name, body)
    except Exception as exc:
        _raise_review(exc)
        raise
    return {**_library_body(), "proposal": proposal.as_dict()}


@app.post("/api/review/{name:path}/format")
def api_format(name: str):
    _unlocked()
    try:
        proposal = format_proposal(name)
    except FormatError as exc:
        raise HTTPException(422, str(exc)) from exc
    except LLMConfigError as exc:
        raise HTTPException(422, "No model is configured.") from exc
    except Exception as exc:
        _raise_review(exc)
        raise
    return {**_library_body(), "proposal": proposal.as_dict()}


@app.post("/api/review/{name:path}/approve")
def api_approve(name: str):
    _unlocked()
    try:
        approve_proposal(name)
    except Exception as exc:
        _raise_review(exc)
    return _library_body()


@app.delete("/api/review/{name:path}")
def api_reject(name: str):
    _unlocked()
    try:
        reject_proposal(name)
    except Exception as exc:
        _raise_review(exc)
    return _library_body()


@app.post("/api/reindex")
async def api_reindex():
    global _rebuilding
    if _rebuilding:
        raise HTTPException(409, "Index is rebuilding. Try again when it finishes.")
    _rebuilding = True
    rebuild: asyncio.Task | None = None
    try:
        await _idle().wait()
        # shield: a cancelled fetch must not abort the worker thread.
        rebuild = asyncio.create_task(asyncio.to_thread(_rebuild_sync))
        try:
            config = await asyncio.shield(rebuild)
        except IndexBuildError as exc:
            raise HTTPException(400, str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(500, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(500, str(exc)[:300]) from exc
        return {
            "ok": True,
            "num_chunks": config["num_chunks"],
            "indexed_at": config["indexed_at"],
            "files": config["files"],
        }
    finally:
        # Drop the lock only after the worker finishes, not on request cancel.
        if rebuild is not None and not rebuild.done():
            current = asyncio.current_task()
            if current is not None:
                current.uncancel()
            try:
                await rebuild
            except Exception:
                pass
        _rebuilding = False


@app.get("/manifest.webmanifest")
def manifest() -> FileResponse:
    path = STATIC_DIR / "manifest.webmanifest"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker() -> FileResponse:
    path = STATIC_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(
        path,
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/icons/{name}")
def icon(name: str) -> FileResponse:
    path = (STATIC_DIR / "icons" / name).resolve()
    if not str(path).startswith(str((STATIC_DIR / "icons").resolve())) or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path)


async def _pump(
    websocket: WebSocket,
    question: str,
    history: object,
    cancel: threading.Event,
    send_lock: asyncio.Lock,
) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def worker() -> None:
        try:
            for event in answer_stream(question, cancel=cancel, history=history):
                if cancel.is_set():
                    break
                asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
        except Exception as exc:
            if not cancel.is_set():
                asyncio.run_coroutine_threadsafe(
                    queue.put({"type": "error", "message": f"Generation failed: {exc}"}),
                    loop,
                ).result()
        finally:
            asyncio.run_coroutine_threadsafe(queue.put(None), loop).result()

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    try:
        while True:
            event = await queue.get()
            if event is None:
                break
            async with send_lock:
                await websocket.send_json(event)
    except WebSocketDisconnect:
        cancel.set()
        raise


async def _notify_cancelled(task: asyncio.Task, send) -> None:
    """Tell the page the in-flight answer has stopped.

    The cancel frame can arrive just after the pump task already finished.
    Waiting here covers the case where it is still running, and the caller
    sends the event itself when the task is already done.
    """
    try:
        await task
    except BaseException:
        pass
    try:
        await send({"type": "cancelled"})
    except Exception:
        pass


@app.websocket("/ws")
async def ws_ask(websocket: WebSocket) -> None:
    global _active_answers
    token = websocket.cookies.get(COOKIE_NAME, "")
    result = await asyncio.to_thread(touch, token)
    if result.state == "store_error":
        await websocket.send_denial_response(Response(status_code=500))
        return
    if result.state != "ok":
        await websocket.send_denial_response(Response(status_code=401))
        return
    await websocket.accept()
    send_lock = asyncio.Lock()
    cancel = threading.Event()  # one event for this connection; do not rebind
    pump_task: asyncio.Task | None = None
    background: set[asyncio.Task] = set()
    register(token_key(token), websocket, cancel)
    note_deadline(token_key(token), result.last_access)

    async def send(payload: dict) -> None:
        async with send_lock:
            await websocket.send_json(payload)

    try:
        while True:
            raw = await websocket.receive_json()
            result = await asyncio.to_thread(touch, token)
            if result.state == "store_error":
                continue
            if result.state != "ok":
                cancel.set()  # do not clear()
                await drop_sockets(token_key(token))
                return
            note_deadline(token_key(token), result.last_access)
            if not isinstance(raw, dict):
                await send(
                    {
                        "type": "error",
                        "message": "Ask a question about Northwind documents.",
                    }
                )
                continue
            if raw.get("cancel"):
                cancel.set()  # do not clear(); the in-flight worker must see it
                if pump_task is not None and not pump_task.done():
                    notice = asyncio.create_task(_notify_cancelled(pump_task, send))
                    background.add(notice)
                    notice.add_done_callback(background.discard)
                else:
                    await send({"type": "cancelled"})
                continue
            question = str(raw.get("question") or "").strip()
            if _rebuilding:
                await send(
                    {
                        "type": "error",
                        "message": "Index is rebuilding. Try again when it finishes.",
                    }
                )
                continue
            if pump_task is not None and not pump_task.done():
                await send({"type": "error", "message": "already answering"})
                continue
            if not question:
                await send(
                    {
                        "type": "error",
                        "message": "Ask a question about Northwind documents.",
                    }
                )
                continue
            if not still_registered(websocket):
                return  # drop_sockets already set(); do not clear()
            history = raw.get("history")

            async def run(
                question: str = question,
                history: object = history,
                turn_cancel: threading.Event = cancel,
            ) -> None:
                global _active_answers
                _active_answers += 1
                _idle().clear()
                try:
                    await _pump(websocket, question, history, turn_cancel, send_lock)
                finally:
                    _active_answers -= 1
                    if _active_answers == 0:
                        _idle().set()

            cancel.clear()
            pump_task = asyncio.create_task(run())
            # back to receive_json; do not await _pump on this loop
    except WebSocketDisconnect:
        cancel.set()
        if pump_task is not None and not pump_task.done():
            pump_task.cancel()
    finally:
        unregister(websocket)


def main() -> None:
    require_lock_password()
    _require_index()
    host, port = _listen_address()
    try:
        import uvicorn
    except ImportError:
        sys.exit("fastapi/uvicorn not installed. Run: pip install -r requirements.txt")
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()

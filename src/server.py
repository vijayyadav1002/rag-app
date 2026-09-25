"""
WebSocket transport for the existing RAG pipeline.

The server does not retrieve or prompt. It accepts one question, iterates
`answer_stream()`, and forwards JSON events. Retrieval and generation stay
in retrieve.py / generate.py so the CLI and the UI share one brain.
"""

from __future__ import annotations

import asyncio
import sys
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
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
HOST = "127.0.0.1"
PORT = 8000


def _require_index() -> None:
    if not INDEX_PATH.exists():
        raise SystemExit(
            f"No index at {INDEX_PATH}. Run: python build_index.py"
        )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    _require_index()
    await asyncio.to_thread(_load)
    yield


app = FastAPI(title="Ask Northwind", lifespan=lifespan)

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


@app.websocket("/ws")
async def ws_ask(websocket: WebSocket) -> None:
    global _active_answers
    await websocket.accept()
    busy = False
    cancel = threading.Event()
    try:
        while True:
            raw = await websocket.receive_json()
            question = str(raw.get("question") or "").strip()
            if _rebuilding:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "Index is rebuilding. Try again when it finishes.",
                    }
                )
                continue
            if busy:
                await websocket.send_json(
                    {"type": "error", "message": "already answering"}
                )
                continue
            if not question:
                await websocket.send_json(
                    {
                        "type": "error",
                        "message": "Ask a question about Northwind documents.",
                    }
                )
                continue
            busy = True
            cancel.clear()
            _active_answers += 1
            _idle().clear()
            try:
                await _pump(websocket, question, cancel)
            finally:
                _active_answers -= 1
                if _active_answers == 0:
                    _idle().set()
                busy = False
    except WebSocketDisconnect:
        cancel.set()


async def _pump(
    websocket: WebSocket, question: str, cancel: threading.Event
) -> None:
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    def worker() -> None:
        try:
            for event in answer_stream(question, cancel=cancel):
                if cancel.is_set():
                    break
                asyncio.run_coroutine_threadsafe(queue.put(event), loop).result()
        except Exception as exc:
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
            await websocket.send_json(event)
    except WebSocketDisconnect:
        cancel.set()
        raise


def main() -> None:
    _require_index()
    try:
        import uvicorn
    except ImportError:
        sys.exit("fastapi/uvicorn not installed. Run: pip install -r requirements.txt")
    uvicorn.run(app, host=HOST, port=PORT)


if __name__ == "__main__":
    main()

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

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from build_index import IndexBuildError, build
from generate import answer_stream
from library import (
    MAX_UPLOAD_FILES,
    UnsafeNameError,
    UploadError,
    delete_doc,
    index_meta,
    list_docs,
    save_upload,
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
    return {
        "rebuilding": _rebuilding,
        "indexed_at": meta.get("indexed_at"),
        "num_chunks": meta.get("num_chunks"),
    }


def _docs_body() -> dict:
    return {**_status_body(), "docs": list_docs()}


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


@app.get("/api/status")
def api_status() -> dict:
    return _status_body()


@app.get("/api/docs")
def api_docs() -> dict:
    return _docs_body()


@app.post("/api/docs")
async def api_upload(files: list[UploadFile] = File(default=[])):
    if _rebuilding:
        raise HTTPException(409, "Index is rebuilding. Try again when it finishes.")
    if not files or len(files) > MAX_UPLOAD_FILES:
        raise HTTPException(400, "Send between 1 and 20 markdown files.")
    errors = []
    for uf in files:
        data = await uf.read()
        try:
            save_upload(uf.filename or "", data)
        except UploadError as exc:
            errors.append({"name": uf.filename or "", "message": str(exc)})
    body = _docs_body()
    if errors and len(errors) == len(files):
        return JSONResponse(status_code=400, content={**body, "errors": errors})
    if errors:
        return JSONResponse(status_code=207, content={**body, "errors": errors})
    return body


@app.delete("/api/docs/{name}")
def api_delete(name: str):
    if _rebuilding:
        raise HTTPException(409, "Index is rebuilding. Try again when it finishes.")
    try:
        delete_doc(name)
    except UnsafeNameError as exc:
        raise HTTPException(400, str(exc)) from exc
    except FileNotFoundError:
        raise HTTPException(404, "File not found.")
    return _docs_body()


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

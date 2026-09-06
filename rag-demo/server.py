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

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from generate import answer_stream
from retrieve import _load

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


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.websocket("/ws")
async def ws_ask(websocket: WebSocket) -> None:
    await websocket.accept()
    busy = False
    cancel = threading.Event()
    try:
        while True:
            raw = await websocket.receive_json()
            question = str(raw.get("question") or "").strip()
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
            try:
                await _pump(websocket, question, cancel)
            finally:
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

"""Operate docs/ for the PWA library.

Filenames are validated to block path traversal; only basenames ending in
.md are accepted because the chunker only reads markdown. Upload and delete
only touch markdown on disk. They do not rebuild FAISS — search still reads
index/ until someone calls build() + reload().
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

DOCS_DIR = Path(__file__).parent / "docs"
INDEX_DIR = Path(__file__).parent / "index"
MAX_UPLOAD_BYTES = 1_000_000
MAX_UPLOAD_FILES = 20


class UnsafeNameError(ValueError):
    pass


class UploadError(ValueError):
    pass


def safe_md_name(name: str) -> str:
    if not name or "\x00" in name or "/" in name or "\\" in name:
        raise UnsafeNameError("Unsafe filename.")
    base = Path(name).name
    if base != name or base in (".", "..") or base.startswith("."):
        raise UnsafeNameError("Unsafe filename.")
    if not base.lower().endswith(".md") or len(base) < 4:
        raise UnsafeNameError("Only .md filenames are allowed.")
    return base


def index_meta(index_dir: Path | None = None) -> dict:
    index_dir = index_dir or INDEX_DIR
    path = index_dir / "config.json"
    if not path.exists():
        return {"indexed_at": None, "num_chunks": None, "files": []}
    with open(path) as f:
        cfg = json.load(f)
    files = cfg.get("files") or []
    if not isinstance(files, list):
        files = []
    return {
        "indexed_at": cfg.get("indexed_at"),
        "num_chunks": cfg.get("num_chunks"),
        "files": [str(x) for x in files],
    }


def list_docs(docs_dir: Path | None = None, index_dir: Path | None = None) -> list[dict]:
    docs_dir = docs_dir or DOCS_DIR
    meta = index_meta(index_dir)
    indexed = set(meta["files"])
    disk = {}
    if docs_dir.exists():
        for path in docs_dir.glob("*.md"):
            st = path.stat()
            disk[path.name] = {
                "name": path.name,
                "size": st.st_size,
                "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            }
    names = sorted(set(disk) | indexed)
    rows = []
    for name in names:
        if name in disk:
            state = "indexed" if name in indexed else "not_indexed"
            rows.append({**disk[name], "state": state})
        else:
            rows.append(
                {
                    "name": name,
                    "size": 0,
                    "mtime": None,
                    "state": "missing_on_disk",
                }
            )
    return rows


def save_upload(filename: str, data: bytes, docs_dir: Path | None = None) -> str:
    docs_dir = docs_dir or DOCS_DIR
    try:
        name = safe_md_name(filename)
    except UnsafeNameError as exc:
        raise UploadError(str(exc)) from exc
    if not data:
        raise UploadError("Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("File larger than 1 MB.")
    docs_dir.mkdir(parents=True, exist_ok=True)
    (docs_dir / name).write_bytes(data)
    return name


def delete_doc(name: str, docs_dir: Path | None = None) -> None:
    docs_dir = docs_dir or DOCS_DIR
    safe = safe_md_name(name)
    path = docs_dir / safe
    if not path.is_file():
        raise FileNotFoundError(safe)
    path.unlink()

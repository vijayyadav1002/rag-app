"""Operate the corpus folder for the PWA library.

Upload accepts basenames only (no slashes) and writes into the DOCS_DIR
root. Delete and list use posix-relative paths so nested files already on
disk can be shown and removed. The chunker only reads markdown. Upload and
delete only touch files on disk. They do not rebuild FAISS — search still
reads index/ until someone calls build() + reload().
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from chunk import markdown_paths, resolve_docs_dir

DOCS_DIR = resolve_docs_dir()
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


def safe_md_relpath(name: str) -> str:
    """Relative corpus path for delete/list identity. Rejects traversal."""
    if not name or "\x00" in name or "\\" in name:
        raise UnsafeNameError("Unsafe filename.")
    path = Path(name)
    if path.is_absolute() or path.anchor:
        raise UnsafeNameError("Unsafe filename.")
    parts = path.parts
    if not parts or any(part in (".", "..") or part.startswith(".") for part in parts):
        raise UnsafeNameError("Unsafe filename.")
    if not path.name.lower().endswith(".md") or len(path.name) < 4:
        raise UnsafeNameError("Only .md filenames are allowed.")
    return path.as_posix()


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
    for path, rel in markdown_paths(docs_dir):
        st = path.stat()
        disk[rel] = {
            "name": rel,
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


def live_path(name: str, docs_dir: Path | None = None) -> tuple[Path, str]:
    """Resolved file and its posix-relative name. Rejects paths outside the corpus."""
    root = (docs_dir or DOCS_DIR).resolve()
    rel = safe_md_relpath(name)
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise UnsafeNameError("Unsafe filename.") from exc
    return path, rel


def file_mtime(path: Path) -> tuple[str | None, int | None]:
    if not path.is_file():
        return None, None
    st = path.stat()
    iso = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat()
    return iso, st.st_mtime_ns


def read_doc(name: str, docs_dir: Path | None = None) -> dict:
    path, rel = live_path(name, docs_dir)
    if not path.is_file():
        raise FileNotFoundError(rel)
    iso, _ns = file_mtime(path)
    return {"name": rel, "body": path.read_text(encoding="utf-8"), "mtime": iso}


def write_doc(name: str, data: bytes, docs_dir: Path | None = None) -> str:
    """Atomic replace. A crash before replace leaves the previous file in place."""
    if not data:
        raise UploadError("Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("File larger than 1 MB.")
    path, rel = live_path(name, docs_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)
    return rel


def delete_doc(name: str, docs_dir: Path | None = None) -> None:
    path, rel = live_path(name, docs_dir)
    if not path.is_file():
        raise FileNotFoundError(rel)
    path.unlink()

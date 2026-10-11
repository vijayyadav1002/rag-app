"""Operate the corpus folder for the PWA library.

Upload accepts basenames only (no slashes) and writes into the DOCS_DIR
root. Delete and list use posix-relative paths so nested files already on
disk can be shown and removed. The chunker only reads markdown. Upload and
delete only touch files on disk. They do not rebuild FAISS — search still
reads index/ until someone calls build() + reload(). List marks an indexed
file `changed` when its bytes differ from the digest stored at index
time. An older index with no digests still falls back to mtime, because a
cloud sync can touch a timestamp without changing the text.
"""
from __future__ import annotations

import hashlib
import json
import pickle
from datetime import datetime, timezone
from pathlib import Path

from chunk import markdown_paths, resolve_docs_dir

DOCS_DIR = resolve_docs_dir()
INDEX_DIR = Path(__file__).parent / "index"
MAX_UPLOAD_BYTES = 1_000_000
MAX_UPLOAD_FILES = 20
# (resolved pickle path, mtime_ns, counts). A failed read is not cached.
_chunk_count_cache: tuple[str, int, dict[str, int]] | None = None


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


def file_digest(path: Path) -> str:
    """SHA-256 of the file bytes. Used to tell an edit from a timestamp touch."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def clean_scope(value: object) -> str:
    """Folder or markdown path a question is allowed to search.

    A file scope ends in `.md`. Anything else is a folder prefix. Hidden
    parts and traversal are rejected so a socket payload cannot escape the corpus.
    """
    if not isinstance(value, str):
        raise UnsafeNameError("Choose a folder or a file.")
    raw = value.strip()
    if not raw or "\x00" in raw or "\\" in raw:
        raise UnsafeNameError("Choose a folder or a file.")
    path = Path(raw)
    if path.is_absolute() or path.anchor or not path.parts:
        raise UnsafeNameError("Choose a folder or a file.")
    if any(part in (".", "..") or part.startswith(".") for part in path.parts):
        raise UnsafeNameError("Choose a folder or a file.")
    return path.as_posix()


def index_meta(index_dir: Path | None = None) -> dict:
    index_dir = index_dir or INDEX_DIR
    path = index_dir / "config.json"
    if not path.exists():
        return {"indexed_at": None, "num_chunks": None, "files": [], "digests": {}}
    with open(path) as f:
        cfg = json.load(f)
    files = cfg.get("files") or []
    if not isinstance(files, list):
        files = []
    raw = cfg.get("digests") or {}
    digests: dict[str, str] = {}
    if isinstance(raw, dict):
        for key, digest in raw.items():
            if isinstance(key, str) and isinstance(digest, str) and len(digest) == 64:
                digests[key] = digest
    return {
        "indexed_at": cfg.get("indexed_at"),
        "num_chunks": cfg.get("num_chunks"),
        "files": [str(x) for x in files],
        "digests": digests,
    }


def _parse_time(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def chunk_counts(index_dir: Path | None = None) -> dict[str, int] | None:
    """How many indexed windows each source file has.

    The library page shows that count beside the file. Reading the pickle on
    every refresh would redo the same work for a large vault, so the result
    is kept until `metadata.pkl` changes. Missing or unreadable metadata
    returns None — the row then omits the count instead of showing a zero
    that might mean "not chunked."
    """
    global _chunk_count_cache
    index_dir = index_dir or INDEX_DIR
    path = index_dir / "metadata.pkl"
    if not path.is_file():
        return None
    try:
        stamp = path.stat().st_mtime_ns
        key = str(path.resolve())
    except OSError:
        return None
    if (
        _chunk_count_cache
        and _chunk_count_cache[0] == key
        and _chunk_count_cache[1] == stamp
    ):
        return _chunk_count_cache[2]
    try:
        with open(path, "rb") as handle:
            chunks = pickle.load(handle)
    except (OSError, pickle.UnpicklingError, EOFError, AttributeError, ImportError, ModuleNotFoundError):
        return None
    if not isinstance(chunks, list):
        return None
    counts: dict[str, int] = {}
    for chunk in chunks:
        name = getattr(chunk, "source_file", None)
        if isinstance(name, str) and name:
            counts[name] = counts.get(name, 0) + 1
    _chunk_count_cache = (key, stamp, counts)
    return counts


def list_docs(docs_dir: Path | None = None, index_dir: Path | None = None) -> list[dict]:
    docs_dir = docs_dir or DOCS_DIR
    meta = index_meta(index_dir)
    counts = chunk_counts(index_dir)
    indexed = set(meta["files"])
    indexed_at = _parse_time(meta.get("indexed_at"))
    digests = meta.get("digests") or {}
    disk = {}
    for path, rel in markdown_paths(docs_dir):
        st = path.stat()
        disk[rel] = {
            "name": rel,
            "size": st.st_size,
            "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "path": path,
        }
    names = sorted(set(disk) | indexed)
    rows = []
    for name in names:
        if name in disk:
            state = "indexed" if name in indexed else "not_indexed"
            recorded = digests.get(name)
            if state == "indexed" and recorded:
                try:
                    current = file_digest(disk[name]["path"])
                except OSError:
                    current = ""
                if current != recorded:
                    state = "changed"
            elif state == "indexed":
                mtime = _parse_time(disk[name]["mtime"])
                if indexed_at is not None and mtime is not None and mtime > indexed_at:
                    state = "changed"
            row = {key: value for key, value in disk[name].items() if key != "path"}
            rows.append({**row, "state": state, "chunk_count": _chunk_count(counts, name)})
        else:
            rows.append(
                {
                    "name": name,
                    "size": 0,
                    "mtime": None,
                    "state": "missing_on_disk",
                    "chunk_count": _chunk_count(counts, name),
                }
            )
    return rows


def _chunk_count(counts: dict[str, int] | None, name: str) -> int | None:
    if counts is None:
        return None
    return counts.get(name, 0)


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


def search_phrases(
    query: str,
    docs_dir: Path | None = None,
    limit: int = 30,
) -> list[dict]:
    """Exact line matches. This is the search between a filename filter and Ask."""
    needle = (query or "").strip().lower()
    if len(needle) < 2:
        return []
    docs_dir = docs_dir or DOCS_DIR
    hits: list[dict] = []
    for path, rel in markdown_paths(docs_dir):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if needle not in line.lower():
                continue
            hits.append({"name": rel, "line": number, "text": line.strip()[:240]})
            if len(hits) >= limit:
                return hits
    return hits


def delete_doc(name: str, docs_dir: Path | None = None) -> None:
    path, rel = live_path(name, docs_dir)
    if not path.is_file():
        raise FileNotFoundError(rel)
    path.unlink()

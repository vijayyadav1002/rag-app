"""PDF text and corpus images, without a second copy of the note.

Writing a sibling markdown file on Re-index would let search cite text the
open PDF no longer contains, and it would write the notes folder. The text
layer becomes ordinary chunks headed by the page. The PDF stays the file a
citation opens. Images are listed and served. They are not embedded.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from chunk import chunk_document, markdown_paths, resolve_docs_dir, windowed_chunks
from library import UnsafeNameError, file_digest, safe_folder, safe_md_relpath

MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}

MAX_MEDIA_BYTES = 8_000_000
MAX_PDF_PAGES = 100
ASSET_STATE = "asset"
PAGE_CAP_WARNING = "Only the first 100 pages were indexed."
STAGE_DIR = Path(__file__).resolve().parent / "media_stage"


class MediaPathError(ValueError):
    """Traversal, a hidden segment, a disallowed suffix, or a non-file."""


class MediaTooLarge(ValueError):
    """Upload or staged bytes exceed MAX_MEDIA_BYTES. Raised before pypdf."""


class PdfExtractError(Exception):
    """The PDF could not be read or decrypted. Not an empty text layer."""


@dataclass(frozen=True)
class _PdfPage:
    number: int
    text: str


@dataclass(frozen=True)
class MediaFile:
    path: Path
    rel: str
    kind: Literal["image", "pdf"]


@dataclass(frozen=True)
class MediaBytes:
    rel: str
    filename: str
    content_type: str
    body: bytes


@dataclass(frozen=True)
class Corpus:
    """Chunks a build may embed, and the files list it must publish.

    `files` includes every PDF on disk, even when the text layer is empty.
    Images are absent. `warnings` is only the page cap. It is not retrieved.
    """

    chunks: tuple
    files: tuple[str, ...]
    warnings: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class StagedMedia:
    rel: str
    op: Literal["upsert", "delete"]
    kind: Literal["image", "pdf"]
    size: int
    content_type: str


def upload_kind(filename: str) -> Literal["markdown", "image", "pdf"]:
    """Classify an upload basename. Does not read the disk."""
    if not filename or "\x00" in filename or "/" in filename or "\\" in filename:
        raise MediaPathError("Unsafe filename.")
    base = Path(filename).name
    if base != filename or base in (".", "..") or base.startswith("."):
        raise MediaPathError("Unsafe filename.")
    lower = base.lower()
    if lower.endswith(".md") and len(base) >= 4:
        return "markdown"
    suffix = Path(base).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in MEDIA_TYPES:
        return "image"
    raise MediaPathError("Only .md, .pdf, and image files are allowed.")


def media_relpath(name: str) -> str:
    """Posix corpus path the byte route may open. Markdown is not media."""
    if not name or "\x00" in name or "\\" in name:
        raise MediaPathError("Unsafe filename.")
    path = Path(name)
    if path.is_absolute() or path.anchor:
        raise MediaPathError("Unsafe filename.")
    parts = path.parts
    if not parts or any(part in (".", "..") or part.startswith(".") for part in parts):
        raise MediaPathError("Unsafe filename.")
    suffix = path.suffix.lower()
    if suffix not in MEDIA_TYPES:
        raise MediaPathError("Only image and PDF files can be opened.")
    return path.as_posix()


def indexable_relpath(name: str) -> str:
    """Posix path a re-index may name: `.md` or `.pdf`."""
    suffix = Path(name).suffix.lower() if isinstance(name, str) else ""
    if suffix == ".md":
        try:
            return safe_md_relpath(name)
        except UnsafeNameError as exc:
            raise MediaPathError(str(exc)) from exc
    if suffix == ".pdf":
        return media_relpath(name)
    raise MediaPathError("Only .md and .pdf files can be indexed.")


def inline_filename(name: str) -> str:
    base = Path(name).name.replace('"', "").replace("\r", "").replace("\n", "")
    return f'inline; filename="{base}"'


def _contained(root: Path, rel: str) -> Path:
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise MediaPathError("Unsafe filename.") from exc
    return path


def open_media(name: str, docs_dir: Path | None = None) -> MediaBytes:
    """Read one live corpus file for GET /api/media/{name}."""
    root = (docs_dir or resolve_docs_dir()).resolve()
    rel = media_relpath(name)
    path = _contained(root, rel)
    if not path.exists():
        raise FileNotFoundError(rel)
    if not path.is_file():
        raise MediaPathError("Unsafe filename.")
    suffix = Path(rel).suffix.lower()
    return MediaBytes(rel, Path(rel).name, MEDIA_TYPES[suffix], path.read_bytes())


def media_paths(docs_dir: Path) -> list[MediaFile]:
    """Live images and PDFs under docs_dir, sorted by relative path."""
    root = docs_dir.resolve()
    if not root.is_dir():
        return []
    found: list[MediaFile] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        suffix = path.suffix.lower()
        if suffix not in MEDIA_TYPES:
            continue
        try:
            resolved = path.resolve()
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        kind: Literal["image", "pdf"] = "pdf" if suffix == ".pdf" else "image"
        found.append(MediaFile(resolved, rel.as_posix(), kind))
    found.sort(key=lambda item: item.rel)
    return found


def _clean_page(text: str | None) -> str:
    if not text:
        return ""
    return text.replace("\x00", "").replace("\f", "\n")


def _extract_pdf_pages(path: Path, source_file: str) -> tuple[list[_PdfPage], bool]:
    """Text layer, capped. A read or decrypt failure raises PdfExtractError.

    A page that has no text, or whose own extract fails, is an empty string.
    That is not a failure of the file. A missing pypdf install is
    MediaPathError. Callers stop at collect_corpus and collect_named.
    """
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise MediaPathError("pypdf is not installed.") from exc
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                opened = reader.decrypt("")
            except Exception as exc:
                raise PdfExtractError(f"Could not read {source_file}.") from exc
            if not opened:
                raise PdfExtractError(f"Could not read {source_file}.")
        total = len(reader.pages)
    except PdfExtractError:
        raise
    except (PdfReadError, OSError, ValueError, TypeError) as exc:
        raise PdfExtractError(f"Could not read {source_file}.") from exc
    truncated = total > MAX_PDF_PAGES
    pages: list[_PdfPage] = []
    for number in range(1, min(total, MAX_PDF_PAGES) + 1):
        try:
            raw = reader.pages[number - 1].extract_text()
        except Exception:
            raw = ""
        pages.append(_PdfPage(number, _clean_page(raw)))
    return pages, truncated


def _pdf_chunks(path: Path, source_file: str) -> tuple[list, str | None]:
    pages, truncated = _extract_pdf_pages(path, source_file)
    stem = Path(source_file).stem
    id_stem = Path(source_file).with_suffix("").as_posix().replace("/", "_")
    chunks = []
    for page in pages:
        if not page.text.strip():
            continue
        heading = f"{stem} > Page {page.number}"
        chunks.extend(
            windowed_chunks(
                page.text,
                source_file=source_file,
                heading=heading,
                id_stem=id_stem,
                section_index=page.number,
            )
        )
    warning = PAGE_CAP_WARNING if truncated else None
    return chunks, warning


def collect_corpus(docs_dir: Path) -> Corpus:
    """Markdown windows, then PDF windows. Does not write the index or the notes."""
    chunks = []
    files: list[str] = []
    warnings: list[tuple[str, str]] = []
    for path, rel in markdown_paths(docs_dir):
        produced = chunk_document(path, source_file=rel)
        chunks.extend(produced)
        if produced:
            files.append(rel)
    for item in media_paths(docs_dir):
        if item.kind != "pdf":
            continue
        produced, warning = _pdf_chunks(item.path, item.rel)
        chunks.extend(produced)
        files.append(item.rel)
        if warning:
            warnings.append((item.rel, warning))
    return Corpus(tuple(chunks), tuple(sorted(set(files))), tuple(warnings))


def collect_named(docs_dir: Path, rels: tuple[str, ...]) -> Corpus:
    """Plan for paths rebuild_paths already checked. Does not walk the corpus."""
    if not rels:
        return Corpus((), ())
    root = docs_dir.resolve()
    chunks = []
    files: list[str] = []
    warnings: list[tuple[str, str]] = []
    for rel in rels:
        checked = indexable_relpath(rel)
        path = _contained(root, checked)
        if not path.is_file():
            continue
        if checked.lower().endswith(".md"):
            produced = chunk_document(path, source_file=checked)
            chunks.extend(produced)
            if produced:
                files.append(checked)
            continue
        produced, warning = _pdf_chunks(path, checked)
        chunks.extend(produced)
        files.append(checked)
        if warning:
            warnings.append((checked, warning))
    return Corpus(tuple(chunks), tuple(sorted(set(files))), tuple(warnings))


def _stage_root(stage_dir: Path | None) -> Path:
    return (stage_dir or STAGE_DIR).resolve()


def _stage_folder(stage_dir: Path | None, rel: str) -> Path:
    root = _stage_root(stage_dir)
    key = hashlib.sha256(rel.encode("utf-8")).hexdigest()[:32]
    folder = (root / key).resolve()
    try:
        folder.relative_to(root)
    except ValueError as exc:
        raise MediaPathError("Unsafe filename.") from exc
    return folder


def _read_meta(folder: Path) -> dict | None:
    path = folder / "meta.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _staged_from_meta(meta: dict) -> StagedMedia:
    rel = media_relpath(str(meta.get("rel") or ""))
    op = meta.get("op")
    if op not in ("upsert", "delete"):
        raise MediaPathError("Unsafe filename.")
    kind = meta.get("kind")
    if kind not in ("image", "pdf"):
        raise MediaPathError("Unsafe filename.")
    size = meta.get("size")
    if not isinstance(size, int) or size < 0:
        size = 0
    content_type = MEDIA_TYPES[Path(rel).suffix.lower()]
    return StagedMedia(rel, op, kind, size, content_type)


def _write_stage(folder: Path, meta: dict, blob: bytes | None) -> None:
    folder.parent.mkdir(parents=True, exist_ok=True)
    tmp = folder.with_name(folder.name + ".tmp")
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    if blob is not None:
        (tmp / "blob").write_bytes(blob)
    if folder.exists():
        shutil.rmtree(folder)
    tmp.rename(folder)


def _drop_stage(folder: Path) -> None:
    if folder.exists():
        shutil.rmtree(folder)


def _load_staged(name: str, stage_dir: Path | None) -> tuple[Path, dict]:
    rel = media_relpath(name)
    folder = _stage_folder(stage_dir, rel)
    meta = _read_meta(folder)
    if not meta or meta.get("rel") != rel:
        raise FileNotFoundError(rel)
    return folder, meta


def stage_media(
    filename: str,
    data: bytes,
    *,
    folder: str = "",
    docs_dir: Path | None = None,
    stage_dir: Path | None = None,
) -> StagedMedia:
    """Park image or PDF bytes outside the corpus until Approve."""
    del docs_dir  # The corpus is not written here. Approve is the gate.
    kind = upload_kind(filename)
    if kind == "markdown":
        raise MediaPathError("Only image and PDF files can be staged here.")
    base = Path(filename).name
    try:
        prefix = safe_folder(folder) if folder else ""
    except UnsafeNameError as exc:
        raise MediaPathError(str(exc)) from exc
    rel = media_relpath(prefix + "/" + base if prefix else base)
    if not data:
        raise MediaPathError("Empty file.")
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaTooLarge("File larger than 8 MB.")
    content_type = MEDIA_TYPES[Path(rel).suffix.lower()]
    meta = {
        "rel": rel,
        "op": "upsert",
        "kind": kind,
        "size": len(data),
        "content_type": content_type,
    }
    _write_stage(_stage_folder(stage_dir, rel), meta, data)
    return StagedMedia(rel, "upsert", kind, len(data), content_type)


def stage_delete(
    name: str,
    *,
    docs_dir: Path | None = None,
    stage_dir: Path | None = None,
) -> StagedMedia:
    """Park a delete of a live image or PDF. Does not remove the live file."""
    root = (docs_dir or resolve_docs_dir()).resolve()
    rel = media_relpath(name)
    path = _contained(root, rel)
    if not path.is_file():
        raise FileNotFoundError(rel)
    kind: Literal["image", "pdf"] = "pdf" if path.suffix.lower() == ".pdf" else "image"
    content_type = MEDIA_TYPES[path.suffix.lower()]
    meta = {
        "rel": rel,
        "op": "delete",
        "kind": kind,
        "size": 0,
        "content_type": content_type,
        "digest": file_digest(path),
    }
    _write_stage(_stage_folder(stage_dir, rel), meta, None)
    return StagedMedia(rel, "delete", kind, 0, content_type)


def pending_media(stage_dir: Path | None = None) -> list[StagedMedia]:
    root = _stage_root(stage_dir)
    if not root.is_dir():
        return []
    items: list[StagedMedia] = []
    for folder in root.iterdir():
        if not folder.is_dir() or folder.name.startswith("."):
            continue
        try:
            resolved = folder.resolve()
            resolved.relative_to(root)
        except ValueError:
            continue
        meta = _read_meta(folder)
        if not meta:
            continue
        try:
            items.append(_staged_from_meta(meta))
        except MediaPathError:
            continue
    items.sort(key=lambda item: item.rel)
    return items


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


def approve_media(
    name: str,
    *,
    docs_dir: Path | None = None,
    stage_dir: Path | None = None,
) -> None:
    """Make one staged media op live. Does not rebuild the index."""
    folder, meta = _load_staged(name, stage_dir)
    rel = str(meta.get("rel") or "")
    root = (docs_dir or resolve_docs_dir()).resolve()
    path = _contained(root, media_relpath(rel))
    op = meta.get("op")
    if op == "delete":
        if not path.is_file():
            _drop_stage(folder)
            return
        recorded = meta.get("digest")
        if not isinstance(recorded, str) or file_digest(path) != recorded:
            raise MediaPathError("The live file changed since this draft was staged.")
        path.unlink()
        _drop_stage(folder)
        return
    if op != "upsert":
        raise MediaPathError("Unsafe filename.")
    blob_path = folder / "blob"
    if not blob_path.is_file():
        raise FileNotFoundError(rel)
    data = blob_path.read_bytes()
    if len(data) > MAX_MEDIA_BYTES:
        raise MediaTooLarge("File larger than 8 MB.")
    _atomic_bytes(path, data)
    _drop_stage(folder)


def reject_media(name: str, *, stage_dir: Path | None = None) -> None:
    """Drop a staged media op. Does not touch the live corpus."""
    folder, _meta = _load_staged(name, stage_dir)
    _drop_stage(folder)

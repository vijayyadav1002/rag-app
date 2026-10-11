"""Corpus edits wait here until a person approves them.

Writing an upload straight into DOCS_DIR would let a formatter rewrite a
policy before anyone read it, and the next Re-index would embed that
rewrite. A proposal sits outside the corpus, so search keeps the last
approved file until Approve and then Re-index.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import format_md
import library
from format_md import FormatError
from library import (
    MAX_UPLOAD_BYTES,
    UnsafeNameError,
    UploadError,
    delete_doc,
    file_mtime,
    live_path,
    safe_md_name,
    safe_md_relpath,
    write_doc,
)
from llm import LLMConfigError

REVIEW_DIR = Path(__file__).parent / "review"


class ReviewError(ValueError):
    pass


class ConflictError(ReviewError):
    pass


@dataclass
class Proposal:
    target: str
    op: str
    body: str
    origin: str
    base_mtime: str | None
    base_mtime_ns: int | None
    updated_at: str
    format_error: str | None = None
    dest: str | None = None
    base_digest: str | None = None

    def as_dict(self) -> dict:
        return asdict(self)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _root(review_dir: Path | None) -> Path:
    return (review_dir or REVIEW_DIR).resolve()


def _proposal_path(target: str, review_dir: Path | None = None) -> Path:
    root = _root(review_dir)
    rel = safe_md_relpath(target)
    path = (root / f"{rel}.json").resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise UnsafeNameError("Unsafe filename.") from exc
    return path


def _proposal_from(data: dict) -> Proposal:
    if not isinstance(data, dict):
        raise ValueError("proposal")
    op = str(data.get("op"))
    if op not in ("upsert", "delete", "rename"):
        raise ValueError("op")
    if data.get("origin") not in ("ai", "manual"):
        raise ValueError("origin")
    ns = data.get("base_mtime_ns")
    if ns is not None:
        ns = int(ns)
    dest = data.get("dest")
    if op == "rename":
        if not isinstance(dest, str):
            raise ValueError("dest")
        dest = safe_md_relpath(dest)
    else:
        dest = None
    digest = data.get("base_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        digest = None
    return Proposal(
        target=safe_md_relpath(str(data["target"])),
        op=op,
        body=str(data.get("body") or ""),
        origin=str(data["origin"]),
        base_mtime=data.get("base_mtime"),
        base_mtime_ns=ns,
        updated_at=str(data.get("updated_at") or ""),
        format_error=data.get("format_error"),
        dest=dest,
        base_digest=digest,
    )


def _write(proposal: Proposal, review_dir: Path | None) -> None:
    path = _proposal_path(proposal.target, review_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proposal.as_dict(), indent=2) + "\n", encoding="utf-8")


def _drop(target: str, review_dir: Path | None) -> None:
    path = _proposal_path(target, review_dir)
    if path.is_file():
        path.unlink()


def list_proposals(review_dir: Path | None = None) -> tuple[list[Proposal], int]:
    root = _root(review_dir)
    if not root.is_dir():
        return [], 0
    found: list[Proposal] = []
    unreadable = 0
    for path in sorted(root.rglob("*.json")):
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
            data = json.loads(resolved.read_text(encoding="utf-8"))
            found.append(_proposal_from(data))
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, UnsafeNameError):
            unreadable += 1
    found.sort(key=lambda item: item.target)
    return found, unreadable


def get_proposal(target: str, review_dir: Path | None = None) -> Proposal:
    path = _proposal_path(target, review_dir)
    rel = safe_md_relpath(target)
    if not path.is_file():
        raise FileNotFoundError(rel)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return _proposal_from(data)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ReviewError("That proposal could not be read.") from exc


def _base(path: Path) -> tuple[str | None, int | None, str | None]:
    iso, ns = file_mtime(path)
    if not path.is_file():
        return iso, ns, None
    return iso, ns, library.file_digest(path)


def _same_base(path: Path, proposal: Proposal) -> bool:
    """True when the live file is still the one this draft was staged from.

    A stored digest ignores a timestamp-only touch. Older proposals that
    have no digest keep the mtime check.
    """
    if not path.is_file():
        return proposal.base_mtime_ns is None and not proposal.base_digest
    if proposal.base_digest:
        try:
            return library.file_digest(path) == proposal.base_digest
        except OSError:
            return False
    _iso, ns = file_mtime(path)
    return ns == proposal.base_mtime_ns


def _formatted(text: str) -> tuple[str, str, str | None]:
    try:
        return format_md.format_markdown(text), "ai", None
    except LLMConfigError:
        return text, "manual", "No model is configured."
    except FormatError as exc:
        return text, "manual", str(exc)


def stage_upload(
    filename: str,
    data: bytes,
    docs_dir: Path | None = None,
    review_dir: Path | None = None,
) -> Proposal:
    try:
        name = safe_md_name(filename)
    except UnsafeNameError as exc:
        raise UploadError(str(exc)) from exc
    if not data:
        raise UploadError("Empty file.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadError("File larger than 1 MB.")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UploadError("File is not valid UTF-8.") from exc
    body, origin, format_error = _formatted(text)
    docs = docs_dir or library.DOCS_DIR
    path, rel = live_path(name, docs)
    iso, ns, digest = _base(path)
    proposal = Proposal(
        target=rel,
        op="upsert",
        body=body,
        origin=origin,
        base_mtime=iso,
        base_mtime_ns=ns,
        updated_at=_now(),
        format_error=format_error,
        base_digest=digest,
    )
    _write(proposal, review_dir)
    return proposal


def stage_delete(
    name: str,
    docs_dir: Path | None = None,
    review_dir: Path | None = None,
) -> Proposal:
    docs = docs_dir or library.DOCS_DIR
    path, rel = live_path(name, docs)
    if not path.is_file():
        raise FileNotFoundError(rel)
    iso, ns, digest = _base(path)
    proposal = Proposal(
        target=rel,
        op="delete",
        body="",
        origin="manual",
        base_mtime=iso,
        base_mtime_ns=ns,
        updated_at=_now(),
        format_error=None,
        base_digest=digest,
    )
    _write(proposal, review_dir)
    return proposal


def save_draft(
    name: str,
    body: str,
    docs_dir: Path | None = None,
    review_dir: Path | None = None,
) -> Proposal:
    if not isinstance(body, str):
        raise ReviewError("Expected a text body.")
    rel = safe_md_relpath(name)
    try:
        existing = get_proposal(rel, review_dir)
    except FileNotFoundError:
        existing = None
    if existing is not None and existing.op == "delete":
        raise ReviewError("A delete is pending for this file.")
    docs = docs_dir or library.DOCS_DIR
    path, _rel = live_path(rel, docs)
    iso, ns, digest = _base(path)
    proposal = Proposal(
        target=rel,
        op="upsert",
        body=body,
        origin="manual",
        base_mtime=iso,
        base_mtime_ns=ns,
        updated_at=_now(),
        format_error=None,
        base_digest=digest,
    )
    _write(proposal, review_dir)
    return proposal


def stage_rename(
    name: str,
    dest: str,
    docs_dir: Path | None = None,
    review_dir: Path | None = None,
) -> Proposal:
    """Stage a move. Approve writes the new path and removes the old one."""
    docs = docs_dir or library.DOCS_DIR
    path, rel = live_path(name, docs)
    if not path.is_file():
        raise FileNotFoundError(rel)
    dest_rel = safe_md_relpath(dest)
    if dest_rel == rel:
        raise ReviewError("Choose a different path.")
    dest_path, _dest = live_path(dest_rel, docs)
    if dest_path.exists():
        raise ReviewError("A file already exists at that path.")
    body = path.read_text(encoding="utf-8")
    if len(body.encode("utf-8")) > MAX_UPLOAD_BYTES:
        raise UploadError("File larger than 1 MB.")
    iso, ns, digest = _base(path)
    proposal = Proposal(
        target=rel,
        op="rename",
        body=body,
        origin="manual",
        base_mtime=iso,
        base_mtime_ns=ns,
        updated_at=_now(),
        format_error=None,
        dest=dest_rel,
        base_digest=digest,
    )
    _write(proposal, review_dir)
    return proposal


def format_proposal(name: str, review_dir: Path | None = None) -> Proposal:
    proposal = get_proposal(name, review_dir)
    if proposal.op != "upsert":
        raise ReviewError("Only a draft can be formatted.")
    proposal.body = format_md.format_markdown(proposal.body)
    proposal.origin = "ai"
    proposal.format_error = None
    proposal.updated_at = _now()
    _write(proposal, review_dir)
    return proposal


def approve(
    name: str,
    docs_dir: Path | None = None,
    review_dir: Path | None = None,
) -> None:
    proposal = get_proposal(name, review_dir)
    docs = docs_dir or library.DOCS_DIR
    path, rel = live_path(proposal.target, docs)
    if proposal.op == "rename":
        if not proposal.dest:
            raise ReviewError("Choose a destination.")
        dest_path, dest_rel = live_path(proposal.dest, docs)
        if dest_rel == rel:
            raise ReviewError("Choose a different path.")
        if not path.is_file():
            raise FileNotFoundError(rel)
        if not _same_base(path, proposal):
            raise ConflictError("The live file changed since this draft was staged.")
        if dest_path.exists():
            raise ReviewError("A file already exists at that path.")
        write_doc(dest_rel, proposal.body.encode("utf-8"), docs)
        delete_doc(rel, docs)
        _drop(proposal.target, review_dir)
        return
    if proposal.op == "delete":
        if not path.is_file():
            _drop(proposal.target, review_dir)
            return
        if not _same_base(path, proposal):
            raise ConflictError("The live file changed since this draft was staged.")
        delete_doc(rel, docs)
        _drop(proposal.target, review_dir)
        return
    if not proposal.body.strip():
        raise ReviewError("Empty draft.")
    if not _same_base(path, proposal):
        raise ConflictError("The live file changed since this draft was staged.")
    write_doc(rel, proposal.body.encode("utf-8"), docs)
    _drop(proposal.target, review_dir)


def reject(name: str, review_dir: Path | None = None) -> None:
    path = _proposal_path(name, review_dir)
    if not path.is_file():
        raise FileNotFoundError(safe_md_relpath(name))
    path.unlink()


if __name__ == "__main__":
    proposals, unreadable = list_proposals()
    print(f"{len(proposals)} proposals, {unreadable} unreadable")
    for item in proposals:
        err = f" ({item.format_error})" if item.format_error else ""
        print(f"{item.op:6} {item.origin:6} {item.target}{err}")

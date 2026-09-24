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
    if data.get("op") not in ("upsert", "delete"):
        raise ValueError("op")
    if data.get("origin") not in ("ai", "manual"):
        raise ValueError("origin")
    ns = data.get("base_mtime_ns")
    if ns is not None:
        ns = int(ns)
    return Proposal(
        target=safe_md_relpath(str(data["target"])),
        op=str(data["op"]),
        body=str(data.get("body") or ""),
        origin=str(data["origin"]),
        base_mtime=data.get("base_mtime"),
        base_mtime_ns=ns,
        updated_at=str(data.get("updated_at") or ""),
        format_error=data.get("format_error"),
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
    iso, ns = file_mtime(path)
    proposal = Proposal(
        target=rel,
        op="upsert",
        body=body,
        origin=origin,
        base_mtime=iso,
        base_mtime_ns=ns,
        updated_at=_now(),
        format_error=format_error,
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
    iso, ns = file_mtime(path)
    proposal = Proposal(
        target=rel,
        op="delete",
        body="",
        origin="manual",
        base_mtime=iso,
        base_mtime_ns=ns,
        updated_at=_now(),
        format_error=None,
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
    iso, ns = file_mtime(path)
    proposal = Proposal(
        target=rel,
        op="upsert",
        body=body,
        origin="manual",
        base_mtime=iso,
        base_mtime_ns=ns,
        updated_at=_now(),
        format_error=None,
    )
    _write(proposal, review_dir)
    return proposal


def format_proposal(name: str, review_dir: Path | None = None) -> Proposal:
    proposal = get_proposal(name, review_dir)
    if proposal.op == "delete":
        raise ReviewError("Delete proposals have no body to format.")
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
    _iso, ns = file_mtime(path)
    if proposal.op == "delete":
        if not path.is_file():
            _drop(proposal.target, review_dir)
            return
        if ns != proposal.base_mtime_ns:
            raise ConflictError("The live file changed since this draft was staged.")
        delete_doc(rel, docs)
        _drop(proposal.target, review_dir)
        return
    if not proposal.body.strip():
        raise ReviewError("Empty draft.")
    if ns != proposal.base_mtime_ns:
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

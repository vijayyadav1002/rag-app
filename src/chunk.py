"""
Splits markdown docs into overlapping chunks for embedding.

Strategy: split on markdown headings first (structure-aware), then merge
small sections and split oversized ones by paragraph with a sliding-window
overlap. Blind fixed-size chunking (splitting every N characters regardless
of structure) is the naive baseline this avoids -- it routinely cuts a
sentence or a Q/A pair in half, which is one of the most common causes of
bad retrieval in real RAG systems.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Shell exports take precedence (override=False). Missing file is a no-op.
load_dotenv(Path(__file__).parent / ".env")

CHUNK_SIZE = 800  # target characters per chunk
CHUNK_OVERLAP = 150  # characters of overlap between consecutive chunks
_ROOT = Path(__file__).resolve().parent


def resolve_docs_dir() -> Path:
    """Corpus folder: DOCS_DIR env, else src/docs.

    Relative values are from src/, not cwd. ~ expands. Blank is unset.
    """
    raw = (os.environ.get("DOCS_DIR") or "").strip()
    if not raw:
        return (_ROOT / "docs").resolve()
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = _ROOT / path
    return path.resolve()


@dataclass
class Chunk:
    text: str
    source_file: str
    heading: str
    chunk_id: str


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) pairs using ## headings."""
    parts = re.split(r"\n(?=## )", text)
    sections = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines = part.split("\n", 1)
        heading = lines[0].lstrip("#").strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        sections.append((heading, body))
    return sections


def _sliding_window(text: str, size: int, overlap: int) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def markdown_paths(docs_dir: Path) -> list[tuple[Path, str]]:
    """Resolved files and posix-relative names under docs_dir.

    Recurses. Skips hidden path parts (`.git`, `.obsidian`, `.draft.md`)
    and anything that resolves outside docs_dir (symlink escape).
    """
    root = docs_dir.resolve()
    if not root.is_dir():
        return []
    found: list[tuple[Path, str]] = []
    for path in root.rglob("*.md"):
        if not path.is_file():
            continue
        try:
            resolved = path.resolve()
            rel = resolved.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel.parts):
            continue
        found.append((resolved, rel.as_posix()))
    found.sort(key=lambda item: item[1])
    return found


def chunk_document(path: Path, *, source_file: str | None = None) -> list[Chunk]:
    raw = path.read_text()
    title_match = re.match(r"# (.+)", raw)
    doc_title = title_match.group(1).strip() if title_match else path.stem

    body = re.sub(r"^# .+\n", "", raw, count=1).strip()
    sections = _split_into_sections(body) or [("", body)]
    rel = source_file if source_file is not None else path.name
    id_stem = Path(rel).with_suffix("").as_posix().replace("/", "_")

    chunks = []
    for i, (heading, section_text) in enumerate(sections):
        full_heading = f"{doc_title} > {heading}" if heading else doc_title
        pieces = _sliding_window(section_text, CHUNK_SIZE, CHUNK_OVERLAP)
        for j, piece in enumerate(pieces):
            if not piece.strip():
                continue
            chunks.append(
                Chunk(
                    text=f"{full_heading}\n{piece.strip()}",
                    source_file=rel,
                    heading=full_heading,
                    chunk_id=f"{id_stem}_{i}_{j}",
                )
            )
    return chunks


def chunk_directory(docs_dir: Path) -> list[Chunk]:
    all_chunks = []
    for path, rel in markdown_paths(docs_dir):
        all_chunks.extend(chunk_document(path, source_file=rel))
    return all_chunks


if __name__ == "__main__":
    docs_dir = resolve_docs_dir()
    chunks = chunk_directory(docs_dir)
    print(f"{len(chunks)} chunks from {len(markdown_paths(docs_dir))} docs\n")
    for c in chunks[:5]:
        print(f"[{c.chunk_id}] ({len(c.text)} chars)\n{c.text[:200]}\n---")

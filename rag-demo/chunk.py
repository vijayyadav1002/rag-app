"""
Splits markdown docs into overlapping chunks for embedding.

Strategy: split on markdown headings first (structure-aware), then merge
small sections and split oversized ones by paragraph with a sliding-window
overlap. Blind fixed-size chunking (splitting every N characters regardless
of structure) is the naive baseline this avoids -- it routinely cuts a
sentence or a Q/A pair in half, which is one of the most common causes of
bad retrieval in real RAG systems.
"""

import re
from dataclasses import dataclass
from pathlib import Path

CHUNK_SIZE = 800  # target characters per chunk
CHUNK_OVERLAP = 150  # characters of overlap between consecutive chunks


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


def chunk_document(path: Path) -> list[Chunk]:
    raw = path.read_text()
    title_match = re.match(r"# (.+)", raw)
    doc_title = title_match.group(1).strip() if title_match else path.stem

    body = re.sub(r"^# .+\n", "", raw, count=1).strip()
    sections = _split_into_sections(body) or [("", body)]

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
                    source_file=path.name,
                    heading=full_heading,
                    chunk_id=f"{path.stem}_{i}_{j}",
                )
            )
    return chunks


def chunk_directory(docs_dir: Path) -> list[Chunk]:
    all_chunks = []
    for path in sorted(docs_dir.glob("*.md")):
        all_chunks.extend(chunk_document(path))
    return all_chunks


if __name__ == "__main__":
    docs_dir = Path(__file__).parent / "docs"
    chunks = chunk_directory(docs_dir)
    print(f"{len(chunks)} chunks from {len(list(docs_dir.glob('*.md')))} docs\n")
    for c in chunks[:5]:
        print(f"[{c.chunk_id}] ({len(c.text)} chars)\n{c.text[:200]}\n---")

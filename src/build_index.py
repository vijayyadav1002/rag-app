"""
Embeds chunks and builds a FAISS index, persisted to disk.

Run this once (and again any time the corpus changes) before querying.
Writes into index/.tmp and replaces the live artifacts only after a
full successful encode — a crash mid-write must not truncate chunks.faiss.

A one-file edit does not re-embed the rest of the corpus: those rows are
copied. The same temp replace is used, and a pickle that disagrees with
the index is rebuilt from scratch instead of spliced. A kept file with no
digest is hashed before publish. The new indexed_at would otherwise make
list_docs treat that copied row as fresh, because mtime is the fallback
only when a digest is missing.
"""
import json
import pickle
import shutil
from datetime import datetime, timezone
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

from chunk import resolve_docs_dir
from library import file_digest
from media import (
    MediaPathError,
    PdfExtractError,
    collect_corpus,
    collect_named,
    indexable_relpath,
)

EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
DOCUMENT_PREFIX = "search_document: "
INDEX_DIR = Path(__file__).parent / "index"


class IndexBuildError(Exception):
    pass


def _default_encode(texts: list[str]) -> np.ndarray:
    """Prefix and embed the way a full build does."""
    model = SentenceTransformer(EMBEDDING_MODEL)
    prefixed = [DOCUMENT_PREFIX + text for text in texts]
    embeddings = model.encode(prefixed, show_progress_bar=True, normalize_embeddings=True)
    return np.asarray(embeddings, dtype="float32")


def _publish(index_dir: Path, index, chunks, config) -> None:
    index_dir.mkdir(exist_ok=True)
    tmp = index_dir / ".tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    tmp.mkdir()
    try:
        faiss.write_index(index, str(tmp / "chunks.faiss"))
        with open(tmp / "metadata.pkl", "wb") as f:
            pickle.dump(chunks, f)
        with open(tmp / "config.json", "w") as f:
            json.dump(config, f)
        for name in ("chunks.faiss", "metadata.pkl", "config.json"):
            (tmp / name).replace(index_dir / name)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


def _plan(docs_dir: Path, named: tuple[str, ...] | None):
    try:
        if named is None:
            return collect_corpus(docs_dir)
        return collect_named(docs_dir, named)
    except PdfExtractError as exc:
        raise IndexBuildError(f"{exc} The index was not replaced.") from exc
    except MediaPathError as exc:
        raise IndexBuildError(f"{exc} The index was not replaced.") from exc


def build(docs_dir: Path | None = None, index_dir: Path | None = None) -> dict:
    docs_dir = resolve_docs_dir() if docs_dir is None else docs_dir
    index_dir = INDEX_DIR if index_dir is None else index_dir
    corpus = _plan(docs_dir, None)
    chunks = list(corpus.chunks)
    if not chunks:
        raise IndexBuildError("No chunks to index.")

    embeddings = _default_encode([c.text for c in chunks])

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    files = list(corpus.files)
    digests = {
        rel: file_digest(docs_dir / rel)
        for rel in files
        if (docs_dir / rel).is_file()
    }
    config = {
        "embedding_model": EMBEDDING_MODEL,
        "num_chunks": len(chunks),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
        "digests": digests,
        "warnings": {rel: message for rel, message in corpus.warnings},
    }

    _publish(index_dir, index, chunks, config)

    print(f"Index built: {len(chunks)} vectors, dim={embeddings.shape[1]}")
    print(f"Saved to {index_dir}/")
    return config


def rebuild_paths(
    paths: list[str],
    docs_dir: Path | None = None,
    index_dir: Path | None = None,
    encode=None,
) -> dict:
    try:
        rels = sorted({indexable_relpath(raw) for raw in paths})
    except MediaPathError as exc:
        raise IndexBuildError(str(exc)) from exc
    if not rels:
        raise IndexBuildError("No files to re-index.")
    docs_dir = resolve_docs_dir() if docs_dir is None else docs_dir
    index_dir = INDEX_DIR if index_dir is None else index_dir
    faiss_path = index_dir / "chunks.faiss"
    if not faiss_path.is_file():
        return build(docs_dir, index_dir)

    index = faiss.read_index(str(faiss_path))
    with open(index_dir / "metadata.pkl", "rb") as f:
        chunks = pickle.load(f)
    with open(index_dir / "config.json") as f:
        config = json.load(f)
    # A non-list pickle, or a length that disagrees with ntotal, is not spliced.
    if not isinstance(chunks, list) or index.ntotal != len(chunks):
        return build(docs_dir, index_dir)
    if not isinstance(config, dict):
        return build(docs_dir, index_dir)
    # Same sentence as retrieve.reload. Do not splice a foreign model.
    if "embedding_model" in config and config["embedding_model"] != EMBEDDING_MODEL:
        indexed_model = config["embedding_model"]
        raise IndexBuildError(
            f"Index was built with {indexed_model!r}, but retrieve.py "
            f"uses {EMBEDDING_MODEL!r}. Re-run build_index.py."
        )

    requested = set(rels)
    kept_chunks = []
    kept_rows = []
    for i, chunk in enumerate(chunks):
        if chunk.source_file in requested:
            continue
        kept_chunks.append(chunk)
        kept_rows.append(np.asarray(index.reconstruct(i), dtype="float32"))

    named = _plan(docs_dir, tuple(rels))
    new_chunks = list(named.chunks)

    if not kept_chunks and not new_chunks:
        raise IndexBuildError("No chunks to index.")

    new_vectors = None
    if new_chunks:
        # Prefix lives in _default_encode so an injected encode sees this list.
        texts = [c.text for c in new_chunks]
        new_vectors = np.asarray((encode or _default_encode)(texts), dtype="float32")
        if (
            new_vectors.ndim != 2
            or new_vectors.shape[0] != len(new_chunks)
            or new_vectors.shape[1] != index.d
        ):
            raise IndexBuildError(
                f"Encoder returned shape {tuple(new_vectors.shape)}, "
                f"but the index has dim {index.d} and {len(new_chunks)} new chunks."
            )

    parts = []
    all_chunks = []
    if new_vectors is not None:
        parts.append(new_vectors)
        all_chunks.extend(new_chunks)
    if kept_rows:
        parts.append(np.vstack(kept_rows))
        all_chunks.extend(kept_chunks)
    matrix = parts[0] if len(parts) == 1 else np.vstack(parts)
    matrix = np.ascontiguousarray(matrix, dtype="float32")
    fresh = faiss.IndexFlatIP(matrix.shape[1])
    fresh.add(matrix)

    previous = config.get("digests")
    digests = dict(previous) if isinstance(previous, dict) else {}
    for rel in rels:
        digests.pop(rel, None)
        path = docs_dir / rel
        if path.is_file():
            digests[rel] = file_digest(path)
    # Copied rows are not re-embedded. A sibling with no digest still needs
    # one: indexed_at moves, and list_docs uses mtime only when it is absent.
    for rel in {c.source_file for c in kept_chunks}:
        if not isinstance(rel, str):
            continue
        recorded = digests.get(rel)
        if isinstance(recorded, str) and len(recorded) == 64:
            continue
        path = docs_dir / rel
        if path.is_file():
            digests[rel] = file_digest(path)
    previous_files = config.get("files")
    published_files = set()
    if isinstance(previous_files, list):
        for item in previous_files:
            if isinstance(item, str) and item not in requested:
                published_files.add(item)
    for chunk in all_chunks:
        if isinstance(chunk.source_file, str):
            published_files.add(chunk.source_file)
    published_files.update(named.files)
    previous_warnings = config.get("warnings")
    warnings = dict(previous_warnings) if isinstance(previous_warnings, dict) else {}
    for rel in rels:
        warnings.pop(rel, None)
    for rel, message in named.warnings:
        warnings[rel] = message
    warnings = {
        rel: message
        for rel, message in warnings.items()
        if isinstance(rel, str) and isinstance(message, str) and rel in published_files
    }
    published = {
        "embedding_model": EMBEDDING_MODEL,
        "num_chunks": len(all_chunks),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "files": sorted(published_files),
        "digests": digests,
        "warnings": warnings,
    }
    _publish(index_dir, fresh, all_chunks, published)
    return published


if __name__ == "__main__":
    build()

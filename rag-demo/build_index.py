"""
Embeds all chunks and builds a FAISS index, persisted to disk.

Run this once (and again any time docs/ changes) before querying.
Writes into index/.tmp and replaces the live artifacts only after a
full successful encode — a crash mid-write must not truncate chunks.faiss.
"""
import json
import pickle
import shutil
from datetime import datetime, timezone
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

from chunk import chunk_directory

EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
DOCUMENT_PREFIX = "search_document: "
INDEX_DIR = Path(__file__).parent / "index"


class IndexBuildError(Exception):
    pass


def build() -> dict:
    docs_dir = Path(__file__).parent / "docs"
    chunks = chunk_directory(docs_dir)
    if not chunks:
        raise IndexBuildError("No chunks to index.")

    model = SentenceTransformer(EMBEDDING_MODEL)
    texts = [DOCUMENT_PREFIX + c.text for c in chunks]
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)
    embeddings = np.asarray(embeddings, dtype="float32")

    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    files = sorted({c.source_file for c in chunks})
    config = {
        "embedding_model": EMBEDDING_MODEL,
        "num_chunks": len(chunks),
        "indexed_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }

    INDEX_DIR.mkdir(exist_ok=True)
    tmp = INDEX_DIR / ".tmp"
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
            (tmp / name).replace(INDEX_DIR / name)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)

    print(f"Index built: {len(chunks)} vectors, dim={embeddings.shape[1]}")
    print(f"Saved to {INDEX_DIR}/")
    return config


if __name__ == "__main__":
    build()

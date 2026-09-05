"""
Embeds all chunks and builds a FAISS index, persisted to disk.

Run this once (and again any time docs/ changes) before querying.
"""

import json
import pickle
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import SentenceTransformer

from chunk import chunk_directory

EMBEDDING_MODEL = "all-MiniLM-L6-v2"  # small, fast, good enough for a demo
INDEX_DIR = Path(__file__).parent / "index"


def build():
    docs_dir = Path(__file__).parent / "docs"
    chunks = chunk_directory(docs_dir)
    print(f"Chunked {len(chunks)} chunks from docs/")

    model = SentenceTransformer(EMBEDDING_MODEL)
    texts = [c.text for c in chunks]
    embeddings = model.encode(texts, show_progress_bar=True, normalize_embeddings=True)
    embeddings = np.asarray(embeddings, dtype="float32")

    # Cosine similarity via inner product on normalized vectors.
    index = faiss.IndexFlatIP(embeddings.shape[1])
    index.add(embeddings)

    INDEX_DIR.mkdir(exist_ok=True)
    faiss.write_index(index, str(INDEX_DIR / "chunks.faiss"))
    with open(INDEX_DIR / "metadata.pkl", "wb") as f:
        pickle.dump(chunks, f)
    with open(INDEX_DIR / "config.json", "w") as f:
        json.dump({"embedding_model": EMBEDDING_MODEL, "num_chunks": len(chunks)}, f)

    print(f"Index built: {len(chunks)} vectors, dim={embeddings.shape[1]}")
    print(f"Saved to {INDEX_DIR}/")


if __name__ == "__main__":
    build()

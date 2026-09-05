"""
Retrieval: embed the query, vector search for candidates, then rerank with
a cross-encoder before handing chunks to the LLM.

Why rerank at all: the embedding model that indexes documents is optimized
for speed over many vectors (a "bi-encoder" -- query and document are
embedded independently, then compared by dot product). A cross-encoder
looks at the query and each candidate document *together* in one forward
pass, so it captures interactions a bi-encoder misses. It's too slow to run
over the whole corpus, but running it over the top ~20 vector-search
candidates to pick the true top-k is cheap and consistently improves
precision. This two-stage retrieve-then-rerank pattern is the standard
accuracy lever in production RAG systems.
"""

import pickle
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import CrossEncoder, SentenceTransformer

INDEX_DIR = Path(__file__).parent / "index"
EMBEDDING_MODEL = "all-MiniLM-L6-v2"
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_embedder = None
_reranker = None
_index = None
_chunks = None


def _load():
    global _embedder, _reranker, _index, _chunks
    if _index is not None:
        return
    _embedder = SentenceTransformer(EMBEDDING_MODEL)
    _reranker = CrossEncoder(RERANKER_MODEL)
    _index = faiss.read_index(str(INDEX_DIR / "chunks.faiss"))
    with open(INDEX_DIR / "metadata.pkl", "rb") as f:
        _chunks = pickle.load(f)


@dataclass
class RetrievedChunk:
    chunk_id: str
    source_file: str
    text: str
    vector_score: float
    rerank_score: float | None = None


def vector_search(query: str, top_k: int = 20) -> list[RetrievedChunk]:
    """Stage 1: fast approximate candidate retrieval over the whole corpus."""
    _load()
    q_emb = _embedder.encode([query], normalize_embeddings=True)
    q_emb = np.asarray(q_emb, dtype="float32")
    scores, indices = _index.search(q_emb, top_k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        c = _chunks[idx]
        results.append(
            RetrievedChunk(
                chunk_id=c.chunk_id,
                source_file=c.source_file,
                text=c.text,
                vector_score=float(score),
            )
        )
    return results


def rerank(query: str, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Stage 2: precise reordering of a small candidate set."""
    _load()
    pairs = [[query, c.text] for c in candidates]
    scores = _reranker.predict(pairs)
    for c, score in zip(candidates, scores):
        c.rerank_score = float(score)
    return sorted(candidates, key=lambda c: c.rerank_score, reverse=True)


def retrieve(query: str, top_k: int = 4, candidate_pool: int = 20, use_reranker: bool = True) -> list[RetrievedChunk]:
    candidates = vector_search(query, top_k=candidate_pool)
    if use_reranker:
        candidates = rerank(query, candidates)
    return candidates[:top_k]


if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) or "How many days of PTO do I get per year?"
    print(f"Query: {query!r}\n")

    print("--- Vector search only (top 4) ---")
    for c in vector_search(query, top_k=4):
        print(f"[{c.vector_score:.3f}] {c.source_file} :: {c.chunk_id}")
        print(f"  {c.text[:120]}...")

    print("\n--- After rerank (top 4) ---")
    for c in retrieve(query, top_k=4):
        print(f"[rerank {c.rerank_score:.3f} | vec {c.vector_score:.3f}] {c.source_file} :: {c.chunk_id}")
        print(f"  {c.text[:120]}...")

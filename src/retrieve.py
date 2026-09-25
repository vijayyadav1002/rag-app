"""
Retrieval: embed the query, vector search for candidates, then rerank with
a cross-encoder before handing chunks to the LLM.

Why rerank at all: the embedding model that indexes documents is optimized
for speed over many vectors (a "bi-encoder" -- query and document are
embedded independently, then compared by dot product). A cross-encoder
looks at the query and each candidate document *together* in one forward
pass, so it captures interactions a bi-encoder misses. It's too slow to run
over the whole corpus, but running it over the top ~20 vector-search
candidates to pick the true top-k is cheap and, when the model is actually
confident, a real second opinion.

Why the confidence gate: this MiniLM reranker emits an MS MARCO logit.
sigmoid(0) is 0.5, so a best score below 0 means the model thinks the
closest passage is probably not relevant. On this corpus that unconfident
order is what swaps the laptop-return policy for the disk-encryption
policy. Shipped retrieval keeps vector order in that case and still
attaches the scores. It does not drop the neighbors.
"""

import json
import pickle
import threading
from dataclasses import dataclass
from pathlib import Path

import faiss
import numpy as np
from sentence_transformers import CrossEncoder, SentenceTransformer

INDEX_DIR = Path(__file__).parent / "index"
EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
QUERY_PREFIX = "search_query: "
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_embedder = None
_reranker = None
_index = None
_chunks = None
_state_lock = threading.Lock()


def reload() -> None:
    """Re-read FAISS + chunk metadata from disk. Keep models if already loaded."""
    global _embedder, _reranker, _index, _chunks
    config_path = INDEX_DIR / "config.json"
    if config_path.exists():
        with open(config_path) as f:
            cfg = json.load(f)
        indexed_model = cfg.get("embedding_model")
        if indexed_model and indexed_model != EMBEDDING_MODEL:
            raise RuntimeError(
                f"Index was built with {indexed_model!r}, but retrieve.py "
                f"uses {EMBEDDING_MODEL!r}. Re-run build_index.py."
            )
    if _embedder is None:
        _embedder = SentenceTransformer(EMBEDDING_MODEL)
        _reranker = CrossEncoder(RERANKER_MODEL)
    new_index = faiss.read_index(str(INDEX_DIR / "chunks.faiss"))
    with open(INDEX_DIR / "metadata.pkl", "rb") as f:
        new_chunks = pickle.load(f)
    with _state_lock:
        _index = new_index
        _chunks = new_chunks


def _load():
    if _index is not None:
        return
    reload()


RERANK_FLOOR = 0.0
RERANK_MARGIN = 1.0
# MS MARCO logit. Below this, P(relevant) < 0.5, so rerank order is not trusted.
RERANK_TRUST = 0.0


@dataclass
class RetrievedChunk:
    chunk_id: str
    source_file: str
    text: str
    vector_score: float
    rerank_score: float | None = None
    heading: str = ""
    section_text: str = ""
    expanded: bool = False
    rank_source: str = "vector"


def vector_search(query: str, top_k: int = 20) -> list[RetrievedChunk]:
    """Stage 1: fast approximate candidate retrieval over the whole corpus."""
    _load()
    q_emb = _embedder.encode(
        [QUERY_PREFIX + query], normalize_embeddings=True
    )
    q_emb = np.asarray(q_emb, dtype="float32")
    with _state_lock:
        index = _index
        chunks = _chunks
    scores, indices = index.search(q_emb, top_k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        c = chunks[idx]
        results.append(
            RetrievedChunk(
                chunk_id=c.chunk_id,
                source_file=c.source_file,
                text=c.text,
                vector_score=float(score),
                heading=getattr(c, "heading", "") or "",
                section_text=getattr(c, "section_text", "") or "",
            )
        )
    return results


def rerank(query: str, candidates: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Stage 2: precise reordering of a small candidate set."""
    _load()
    pairs = [[query, c.text] for c in candidates]
    with _state_lock:
        reranker = _reranker
    scores = reranker.predict(pairs)
    for c, score in zip(candidates, scores):
        c.rerank_score = float(score)
    return sorted(candidates, key=lambda c: c.rerank_score, reverse=True)


def retrieve(
    query: str,
    top_k: int = 4,
    candidate_pool: int = 20,
    use_reranker: bool = True,
    trust_gate: bool = True,
) -> list[RetrievedChunk]:
    """Vector top-`candidate_pool`, optional rerank, then the trust gate.

    `trust_gate=False` returns pure rerank order so eval can still show the
    laptop-policy flip. The default restores vector order when the best
    logit is below `RERANK_TRUST`.
    """
    candidates = vector_search(query, top_k=candidate_pool)
    if not use_reranker:
        return candidates[:top_k]
    ranked = rerank(query, candidates)
    best = ranked[0].rerank_score if ranked else None
    trusted = best is not None and best >= RERANK_TRUST
    if trust_gate and not trusted:
        ranked = sorted(ranked, key=lambda c: c.vector_score, reverse=True)
        source = "vector"
    else:
        source = "rerank"
    for chunk in ranked:
        chunk.rank_source = source
    return ranked[:top_k]


def prompt_excerpts(chunks: list[RetrievedChunk]) -> list[RetrievedChunk]:
    """Swap in section text for hits that sit with the top rerank score.

    Ranking already happened on the 800-character window. This only changes
    what the prompt is allowed to read. Vector-only results have no rerank
    score and stay as windows, so eval recall is unchanged.
    """
    scored = [c.rerank_score for c in chunks if c.rerank_score is not None]
    if not chunks or not scored:
        return list(chunks)
    top = float(max(scored))
    out: list[RetrievedChunk] = []
    slot: dict[tuple[str, str], int] = {}
    for chunk in chunks:
        key = (chunk.source_file, chunk.heading or chunk.chunk_id)
        qualifies = (
            chunk.rerank_score is not None
            and chunk.rerank_score >= RERANK_FLOOR
            and chunk.rerank_score >= top - RERANK_MARGIN
            and bool(chunk.section_text)
        )
        if qualifies:
            excerpt = RetrievedChunk(
                chunk_id=chunk.chunk_id,
                source_file=chunk.source_file,
                text=chunk.section_text,
                vector_score=chunk.vector_score,
                rerank_score=chunk.rerank_score,
                heading=chunk.heading,
                section_text=chunk.section_text,
                expanded=True,
                rank_source=chunk.rank_source,
            )
            if key in slot:
                if out[slot[key]].expanded:
                    continue
                out[slot[key]] = excerpt
                continue
            slot[key] = len(out)
            out.append(excerpt)
            continue
        if key in slot and out[slot[key]].expanded:
            continue
        if key not in slot:
            slot[key] = len(out)
        out.append(chunk)
    return out


if __name__ == "__main__":
    import sys

    query = " ".join(sys.argv[1:]) or "How many days of PTO do I get per year?"
    print(f"Query: {query!r}\n")

    print("--- Vector search only (top 4) ---")
    for c in vector_search(query, top_k=4):
        print(f"[{c.vector_score:.3f}] {c.source_file} :: {c.chunk_id}")
        print(f"  {c.text[:120]}...")

    print("\n--- Rerank order, gate off (top 4) ---")
    ungated = retrieve(query, top_k=4, trust_gate=False)
    for c in ungated:
        print(f"[rerank {c.rerank_score:.3f} | vec {c.vector_score:.3f}] {c.source_file} :: {c.chunk_id}")
        print(f"  {c.text[:120]}...")

    print("\n--- Shipped retrieve (top 4) ---")
    ranked = retrieve(query, top_k=4)
    how = ranked[0].rank_source if ranked else "vector"
    print(f"Order: {how}")
    for c in ranked:
        print(f"[rerank {c.rerank_score:.3f} | vec {c.vector_score:.3f}] {c.source_file} :: {c.chunk_id}")
        print(f"  {c.text[:120]}...")

    print("\n--- Prompt excerpts ---")
    for c in prompt_excerpts(ranked):
        kind = "section" if c.expanded else "excerpt"
        print(f"[{kind} {len(c.text)} chars] {c.source_file} :: {c.chunk_id}")

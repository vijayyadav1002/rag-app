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

from lexical import LexicalIndex

INDEX_DIR = Path(__file__).parent / "index"
EMBEDDING_MODEL = "nomic-ai/nomic-embed-text-v1.5"
QUERY_PREFIX = "search_query: "
RERANKER_MODEL = "cross-encoder/ms-marco-MiniLM-L-6-v2"

_embedder = None
_reranker = None
_index = None
_chunks = None
_lexical = None
_state_lock = threading.Lock()


def reload() -> None:
    """Re-read FAISS + chunk metadata from disk. Keep models if already loaded."""
    global _embedder, _reranker, _index, _chunks, _lexical
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
    new_lexical = LexicalIndex(new_chunks)
    with _state_lock:
        _index = new_index
        _chunks = new_chunks
        _lexical = new_lexical


def _load():
    if _index is not None:
        return
    reload()


RERANK_FLOOR = 0.0
RERANK_MARGIN = 1.0
# MS MARCO logit. Below this, P(relevant) < 0.5, so rerank order is not trusted.
RERANK_TRUST = 0.0
# Reciprocal rank fusion constant. Rank 1 contributes 1/61 from each list.
RRF_K = 60


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
    lexical_score: float | None = None


def in_scope(source_file: str, scope: str | None) -> bool:
    """A file scope is an exact path. A folder scope is that prefix only."""
    if not scope:
        return True
    if scope.lower().endswith(".md"):
        return source_file == scope
    prefix = scope.rstrip("/")
    return source_file == prefix or source_file.startswith(prefix + "/")


def _hit(chunk, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk.chunk_id,
        source_file=chunk.source_file,
        text=chunk.text,
        vector_score=float(score),
        heading=getattr(chunk, "heading", "") or "",
        section_text=getattr(chunk, "section_text", "") or "",
    )


def vector_search(query: str, top_k: int = 20, scope: str | None = None) -> list[RetrievedChunk]:
    """Stage 1: nearest chunks. `scope` limits the scan to one folder or file.

    The unscoped path is the one eval measures. A scope searches only the
    matching vectors, so a small folder is not dropped for ranking outside it.
    """
    _load()
    q_emb = _embedder.encode(
        [QUERY_PREFIX + query], normalize_embeddings=True
    )
    q_emb = np.asarray(q_emb, dtype="float32")
    with _state_lock:
        index = _index
        chunks = _chunks
    if scope:
        allowed = [
            i
            for i, chunk in enumerate(chunks)
            if in_scope(getattr(chunk, "source_file", ""), scope)
        ]
        if not allowed:
            return []
        vecs = []
        ids = []
        for i in allowed:
            try:
                vecs.append(index.reconstruct(int(i)))
            except RuntimeError:
                continue
            ids.append(i)
        if not ids:
            return []
        matrix = np.vstack(vecs).astype("float32")
        sims = matrix @ q_emb[0]
        order = np.argsort(-sims)[:top_k]
        return [_hit(chunks[ids[int(pos)]], float(sims[int(pos)])) for pos in order]
    scores, indices = index.search(q_emb, top_k)
    results = []
    for score, idx in zip(scores[0], indices[0]):
        if idx == -1:
            continue
        results.append(_hit(chunks[idx], float(score)))
    return results


def lexical_search(query: str, top_k: int = 20, scope: str | None = None) -> list[RetrievedChunk]:
    """BM25 hits. `vector_score` is the cosine so the trust gate can sort this pool too."""
    _load()
    q_emb = _embedder.encode([QUERY_PREFIX + query], normalize_embeddings=True)
    q_emb = np.asarray(q_emb, dtype="float32")
    with _state_lock:
        lexical = _lexical
        chunks = _chunks
        index = _index
        if lexical is None or not chunks or index is None:
            return []
        allowed = None
        if scope:
            allowed = {
                i
                for i, chunk in enumerate(chunks)
                if in_scope(getattr(chunk, "source_file", ""), scope)
            }
            if not allowed:
                return []
        hits = lexical.search(query, top_k, allowed)
        rows = []
        for idx, score in hits:
            vec = float(np.dot(index.reconstruct(int(idx)), q_emb[0]))
            chunk = chunks[idx]
            rows.append(
                RetrievedChunk(
                    chunk_id=chunk.chunk_id,
                    source_file=chunk.source_file,
                    text=chunk.text,
                    vector_score=vec,
                    heading=getattr(chunk, "heading", "") or "",
                    section_text=getattr(chunk, "section_text", "") or "",
                    lexical_score=float(score),
                )
            )
        return rows


def fuse_rrf(
    vector: list[RetrievedChunk],
    lexical: list[RetrievedChunk],
    limit: int,
    k: int = RRF_K,
) -> list[RetrievedChunk]:
    """Union by chunk id. Reciprocal rank sums; a tie keeps the higher vector score."""
    by_id: dict[str, RetrievedChunk] = {}
    for chunk in vector:
        by_id[chunk.chunk_id] = chunk
    for chunk in lexical:
        existing = by_id.get(chunk.chunk_id)
        if existing is None:
            by_id[chunk.chunk_id] = chunk
        else:
            existing.lexical_score = chunk.lexical_score
    rank_v = {chunk.chunk_id: i + 1 for i, chunk in enumerate(vector)}
    rank_l = {chunk.chunk_id: i + 1 for i, chunk in enumerate(lexical)}
    scored = []
    for cid, chunk in by_id.items():
        score = 0.0
        if cid in rank_v:
            score += 1.0 / (k + rank_v[cid])
        if cid in rank_l:
            score += 1.0 / (k + rank_l[cid])
        scored.append((score, chunk.vector_score, chunk))
    scored.sort(key=lambda item: (-item[0], -item[1]))
    return [item[2] for item in scored[:limit]]


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


def _brief(chunks: list[RetrievedChunk], limit: int = 5) -> list[dict]:
    rows = []
    for chunk in chunks[:limit]:
        rows.append(
            {
                "source_file": chunk.source_file,
                "heading": chunk.heading,
                "vector_score": float(chunk.vector_score),
                "rerank_score": None if chunk.rerank_score is None else float(chunk.rerank_score),
                "lexical_score": None
                if getattr(chunk, "lexical_score", None) is None
                else float(chunk.lexical_score),
            }
        )
    return rows


def _empty_trace(source: str = "vector") -> dict:
    return {
        "best_logit": None,
        "trusted": False,
        "rank_source": source,
        "vector": [],
        "rerank": [],
        "lexical": [],
    }


def retrieve_explained(
    query: str,
    top_k: int = 4,
    candidate_pool: int = 20,
    use_reranker: bool = True,
    trust_gate: bool = True,
    scope: str | None = None,
    hybrid: bool = False,
) -> tuple[list[RetrievedChunk], dict]:
    """Same selection as retrieve(), plus a score trace that holds no chunk text.

    `hybrid` False keeps the vector candidate pool, which is the pool eval
    measures. True fuses BM25 hits into that pool before rerank. Ask does
    not pass True yet.
    """
    vector_hits = vector_search(query, top_k=candidate_pool, scope=scope)
    lexical_hits = lexical_search(query, top_k=candidate_pool, scope=scope) if hybrid else []
    if not use_reranker:
        chosen = vector_hits[:top_k]
        trace = _empty_trace("vector")
        trace["vector"] = _brief(vector_hits)
        trace["lexical"] = _brief(lexical_hits) if hybrid else []
        return chosen, trace
    pool = fuse_rrf(vector_hits, lexical_hits, candidate_pool) if lexical_hits else list(vector_hits)
    ranked = rerank(query, list(pool))
    best = ranked[0].rerank_score if ranked else None
    trusted = best is not None and best >= RERANK_TRUST
    if trust_gate and not trusted:
        shipped = sorted(ranked, key=lambda c: c.vector_score, reverse=True)
        source = "vector"
    else:
        shipped = ranked
        source = "rerank"
    for chunk in shipped:
        chunk.rank_source = source
    trace = {
        "best_logit": None if best is None else float(best),
        "trusted": trusted,
        "rank_source": source,
        "vector": _brief(vector_hits),
        "rerank": _brief(ranked),
        "lexical": _brief(lexical_hits) if hybrid else [],
    }
    return shipped[:top_k], trace


def retrieve(
    query: str,
    top_k: int = 4,
    candidate_pool: int = 20,
    use_reranker: bool = True,
    trust_gate: bool = True,
    scope: str | None = None,
    hybrid: bool = False,
) -> list[RetrievedChunk]:
    chunks, _trace = retrieve_explained(
        query,
        top_k=top_k,
        candidate_pool=candidate_pool,
        use_reranker=use_reranker,
        trust_gate=trust_gate,
        scope=scope,
        hybrid=hybrid,
    )
    return chunks


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
                lexical_score=chunk.lexical_score,
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

    print("\n--- Hybrid (top 4) ---")
    fused = retrieve(query, top_k=4, hybrid=True)
    fused_how = fused[0].rank_source if fused else "vector"
    print(f"Order: {fused_how}")
    for c in fused:
        print(f"[rerank {c.rerank_score:.3f} | vec {c.vector_score:.3f}] {c.source_file} :: {c.chunk_id}")
        print(f"  {c.text[:120]}...")

    print("\n--- Prompt excerpts ---")
    for c in prompt_excerpts(ranked):
        kind = "section" if c.expanded else "excerpt"
        print(f"[{kind} {len(c.text)} chars] {c.source_file} :: {c.chunk_id}")

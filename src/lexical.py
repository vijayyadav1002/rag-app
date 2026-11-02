"""Okapi BM25 over the chunk texts already stored in the FAISS metadata.

Dense search misses exact tokens — an error code, a note title, a product
name — that a count of the same tokens catches. This module only scores.
retrieve.py decides whether those hits enter the rerank pool.
"""
from __future__ import annotations

import math
import re

K1 = 1.5
B = 0.75
_TOKEN = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


class LexicalIndex:
    def __init__(self, chunks: list):
        self.tf: list[dict[str, int]] = []
        self.length: list[int] = []
        self.df: dict[str, int] = {}
        for chunk in chunks:
            counts: dict[str, int] = {}
            tokens = tokenize(getattr(chunk, "text", "") or "")
            for token in tokens:
                counts[token] = counts.get(token, 0) + 1
            self.tf.append(counts)
            self.length.append(len(tokens))
            for token in counts:
                self.df[token] = self.df.get(token, 0) + 1
        self.n = len(chunks)
        self.avgdl = (sum(self.length) / self.n) if self.n else 0.0

    def search(self, query: str, top_k: int, allowed: set[int] | None) -> list[tuple[int, float]]:
        terms = list(dict.fromkeys(tokenize(query)))
        if not terms or not self.n or self.avgdl <= 0 or top_k <= 0:
            return []
        scored: list[tuple[float, int]] = []
        for i, counts in enumerate(self.tf):
            if allowed is not None and i not in allowed:
                continue
            score = 0.0
            dl = self.length[i]
            for token in terms:
                freq = counts.get(token, 0)
                if not freq:
                    continue
                n_qi = self.df[token]
                idf = math.log(1.0 + (self.n - n_qi + 0.5) / (n_qi + 0.5))
                denom = freq + K1 * (1.0 - B + B * dl / self.avgdl)
                score += idf * (freq * (K1 + 1.0)) / denom
            if score > 0:
                scored.append((score, i))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [(i, score) for score, i in scored[:top_k]]

"""
Generation: assemble a grounded prompt from retrieved chunks and call
Claude for the final answer.

Two accuracy levers live here, separate from retrieval quality:
1. Forcing citations ([1], [2], ...) ties every claim back to a specific
   source chunk, which makes hallucinations visible instead of invisible.
2. An explicit instruction to say "I don't know" when the context doesn't
   contain the answer, rather than letting the model fall back on its own
   (unverified, un-cited) general knowledge.
"""

import os

from anthropic import Anthropic

from retrieve import RetrievedChunk, retrieve

MODEL = "claude-sonnet-4-5"

SYSTEM_PROMPT = """You are an internal support assistant for Northwind Retail Co. \
Answer the user's question using ONLY the numbered source excerpts provided below. \

Rules:
- Cite the source number(s) for every factual claim, like [1] or [1][3].
- If the excerpts do not contain enough information to answer, say so explicitly \
  ("I don't have information about that in the available documents") instead of guessing.
- Do not use outside knowledge, even if you're confident it's correct.
- Be concise and direct."""


def build_prompt(question: str, chunks: list[RetrievedChunk]) -> str:
    sources = "\n\n".join(
        f"[{i + 1}] (from {c.source_file})\n{c.text}" for i, c in enumerate(chunks)
    )
    return f"""Source excerpts:

{sources}

Question: {question}"""


def answer(question: str, top_k: int = 4) -> tuple[str, list[RetrievedChunk]]:
    chunks = retrieve(question, top_k=top_k)
    if not chunks:
        return "I don't have information about that in the available documents.", []

    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    prompt = build_prompt(question, chunks)
    response = client.messages.create(
        model=MODEL,
        max_tokens=500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return response.content[0].text, chunks


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "How many days of PTO do I get per year?"
    text, chunks = answer(q)
    print(f"Q: {q}\n")
    print(f"A: {text}\n")
    print("Sources used:")
    for i, c in enumerate(chunks):
        print(f"  [{i + 1}] {c.source_file} ({c.chunk_id})")

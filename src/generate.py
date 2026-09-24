"""
Generation: assemble a grounded prompt from retrieved chunks and call
an LLM for the final answer.

Two accuracy levers live here, separate from retrieval quality:
1. Forcing citations ([1], [2], ...) ties every claim back to a specific
   source chunk, which makes hallucinations visible instead of invisible.
2. An explicit instruction to say "I don't know" when the context doesn't
   contain the answer, rather than letting the model fall back on its own
   (unverified, un-cited) general knowledge.

Which vendor runs the model is llm.py (Anthropic vs OpenAI-compatible,
including Ollama). This file stays the grounded-prompt layer.

`answer()` is the blocking CLI path. `answer_stream()` is the same retrieve
→ prompt → LLM sequence, but yields typed events so a UI can show the
pipeline instead of a single blob at the end.
"""

from collections.abc import Iterator
from threading import Event

from llm import LLMConfigError, complete, load_settings, stream as llm_stream
from retrieve import RetrievedChunk, prompt_excerpts, retrieve

PREVIEW_CHARS = 240
NO_INFO = (
    "I don't have information about that in the available documents."
)

SYSTEM_PROMPT = """You are an internal support assistant for Northwind Retail Co. \
Answer the user's question using ONLY the numbered source excerpts provided below. \

Rules:
- Cite the source number(s) for every factual claim, like [1] or [1][3].
- If the excerpts do not contain enough information to answer, say so explicitly \
  ("I don't have information about that in the available documents") instead of guessing.
- Do not use outside knowledge, even if you're confident it's correct.
- Be concise and direct.
- Format in Markdown (short headings when useful, lists for steps or rules, \
  **bold** for key numbers or names). Keep citation markers like [1] as plain text, not links."""


def build_prompt(question: str, chunks: list[RetrievedChunk]) -> str:
    sources = "\n\n".join(
        f"[{i + 1}] (from {c.source_file})\n{c.text}" for i, c in enumerate(chunks)
    )
    return f"""Source excerpts:

{sources}

Question: {question}"""


def chunk_preview(text: str, limit: int = PREVIEW_CHARS) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _cancelled(cancel: Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def answer(question: str, top_k: int = 4) -> tuple[str, list[RetrievedChunk]]:
    chunks = prompt_excerpts(retrieve(question, top_k=top_k))
    if not chunks:
        return NO_INFO, []

    prompt = build_prompt(question, chunks)
    try:
        text = complete(SYSTEM_PROMPT, prompt)
    except LLMConfigError as exc:
        return str(exc), chunks
    return text, chunks


def answer_stream(
    question: str,
    top_k: int = 4,
    cancel: Event | None = None,
) -> Iterator[dict]:
    """Yield pipeline events: status → sources → token* → done (or error)."""
    yield {"type": "status", "stage": "retrieving"}
    chunks = prompt_excerpts(retrieve(question, top_k=top_k))
    if _cancelled(cancel):
        return

    yield {
        "type": "sources",
        "chunks": [
            {
                "n": i + 1,
                "source_file": c.source_file,
                "preview": chunk_preview(c.text),
                "expanded": c.expanded,
            }
            for i, c in enumerate(chunks)
        ],
    }
    if not chunks:
        yield {"type": "error", "message": NO_INFO}
        return
    if _cancelled(cancel):
        return

    try:
        load_settings()
    except LLMConfigError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    yield {"type": "status", "stage": "generating"}
    prompt = build_prompt(question, chunks)
    try:
        for text in llm_stream(SYSTEM_PROMPT, prompt, cancel=cancel):
            if _cancelled(cancel):
                return
            if text:
                yield {"type": "token", "text": text}
    except LLMConfigError as exc:
        yield {"type": "error", "message": str(exc)}
        return
    except Exception as exc:
        yield {"type": "error", "message": f"Generation failed: {exc}"}
        return

    if _cancelled(cancel):
        return
    yield {"type": "done", "question": question}


if __name__ == "__main__":
    import sys

    q = " ".join(sys.argv[1:]) or "How many days of PTO do I get per year?"
    text, chunks = answer(q)
    print(f"Q: {q}\n")
    print(f"A: {text}\n")
    print("Sources used:")
    for i, c in enumerate(chunks):
        print(f"  [{i + 1}] {c.source_file} ({c.chunk_id})")

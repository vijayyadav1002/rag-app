"""
Simple CLI entrypoint.

Usage:
    python cli.py "How many days of PTO do I get per year?"
"""

import sys

from generate import answer


def main():
    if len(sys.argv) < 2:
        print('Usage: python cli.py "your question"')
        sys.exit(1)

    question = " ".join(sys.argv[1:])
    text, chunks = answer(question)

    print(f"\nQ: {question}\n")
    print(f"{text}\n")
    how = chunks[0].rank_source if chunks else "vector"
    label = "reranker" if how == "rerank" else "vector search (reranker not confident)"
    print(f"--- Sources ({label}) ---")
    for i, c in enumerate(chunks):
        print(f"[{i + 1}] {c.source_file}")


if __name__ == "__main__":
    main()

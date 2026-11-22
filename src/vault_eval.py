"""Recall and a lexical faithfulness check for questions from this vault.

The handbook columns stay in eval.py. This file reads vault_questions.jsonl
when a person has added rows. It does not call an LLM and it does not score
the example file. A marked-wrong line in misses.jsonl is printed, not
treated as a failure, because the person already judged it.
"""

import json
import re
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
QUESTIONS_PATH = _ROOT / "vault_questions.jsonl"
MISSES_PATH = _ROOT / "misses.jsonl"
_WORD = re.compile(r"[a-z0-9]{4,}")


def load_vault_questions(path: Path) -> list[tuple[str, str]]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        question = item["question"].strip()
        source = item["source_file"].strip()
        if question and source:
            rows.append((question, source))
    return rows


def unsupported_sentences(answer: str, excerpts: list[str]) -> list[str]:
    """Sentences that cite [n] but share no 4-character word with excerpt n.

    A sentence with no citation is unsupported, except the fixed refusal
    sentence. This is a lexical overlap check, not an LLM judge.
    """
    from generate import NO_INFO

    bad = []
    # A citation after the period ("seat. [1]") stays with that sentence.
    parts = re.split(r"(?<=[.!\?])\s+(?!\[\d+\])", answer.strip())
    for part in parts:
        text = part.strip()
        if not text or text == NO_INFO or text.startswith(NO_INFO):
            continue
        cites = [int(n) for n in re.findall(r"\[(\d+)\]", text)]
        words = set(_WORD.findall(re.sub(r"\[\d+\]", " ", text.lower())))
        if not cites:
            bad.append(text)
            continue
        supported = False
        for n in cites:
            if n < 1 or n > len(excerpts):
                continue
            excerpt_words = set(_WORD.findall(excerpts[n - 1].lower()))
            if words & excerpt_words:
                supported = True
                break
        if not supported:
            bad.append(text)
    return bad


def main() -> int:
    misses = []
    if MISSES_PATH.is_file():
        misses = [line for line in MISSES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    print(f"Marked wrong: {len(misses)}")
    questions = load_vault_questions(QUESTIONS_PATH)
    if not questions:
        print("No vault questions. Add rows to src/vault_questions.jsonl.")
        return 0
    from retrieve import retrieve
    missed = []
    for question, source in questions:
        found = retrieve(question, top_k=1, candidate_pool=20, use_reranker=True, trust_gate=True, hybrid=True)
        names = [chunk.source_file for chunk in found]
        if source not in names:
            missed.append(question)
    print(f"Vault hybrid recall@1: {len(questions) - len(missed)}/{len(questions)}")
    for question in missed:
        print(f"  miss: {question}")
    return 1 if missed else 0


if __name__ == "__main__":
    sys.exit(main())

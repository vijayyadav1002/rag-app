"""
Tiny retrieval evaluation harness.

This is the concrete "accuracy" story: a hand-labeled set of questions with
a known correct source document, scored with recall@k (did the correct
source appear anywhere in the top-k retrieved chunks?). We run it once with
vector search alone and once with reranking enabled, so there's a real
before/after number instead of a vibe.

In a production system this set would be much larger and ideally sourced
from real user queries / support tickets, and you'd also track precision
and answer-level correctness (e.g. via an LLM-as-judge or RAGAS), not just
whether the right doc was retrieved. See README.md for that discussion.
"""

from dataclasses import dataclass

from retrieve import retrieve, vector_search

TEST_SET = [
    ("How many PTO days do I accrue per year?", "pto_policy.md"),
    ("What happens to unused PTO at the end of the year?", "pto_policy.md"),
    ("Do I need a doctor's note if I'm out sick for a week?", "sick_leave_policy.md"),
    ("How much sick time can I bank up over time?", "sick_leave_policy.md"),
    ("How many weeks of paid leave do I get after adopting a child?", "parental_leave_policy.md"),
    ("Does parental leave use up my PTO balance?", "parental_leave_policy.md"),
    ("What's the daily food budget when I'm traveling for work internationally?", "expense_policy.md"),
    ("How long do I have to submit a receipt after a business trip?", "expense_policy.md"),
    ("What's included in the home office stipend for remote workers?", "remote_work_equipment_policy.md"),
    ("What happens if I don't return my laptop when I leave the company?", "remote_work_equipment_policy.md"),
    ("How often do I need to change my password?", "it_security_policy.md"),
    ("Can I store customer data on my personal Google Drive?", "it_security_policy.md"),
    ("When do I need to enroll in health insurance as a new hire?", "benefits_enrollment_guide.md"),
    ("Does the company match my 401k contributions?", "benefits_enrollment_guide.md"),
    ("My thermostat is showing an E3 error, what does that mean?", "product_faq.md"),
    ("The thermostat won't connect to my 5GHz wifi, why not?", "product_faq.md"),
    ("What should a support agent do if a customer reports a burning smell?", "support_runbook.md"),
    ("Is a refund available for a thermostat that's 6 months old and defective?", "support_runbook.md"),
    ("What's the spending limit on a gift from a vendor before I have to report it?", "code_of_conduct.md"),
    ("Who do I contact to report a code of conduct violation anonymously?", "code_of_conduct.md"),
]


@dataclass
class EvalResult:
    label: str
    recall_at_k: dict[int, float]


def evaluate(use_reranker: bool, k_values=(1, 3, 5)) -> EvalResult:
    max_k = max(k_values)
    hits = {k: 0 for k in k_values}

    for query, expected_source in TEST_SET:
        if use_reranker:
            results = retrieve(query, top_k=max_k, candidate_pool=20, use_reranker=True)
        else:
            results = vector_search(query, top_k=max_k)

        retrieved_sources = [r.source_file for r in results]
        for k in k_values:
            if expected_source in retrieved_sources[:k]:
                hits[k] += 1

    n = len(TEST_SET)
    return EvalResult(
        label="with reranker" if use_reranker else "vector search only",
        recall_at_k={k: hits[k] / n for k in k_values},
    )


if __name__ == "__main__":
    print(f"Evaluating on {len(TEST_SET)} hand-labeled queries...\n")

    baseline = evaluate(use_reranker=False)
    reranked = evaluate(use_reranker=True)

    print(f"{'k':<4}{'vector only':<15}{'with rerank':<15}")
    for k in baseline.recall_at_k:
        v = baseline.recall_at_k[k]
        r = reranked.recall_at_k[k]
        print(f"{k:<4}{v:<15.0%}{r:<15.0%}")

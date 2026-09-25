# Confident rerank and a visible stale index

Two holes in the app as it runs today, measured on the current index (65 chunks, including `Ollama.md`).

The cross-encoder is allowed to replace vector order even when it thinks every candidate is irrelevant. On the 20-question eval set that swap is the only rerank miss: “What happens if I don't return my laptop when I leave the company?” Vector search returns `remote_work_equipment_policy.md` (cosine 0.694). Ungated rerank returns `it_security_policy.md` with a best logit of about -6.0. A MiniLM MS MARCO logit below 0 means the model’s own probability that the passage is relevant is under one half. Letting that ordering win is noise.

The other @1 miss is the opposite, and it should stay a rerank win. “When do I need to enroll in health insurance as a new hire?” Vector search prefers `onboarding_guide.md` (0.798 vs 0.775). Rerank prefers `benefits_enrollment_guide.md` with logit +0.35. That score is a real yes.

Separately, Approve writes the live Markdown and Library still labels that file `indexed`, because `list_docs` only compares paths with `config.json`’s `files` list. Ask then answers from the previous index with no signal. Re-index remains the only search update. The missing piece is telling the truth before the click.

Hybrid BM25, query rewrite, metadata filters, RAGAS, citation verification, auth, PDF, auto-reindex, and chat history stay out. This corpus already retrieves the `E3` query at rank 1 with vectors alone, and a lexical stage would not fix a reranker that already has the right chunk in its pool.

## Decisions

- Shipped `retrieve()` reranks the vector top 20, then keeps that order only when the best logit is at least `RERANK_TRUST` (`0.0`). Otherwise it restores vector-score order. The scores stay on the chunks either way.
- `0.0` is the MS MARCO decision point, `sigmoid(0) = 0.5`. It is not a cutoff fit to these 20 questions. Eval is the check that the choice does not push a correct document out of the top 3.
- The gate chooses one of the two existing lists. It does not blend scores and it does not drop neighbors. “I don’t know” is still the model’s job when every neighbor is off topic.
- `eval.py` prints three columns: vector only, ungated rerank order, and the shipped gate. The first two stay in the output so the laptop flip and the enrollment fix remain visible. Recall at 3 for all three stays 100%. The shipped column’s recall at 1 on this set is 100%.
- A live file is `changed` when it is in the index file list and its mtime is strictly after `indexed_at`. Paths missing from the list stay `not_indexed`. Paths in the list but gone stay `missing_on_disk`.
- `GET /api/status` adds `stale: true` when any listed file is not `indexed`. Ask shows that, with a link to Library. Library badges the file `changed` and says to Re-index. Neither page rebuilds on its own.
- Prompt expansion is unchanged: a hit still expands only when its logit is at least `0.0` and within `1.0` of the best logit. A gated result has a negative best logit, so nothing expands. Embedding and rerank still use the 800-character window.
- No new dependency, no new route, no pytest.

## Ask copy

Under the excerpts, one line:

- `ranking: "rerank"` → “Ordered by the reranker.”
- `ranking: "vector"` → “Reranker scores were below 0, so these stay in vector-search order.”

Above the question box, only when `stale` is true:

“The library has changed since the last Re-index. Answers still use the previous index.” The link goes to `/library`.

## What this does not do

It does not hash file bytes. A touch that updates mtime without an edit looks stale, and a restore that keeps an old mtime can look fresh. Approve and a normal save both update mtime, which is the path this app actually uses. It does not auto-reindex, and it does not hide the ungated rerank mistake from `eval.py` or from `python retrieve.py`.

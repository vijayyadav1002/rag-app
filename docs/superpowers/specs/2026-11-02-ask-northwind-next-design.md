# Ask Northwind next

Decision record for the three plans dated 2026-11-02. An executor reads this file and the plan it is implementing.

The product stays a framework-light RAG demo whose pipeline you can inspect. These changes make that inspection visible on an answer, make exact words competitive with vectors, and make a nested markdown folder practical to maintain. They do not add accounts, PDF, a server transcript, auto-reindex, a third HTML page, or another dependency.

## Already shipped

Do not rebuild these. The plans start after them.

- Citation text is checked in the browser. `claimRanges` / `paintExcerpt` / `.support-miss` in `src/static/index.html` mark a sentence that cites an excerpt the excerpt does not contain.
- Ask’s file preview has **Ask about this file**. It pins that path in the Search in menu. It does not draft a question, and Library has no equivalent control.
- A follow-up already shows `Follow-up searched as:`. The excerpts panel already says whether rerank order or vector order shipped, and it shows each shipped logit.
- Ask already shows a page-level note when `GET /api/status` has `stale: true`. A single source row does not say that its own file changed.
- New notes can be staged at `folder/name.md`. Approve already writes nested paths. Upload still stages a basename at the corpus root.
- `list_docs` marks `changed` from the stored SHA-256 when `config.json` has `digests`. Mtime is only the fallback when a digest is absent.

## Plans

Implement in this order. Each plan leaves the app working if the later plans are never started.

| Order | Plan | Outcome |
|---|---|---|
| 1 | `docs/superpowers/plans/2026-11-02-retrieval-trace.md` | The answer shows how retrieval chose its excerpts, a changed file is badged on that source, an empty thread offers headings, and Library can open Ask with a draft. |
| 2 | `docs/superpowers/plans/2026-11-02-hybrid-search.md` | BM25 joins the candidate pool. The existing three eval columns stay on the vector pool. Ask uses the fused pool only after the new column clears the bar. |
| 3 | `docs/superpowers/plans/2026-11-02-vault-notes.md` | Upload lands in the open folder, `[[wikilinks]]` open a note, and one file can be re-indexed without embedding the rest. |

## Retrieval trace

`retrieve()`’s return type stays `list[RetrievedChunk]`. Eval and `python retrieve.py` keep calling it.

Add `retrieve_explained(...)` with the same arguments plus `hybrid: bool = False`. It returns `(chunks, trace)`. `retrieve()` calls it with `hybrid=False` and drops the trace. With `hybrid=False` and the other defaults, `chunks` is the list `retrieve()` returns today, including the early return when `use_reranker` is false.

`trace` is a JSON object:

- `best_logit`: float or null
- `trusted`: bool, true only when `best_logit` is not null and `best_logit >= RERANK_TRUST` (`0.0`)
- `rank_source`: `"vector"` or `"rerank"`
- `vector`, `rerank`, `lexical`: each a list of at most 5 briefs

A brief is `{source_file, heading, vector_score, rerank_score, lexical_score}`. Scores are JSON numbers or null. Briefs contain no chunk text. `lexical` is empty until the hybrid plan fills it. `rerank` is empty when the reranker did not run.

`answer()` and `answer_stream()` call `retrieve_explained` and then `prompt_excerpts` on the chunk list, as they call `retrieve` today. The sources event gains `"trace": trace`. The prompt is unchanged.

The excerpts panel grows a `<details class="trace">` whose summary is `How this was retrieved`. The body states the best logit and which order shipped. It lists the vector briefs and, when present, the rerank briefs and the lexical briefs. A stored turn with no `trace` still renders its excerpts. The trace is stored on the thread snapshot.

## Changed source

The badge is client state from `GET /api/docs`, which already returns `docs[].state`. No new status field.

| `state` | Badge text |
|---|---|
| `changed` | Changed since Re-index |
| `not_indexed` | Not in the index |
| `missing_on_disk` | Missing from the library |
| `indexed` or absent | no badge |

The file name still opens the read-only preview. The badge is a link to `/library?doc=<path>`. Refresh the map when the status poll reports `stale`, and repaint badges on turns already on screen.

## Heading suggestions

`library.heading_questions(scope=None, docs_dir=None, limit=5)` reads markdown already on disk. It does not call a model and it does not read `src/review/`.

A heading is a line that starts with `## `, with the marker removed and backticks and emphasis markers stripped. Keep titles from 8 through 120 characters, at most 8 per file. Walk files in path order and take one heading from each file before taking a second from the first file. Each item is `{question, scope}` where `scope` is that file’s posix path and `question` is the heading text.

`GET /api/docs?suggest=` adds `suggestions`. An empty value is the whole corpus. Any other value goes through `clean_scope`; a bad value is HTTP 400. Omitting `suggest` leaves the key off the body. Ask requests this when the vault loads and when Search in changes. It does not request it on the status poll.

On an empty thread, up to five buttons sit under the lede. A click sets the textarea to `question`, sets Search in to that file (pinned if needed), and focuses the textarea. It does not submit.

## Library to Ask

A file row gets an Ask control whose href is `/?scope=<urlencoded path>`. The library preview lists that note’s `##` headings as links `/?scope=<path>&q=<urlencoded heading>`.

On load, Ask reads `scope` and `q`, applies them once, then `history.replaceState` to `/`. Ignore `scope` when it contains `\`, NUL, `..`, an empty or hidden segment, or an absolute path. Ignore `q` when it is empty or longer than 400 characters. Never submit from the query string.

## Hybrid search

New module `src/lexical.py`. No new package. Okapi BM25 with `k1 = 1.5` and `b = 0.75`. Tokens are the regex `[a-z0-9]+` on lowercased text. The index is rebuilt inside `reload()` from the same chunk list FAISS just loaded, guarded by the existing state lock.

`lexical_search(query, top_k=20, scope=None)` returns `RetrievedChunk` rows whose `lexical_score` is the BM25 score and whose `vector_score` is the real cosine against the query embedding. A query with no tokens returns `[]`. Scope uses `in_scope`. Chunks outside the scope are not scored.

`fuse_rrf(vector, lexical, limit, k=60)` unions the two lists by `chunk_id`. Rank is 1-based. The score is `1/(k+rank)` summed across lists the id appears in. A tie breaks toward the higher `vector_score`. The fused list is capped at `limit`.

`retrieve_explained(..., hybrid=False)` keeps today’s candidate pool: the vector search only. `hybrid=True` replaces that pool with `fuse_rrf` of the vector top `candidate_pool` and the lexical top `candidate_pool`, then runs the same rerank and the same trust gate. When the best logit is below `0`, the gate restores vector-score order of that fused pool. A lexical miss (`[]`) leaves the pool equal to the vector hits.

Eval prints a fourth column, `hybrid`, from `retrieve(..., hybrid=True, trust_gate=True)`. The existing three fetches stay on `hybrid=False`. Their misses stay the misses documented in `src/README.md`: ungated rerank still misses the laptop-return question; vector search still misses the new-hire enrollment question; confident still misses nothing on this set.

Ask and the CLI call `hybrid=True` only in the task that runs after `eval.py` has shown all four columns at 100% for k=3 and k=5, the original confident column at 100% for k=1, and the hybrid column at 100% for k=1. If that run fails, stop. Leave Ask on `hybrid=False`. Do not edit `TEST_SET` to clear the bar.

## Vault notes

### Upload folder

`stage_upload` gains `folder: str = ""`. The file’s own name still passes `safe_md_name` (basename, `.md`, no slash). A non-empty folder is one or more path segments joined in front of that basename, then the whole string passes `safe_md_relpath`. `POST /api/docs` accepts a multipart field `folder`. Library sends the selected pill when it is not `All`, and omits the field otherwise. A rejected folder fails that file with the existing upload error shape and writes nothing.

### Wikilinks

`library.resolve_wikilink(target, names) -> str | None`. Strip a `#heading` and a `|label`. Trim. Reject empty targets, backslashes, NUL, absolute paths, and any `.` or `..` or hidden segment. Compare case-insensitively. An exact relative path, with or without `.md`, wins. Otherwise the stem must match exactly one name. Zero or several stem matches return `None`.

Both markdown renderers turn a resolved `[[target]]` or `[[target|label]]` into a button. The label is the alias when present, otherwise the target. A click opens the note in the preview that page already has. `![[...]]` stays text. An unresolved link stays text.

### One-file re-index

`build(docs_dir=None, index_dir=None)` keeps today’s behavior when both are omitted.

`rebuild_paths(paths, docs_dir=None, index_dir=None, encode=None)`:

- Clean each path with `safe_md_relpath`. An empty list raises `IndexBuildError("No files to re-index.")`.
- A missing index, or `index.ntotal != len(chunks)`, calls `build()` for those directories and returns. It does not splice.
- Drop every stored chunk whose `source_file` is in the set. Reconstruct the kept vectors from the existing index. Chunk files that still exist. A path that is not on disk stays dropped.
- If nothing remains, raise `IndexBuildError("No chunks to index.")` before any replace. The live `chunks.faiss`, `metadata.pkl`, and `config.json` stay as they were.
- Embed only the new chunks. `encode(texts) -> float32 ndarray` of shape `(n, dim)`, already L2-normalized. The default encode is the existing `SentenceTransformer` path. Kept vectors are copied, not re-embedded.
- Write through `index/.tmp` and replace, the same way `build()` does.
- `indexed_at` is now. `files` is the sorted source set of the new chunk list. Digests are recomputed for paths in the request that still exist. Digests for every other remaining file are copied from the previous config. Digests for dropped files are removed.

`POST /api/reindex` with no JSON body, or with `{}`, calls `build()` as it does now. A JSON body `{"paths": ["hr/pto.md"]}` calls `rebuild_paths`. More than 20 paths is HTTP 400. A bad path is HTTP 400. `IndexBuildError` stays HTTP 400. The rebuild lock, the 409s, and `reload()` after a successful write stay as they are. The page-level Re-index button still posts no body. A file row posts that one path. Nothing watches the disk, and Approve still does not re-index.

## Global constraints

- Python 3.12. Run from `src/` with `./.venv/bin/python`. No pytest, no new dependency, no new HTML route, no Pydantic.
- Do not write the user’s `DOCS_DIR`, `src/review/`, `src/index/`, `src/sessions.json`, or `src/.env`. Checks use temporary directories.
- Retrieval windows stay 800 characters. `RERANK_TRUST`, `RERANK_FLOOR`, and `RERANK_MARGIN` stay `0.0`, `0.0`, and `1.0`.
- The website lock, the Approve gate, and browser-local threads stay. The server still stores no transcript.
- Markdown only. 1 MB per file. 20 files per upload.
- Company name is Northwind. Product name is Ask Northwind.
- When a plan changes `src/static/index.html`, `library.html`, or `app.css`, increment the integer in `CACHE` inside `src/static/sw.js`. Plan 1 expects `ask-northwind-v14` and writes `ask-northwind-v15`. Plan 3 expects `ask-northwind-v15` and writes `ask-northwind-v16`. If the constant is already newer, add 1 to whatever is there.
- A retrieval change fails if any existing column’s recall at 3 drops below 100%, or if the confident column’s recall at 1 drops below 100%, on the current `TEST_SET`.

# Self-updating RAG: review gate and longer sections

Approved changes to the Northwind corpus go through a human review queue before they touch the live Markdown. Ask still searches the last Re-index. When a retrieved hit is relevant, the prompt receives more of that `##` section than the 800-character window used for embedding and reranking.

This adds a review queue and a prompt-time section expansion on top of the library page in `2026-09-18-library-page-design.md`. Two-stage ingest stays: disk changes on Approve, search changes on Re-index. No auth, no auto-reindex, no chat history, no new HTML route, no pytest.

## Goal

A person can upload or edit Markdown, let the same LLM restack it into `#` / `##` form, correct that draft by hand, and approve it before it replaces the live file. Delete waits for the same approval. Relevant hits contribute the section text to the answer prompt. The answer itself stays concise.

## Decisions

- Longer means more source text in the prompt. It does not mean a longer answer, and it does not mean rewriting files to be longer.
- Relevant means the cross-encoder score is at least `0.0` and within `1.0` of the top hit. No second LLM judge. No user-feedback signal.
- Upload, edit, and delete on Library create a proposal. Ask does not write documents.
- The formatter output is a structural rewrite. Facts stay as written.
- A hand edit of the staged draft is the override. Approve writes that text.
- Approve writes the live file only. Re-index is still what updates search.
- One open proposal per target path. A later submit for that path replaces it.
- There is no login. Approve on Library is the human gate.
- Proposals live in `src/review/`, outside `DOCS_DIR`, and that directory is gitignored.

## Architecture

```
Upload / Edit / Delete
        │
        ▼
  src/review/<path>.json     (not searchable)
        │
   Approve │  Reject
        ▼
     DOCS_DIR/*.md           (live Markdown)
        │
   Re-index (unchanged)
        ▼
  FAISS + chunk metadata     (800-character window for search,
                              section text for the prompt)
        │
   Ask: rerank top 4, then expand hits that sit with the top score
```

`server.py` still does not retrieve or prompt. It transports, serves files, locks, and calls `build()` / `reload()`. Staging and Approve join the existing rebuild lock.

| Unit | Does | Depends on |
|------|------|------------|
| `format_md.py` | Rewrite one Markdown file into `#` / `##` form and reject a bad result | `llm.complete` |
| `review.py` | Proposal JSON, stage, save draft, approve, reject | `library.py`, `format_md.py` |
| `library.py` | Safe paths, list, atomic write, read, delete of live files | `chunk.resolve_docs_dir` |
| `chunk.py` | Same windows as today, plus `section_text` on each chunk | nothing new |
| `retrieve.py` | Copy section fields; `prompt_excerpts()` applies the score rule | pickled chunks |
| `generate.py` | Prompt and Ask sources use `prompt_excerpts()` | `retrieve.py` |
| `server.py` | Review HTTP API; upload and delete stage proposals | `review.py` |
| `static/library.html` | Review panel, editor, pending rows | the API above |
| `static/index.html` | Label each source `section` or `excerpt` | `sources` events |

## Review records

`src/review/` holds one JSON file per target. `hr/pto.md` is stored at `src/review/hr/pto.md.json`. The resolved path must stay inside `src/review/`. The same path rules as `safe_md_relpath` apply.

```json
{
  "target": "pto_policy.md",
  "op": "upsert",
  "body": "# Paid Time Off (PTO) Policy\n\n## Accrual\n...",
  "origin": "ai",
  "base_mtime": "2026-09-23T12:00:00+00:00",
  "base_mtime_ns": 1727092800000000000,
  "updated_at": "2026-09-23T12:01:00+00:00",
  "format_error": null
}
```

`op` is `upsert` or `delete`. `origin` is `ai` or `manual`. `base_mtime` is the live file's UTC modification time when the proposal was staged, or null when the file does not exist yet. `base_mtime_ns` is `st_mtime_ns` for the same moment, or null, and it is what Approve compares. `format_error` is a short string when formatting failed, otherwise null. A delete proposal has an empty body.

Upload accepts 1 to 20 `.md` files, basenames only, 1 MB each, UTF-8. It does not write the live file. Each accepted file is formatted and stored as `upsert` / `origin: ai`. A formatter failure still stores the raw text as `origin: manual` and sets `format_error`. Uploading a path that already has a proposal replaces that record.

Edit loads the open proposal when one exists, otherwise the live file. **Save draft** stores the textarea as `origin: manual`, clears `format_error`, and sets `base_mtime` / `base_mtime_ns` from the live file at save time. It does not call the model. **Format** runs the model on the current proposal body and, on success, stores `origin: ai` with `format_error` null. It does not change `base_mtime_ns` and does not approve. Delete on a live row replaces any open proposal with `op: delete`.

Approve of an upsert writes the body through an atomic replace (temporary file beside the target, then `replace`). Approve of a delete unlinks the live file. Both then remove the proposal. Reject removes the proposal and leaves the live file alone.

`GET /api/docs` adds `"pending": true` on a row whose name is an open proposal target. `list_docs` itself stays unaware of the queue, so `library.py` does not import `review.py`.

## HTTP

Unauthenticated. Rebuild lock copy stays `Index is rebuilding. Try again when it finishes.`

| Method | Path | Behavior |
|--------|------|----------|
| `GET` | `/api/docs` | Live files plus `pending`, as today otherwise |
| `GET` | `/api/docs/{path}` | `{ "name", "body", "mtime" }` for the live file. 404 when absent |
| `POST` | `/api/docs` | Stage uploads. Does not write live files. 400 when every file fails, 207 when some fail |
| `DELETE` | `/api/docs/{path}` | Stage `op: delete`. 404 when the live file is absent |
| `GET` | `/api/review` | `{ "proposals": [...], "unreadable": 0 }` |
| `PUT` | `/api/review/{path}` | JSON `{ "body": "..." }`. Save a hand edit. Creates a manual upsert when no proposal exists. 400 when the open proposal is a delete |
| `POST` | `/api/review/{path}/format` | Reformat the draft. 422 leaves the draft unchanged. 400 when `op` is `delete` |
| `POST` | `/api/review/{path}/approve` | Write or remove the live file, then drop the record |
| `DELETE` | `/api/review/{path}` | Reject. 404 when no proposal |

`POST /api/docs`, the review mutations, and Approve/Reject return 409 while `_rebuilding` is true. `GET /api/docs`, `GET /api/review`, and `GET /api/status` stay open.

Approve upsert returns 409 when the live `st_mtime_ns` does not match `base_mtime_ns`, and the proposal stays. Detail: `The live file changed since this draft was staged.` Approve delete of a file that is already gone drops the record and returns success. Approve upsert of an empty body returns 400. A proposal file that is not valid JSON is skipped; `unreadable` counts it.

`GET /api/review` and the upload response include the proposal objects so the page can render the queue. Upload and delete responses still include the docs list.

## Formatter

`complete(system, user, max_tokens=None)` keeps the answer cap of 500 tokens when `max_tokens` is omitted. The formatter calls it with `FORMAT_MAX_TOKENS = 4096`. A `max_tokens` / `length` stop raises `LLMTruncatedError`. `stream()` is unchanged.

`format_markdown` sends this system prompt and the draft as the user message:

```
You rewrite one internal company Markdown document so a heading-based chunker can split it.

Rules:
- Return only the Markdown document. No preamble.
- The first line is one title: "# Title".
- Use ## for every section. Do not use ### or deeper headings.
- Keep each ## section to one topic. If a section is longer than about 3000 characters, split it into further ## sections at a topic boundary.
- Do not add, remove, or change facts. Numbers, names, dates, durations, and conditions stay as written. Do not paraphrase sentences.
- You may add ## headings where the source has none. Heading text must be a label for the text already there, not a new claim.
- Keep lists as lists.
```

`validate_rag_markdown` strips one wrapping ` ``` ` / ` ```markdown ` fence, then requires a non-empty document, exactly one `# ` title, a first non-empty line that is that title, and no line that starts with `###`. The returned text ends with a newline. Any failure raises `FormatError` and does not replace the draft.

On upload, `FormatError`, `LLMTruncatedError`, and `LLMConfigError` store the raw file as a hand edit. The queue message is `No model is configured.` for a missing provider/key, `Formatting hit the token limit.` for truncation, and the validation message otherwise. **Format** on an open draft returns 422 with that detail and leaves the record unchanged.

Shipped policy files are about 1–1.6 KB. A file the model cannot return inside 4096 tokens stays as the original draft for a hand edit or a split.

## Section expansion

Embedding and reranking still use the 800-character window prefixed with `{doc title} > {heading}`. `eval.py` still scores whether the expected file is in the top k. Recall at 3 stays 100%. The current policy sections are all shorter than 800 characters, so their prompt text does not change. A longer `##` section does. The long Ollama note in the local `rag_docs` corpus is one section split into many windows.

`Chunk` gains `section_text: str = ""`. For a section of `SECTION_PROMPT_MAX = 4000` characters or less, `section_text` is the heading line plus the whole section body. For a longer section it is the heading line plus a 4000-character body slice that contains that chunk's window:

```python
start = min(max(0, window_start), len(body) - SECTION_PROMPT_MAX)
slice = body[start : start + SECTION_PROMPT_MAX]
```

The heading line is outside the 4000. Old pickles have no `section_text`. Readers use `getattr(..., "section_text", "")` and then expansion does nothing until the next Re-index. The window string stored in `Chunk.text` stays the same, so a rebuild does not move chunk boundaries.

`RetrievedChunk` gains `heading: str = ""`, `section_text: str = ""`, and `expanded: bool = False`. Rerank still scores `text` (the window).

`prompt_excerpts(chunks)` runs after the top 4 are chosen. It returns the chunks unchanged when the list is empty or the first chunk has no `rerank_score`. Otherwise let `top` be that score. A chunk qualifies when `rerank_score >= RERANK_FLOOR` (`0.0`), `rerank_score >= top - RERANK_MARGIN` (`1.0`), and `section_text` is non-empty. Qualifying chunks are returned with `text` set to `section_text` and `expanded` true. Chunks are already in rerank order.

Walk the top 4 in rerank order. The key is `(source_file, heading)`, or `chunk_id` when the heading is empty.

- A qualifying chunk is appended with `text = section_text` and `expanded` true.
- When that key is already in the result as a window, replace that slot instead of appending.
- When that key is already expanded, drop the later chunk.
- A chunk that does not qualify is appended, unless that key is already expanded.
- Two windows that both fail the score rule both stay. That keeps today's overlap behavior for weak hits.

| Top score | This hit | Prompt text |
|-----------|----------|-------------|
| 2.5 | 2.5 | Section text |
| 2.5 | 1.6 | Section text |
| 2.5 | 0.5 | 800-character window |
| −0.4 | −0.4 | 800-character window |

`answer()` and `answer_stream()` call `prompt_excerpts` before `build_prompt`. Citation numbers follow that list. The `sources` event adds `"expanded": true|false`. The preview is still ~240 characters of the text the model receives. `SYSTEM_PROMPT` stays concise. Vector-only retrieval does not expand.

## UI

`/library` keeps the current type, colors, and row layout. A **Review** panel sits above the file list.

Empty queue copy: `No pending changes.` Each row shows the path, **Update** or **Delete**, and **Formatted** or **Hand edit**. When `format_error` is set, the row also shows `Formatting failed` in the error color. Update rows have Edit, Approve, and Reject. Delete rows have Approve and Reject.

Edit opens a full-width textarea in the Review panel. The file list's Edit loads the open upsert when one exists, otherwise the live file. A pending delete does not open the editor; the status line says to reject that delete first. **Format**, **Save draft**, and **Approve** sit under it and wrap on a narrow viewport the way `.lib-actions` already wraps. Format and Approve save the textarea first, so the model and the live write use the text on screen. A status line under the buttons reports rebuild lock, conflict, and formatter failure. **Reload live file** loads `GET /api/docs/{path}` into the textarea and does not save. A 404 says `No live file yet.`

The file list gains **Edit** beside Delete. A pending file shows a brass `pending` label next to its existing state. Upload status becomes `Staged for review.` Delete confirm copy becomes `Stage deletion of {name}? It stays on disk and searchable until you approve it and re-index.` Offline Library copy stays `You're offline.`

On Ask, the source meta line is `[n] {file} · section` or `[n] {file} · excerpt`. The service worker cache name becomes `ask-northwind-v7`.

## Errors

| Situation | Result |
|-----------|--------|
| Mutation during rebuild | 409, existing lock sentence |
| Unsafe name, empty file, over 1 MB, batch size, non-UTF-8 | 400, or 207 when some files in a batch were staged. Messages stay the `library.py` / review messages |
| Missing live file or proposal | 404 |
| Approve empty upsert | 400 `Empty draft.` |
| Format or save on a delete proposal | 400 |
| Format failure on an open draft | 422, draft unchanged |
| Live `st_mtime_ns` mismatch on approve | 409, proposal kept |
| Approve delete of a missing file | 200, proposal dropped |
| Unreadable proposal JSON | Skipped, `unreadable` incremented, queue still loads |
| Path escapes `src/review/` | 400 |
| Chunk has no `section_text` | Ask uses the window |
| Re-index of an empty corpus | 400, existing index untouched |

Approve's temporary file is named `{name}.tmp` beside the target, so it does not match `*.md`. A crash before `replace` leaves the previous live file in place.

## Testing

No pytest and no new test package. From `src/`, with temporary directories (never the real `DOCS_DIR` or `src/review/`):

- `validate_rag_markdown` accepts a `#` / `##` document, strips one fence, and rejects an empty result, a missing title, two `#` titles, and a `###` heading.
- `prompt_excerpts` matches the score table above, collapses two qualifying windows of one heading into one excerpt, keeps two non-qualifying windows, and returns vector-only chunks unchanged.
- `chunk_document` on a section longer than 800 characters stores `section_text` that contains the window body, and the embedded `text` is still the 800-character window. A section under 800 stores the whole section.
- `TestClient`: upload does not create the live file and does create a proposal; a patched formatter failure stores `origin: manual` and `format_error`; PUT saves a hand edit; approve writes the bytes; a mismatched `base_mtime_ns` returns 409 and does not write; reject leaves the live file; staged delete plus approve removes the file; approve of an already-missing delete target returns 200; rebuild lock returns 409; `../x.md` returns 400.
- `eval.py` after the retrieve change: recall at 3 stays 100% for vector search and for rerank. Expansion runs in `generate.py`, so this check guards the window text and the top-k cut.

## Docs

Update the Library paragraphs in `src/README.md` and `AGENTS.md` so they describe staging, Approve, and Re-index. Mention `format_md.py`, `review.py`, `src/review/`, and the section-expansion constants in the key-files list. Add `src/review/` to `.gitignore`.

## Out of scope

Ask-page corrections, a second reviewer identity, auto-reindex on Approve, paraphrasing the source, PDF/Word, chat history, and changing `CHUNK_SIZE`.

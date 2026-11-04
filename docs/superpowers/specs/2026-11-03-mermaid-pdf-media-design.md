# Mermaid, PDF text, and media preview

Ask Northwind can draw one diagram in an answer, render that diagram in Markdown preview, index the text layer of a PDF, and show an image or PDF that a note names. Approve still writes the corpus. Search still changes only on Re-index.

This file records the choice. It does not replace the earlier library, lock, or retrieval specs.

## What shipped

- One renderer, `src/static/render.js`. Ask and Library both call it. A closed mermaid fence becomes a diagram only after the caller asks. An unclosed fence stays a code block while an answer is streaming. When the answer finishes, a mermaid fence is drawn even if the model left it open. Copy markdown returns the fence source. A closed fence in a chat excerpt is drawn beside the plain text.
- Mermaid 11.17.2 is vendored at `src/static/vendor/mermaid.min.js`. Drawing uses `securityLevel: "strict"`, `htmlLabels: false`, and `mermaid.render`. The page does not use `class="mermaid"` or `startOnLoad`.
- `GET /api/media/{path}` serves png, jpg, jpeg, gif, webp, and pdf. The session cookie is required. The content type comes from the suffix. SVG is rejected. The response is `nosniff`, `inline`, and `Cache-Control: private, no-store`.
- `![alt](rel.png)` resolves against the note's directory. `![[file.png]]` and `![[file.pdf]]` resolve when the basename is unique among media names. `![[note]]` and `![[note.md]]` stay text. `https` images stay links. A PDF reference is a document frame. An Ask excerpt keeps its plain text and puts the media beside it.
- A PDF becomes ordinary chunks. The heading is `{filename stem} > Page N`. There is no page field and no sibling Markdown file. Images are listed as `asset` and are not embedded.
- Image and PDF bytes wait in `src/media_stage/` until Approve. The review screen shows them in the same list as Markdown proposals. Format does not run on those bytes. Format of a note shields a closed mermaid fence and an embed, then splices the original back.
- A PDF with no text layer may be published as zero chunks. A read or decrypt error does not replace the last good index. A single page whose own extract throws becomes an empty string and that page is dropped. That is not a failure of the file. Only the first 100 pages are indexed. That sentence is a file-row warning, not retrieved prose. The size check (8 MB) happens before pypdf. Markdown stays at 1 MB.
- Answer generation may add at most one closed mermaid fence. `MAX_TOKENS` is 1200. The follow-up rewrite stays at 80. Format stays at 4096.

## Why the PDF stays the file

Two shapes were sketched.

INDEX reads the text layer into `Chunk` rows and leaves the PDF as the file a citation opens. SIDECAR would write `name.pdf.md` into the notes folder on Re-index and keep the chunker Markdown-only.

SIDECAR was rejected. A rebuild would write and delete notes the user did not approve. A corrupt PDF could leave the old sibling in place, so search would cite text the open PDF no longer contains. An empty extract could delete that note before publish refused the build.

The cross-check scored INDEX 4/5/5/4/4/4 and SIDECAR 3/2/2/2/2/3 on: one owner per invariant, an honest index, one renderer, a small public surface, a boring Markdown path, and safe failure.

## Grafts

- Page extract stays private. Callers stop at `collect_corpus`, `collect_named`, and `open_media`.
- An empty text layer may publish. A failed read may not.
- The media route takes a corpus path. It does not take a note plus a reference. The browser joins a relative reference to the note directory. The server still checks the path.
- Format shields diagrams and embeds. It does not refuse the whole note because a fence is present.

## Out of scope

OCR, vision models, Word, plain `.txt`, SVG uploads, generated sibling notes, auto-reindex, and a server-side chat log stay out. Note wikilinks stay buttons. The 20-question eval set stays on the synthetic Markdown handbook.

# Ask fullscreen document and in-page edit Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** On Ask, opening a document covers the screen the way Library already does, and a Markdown note can be edited in that same dialog.

**Architecture:** Keep one `#preview` dialog. Extend the existing Library fullscreen CSS to `body.ask-app #preview`. Add an edit mode in that dialog that calls the review routes Library already uses (`PUT /api/review/{path}`, `POST .../format`, `POST .../approve`). Save draft and Format write a proposal. Approve writes the live file. Nothing in this feature rebuilds the index. Images and PDFs stay view-only. No new route, no new JS file, no Python change.

**Tech Stack:** Existing Ask page (`src/static/index.html`), shared CSS (`src/static/app.css`), service worker (`src/static/sw.js`). Review API in `src/server.py` and `src/review.py` stays as it is. Browser proof uses `.cursor/skills/verify-ask-northwind/verify-ask` on `127.0.0.1:8765` only.

**Spec:** No separate spec. This plan is the spec. The user asked for fullscreen documents on the Ask page, and for editing a document from that page. Library already opens notes fullscreen (`body.library-page #preview` in `src/static/app.css`) and edits them in the review panel (`#draft`, Save draft, Format, Approve). Ask still uses the 36rem side sheet and a read-only body.

## Global Constraints

- No new HTML route, no new dependency, no pytest, no new test file, no new static JS file.
- Approve stays the corpus gate. Save draft does not write `DOCS_DIR`. Format does not write `DOCS_DIR`.
- Do not call `POST /api/reindex` from the Ask dialog. Search changes only after Re-index on Library.
- Edit only a path whose basename ends with `.md` (case-insensitive). `save_draft` rejects anything else with `Only .md filenames are allowed.`
- An image (png, jpg, jpeg, gif, webp) or PDF stays view-only. Do not send those paths to `/api/review/`.
- A pending delete or rename for that path is not overwritten from Ask.
- Do not rewrite older files under `docs/superpowers/specs/`.
- Verification never starts `src/server.py`, never uses port 8000, never reads `src/.env`, and never writes the user's `DOCS_DIR`, `src/review/`, `src/index/`, or `src/sessions.json`.
- The isolated copy's password is `verify-northwind`. Five wrong passwords from `127.0.0.1` lock login for 60 seconds. Do not retry a failed password in a loop.
- Bump `src/static/sw.js` `CACHE` whenever `/`, `/library`, or `/app.css` changes. Task 1 sets `ask-northwind-v21`. Task 2 sets `ask-northwind-v22`.
- Copy in the dialog, status line, and confirm box is the exact sentences in the tasks below.

## Review Focus

- Save draft writes a review proposal and leaves the live file bytes unchanged. A person who hits Save draft has not published the note.
- Approve writes the live file and does not rebuild the index. The Ask stale note appears. Answers keep using the previous index until Library Re-index.
- An image or PDF never shows Edit and never receives a review PUT.
- A pending delete or rename is left intact. Ask tells the person to finish it in Library.
- Escape, Close, Done, Ask about this file, and opening another document discard unsaved textarea text only after `window.confirm("Discard unsaved edits?")`. The lock screen closes the dialog without that confirm.

---

### Task 1: Fullscreen Ask preview

**Files:**
- Modify: `src/static/app.css` (the `#preview` block near the comment `Library preview covers the viewport`)
- Modify: `src/static/index.html` (`readLaunch`, `applyLaunchDraft`)
- Modify: `src/static/sw.js` (`CACHE`)
- Modify: `AGENTS.md` (the service-worker cache sentence only)

**Interfaces:**
- Consumes: `openPreview(name)` already in `src/static/index.html`. `acceptScope(value)` already rejects empty segments, `..`, absolute paths, backslashes, and hidden segments.
- Produces: `pendingLaunch.doc` (string). `applyLaunchDraft()` calls `openPreview(doc)` when `doc` passes `acceptScope` and ends with `.md`, `.pdf`, `.png`, `.jpg`, `.jpeg`, `.gif`, or `.webp`. Library's dialog rules are unchanged except that the shared fullscreen selectors also match `body.ask-app #preview`.

- [ ] **Step 1: Share the fullscreen rules with Ask**

In `src/static/app.css`, replace the comment and the selector prefix `body.library-page #preview` on the viewport, sheet, header, and `.preview-body` reading-column rules (the block that starts at the comment below, through the `img:only-child > img` rule). Do not change the `#preview-asks` rules or the footer rules that mention `#preview-edit`. Those stay `body.library-page` only.

`--library-measure` and `--library-rail` are set on `body.library-page` only. The shared reading column uses them. Set both on Ask too, or the `padding-inline` math is invalid on Ask and the text will not sit in the column:

```css
body.library-page,
body.ask-app {
  --library-measure: 68ch;
  --library-rail: 72rem;
}
```

Remove the duplicate custom properties from the old `body.library-page` block so they are declared once.

The comment today:

```css
/* Library preview covers the viewport. Ask keeps the #preview sheet above.
   max-width/max-height: none clears the user-agent dialog:modal cap. */
```

Replace that comment with:

```css
/* Ask and Library previews cover the viewport.
   max-width/max-height: none clears the user-agent dialog:modal cap. */
```

On each rule in that block except `#preview-asks` and `.sheet footer`, change the selector from `body.library-page #preview` to `body.library-page #preview, body.ask-app #preview`. One example, the dialog box:

```css
body.library-page #preview,
body.ask-app #preview {
  position: fixed;
  inset: 0;
  margin: 0;
  width: auto;
  height: 100dvh;
  max-width: none;
  max-height: none;
  border: 0;
  border-radius: 0;
  box-shadow: none;
  overflow: hidden;
  overscroll-behavior: contain;
  background: var(--bg);
}
```

Apply that same comma form to the sheet, sheet header, sheet header `> div`, and every `.preview-body` rule in the block (measure column, `pre`, wide blocks, pdf-only frame, image-only frame). Leave these Library-only:

```css
body.library-page #preview #preview-asks { /* unchanged */ }
body.library-page #preview .sheet footer { /* unchanged */ }
body.library-page #preview .sheet footer:has(#preview-edit[hidden]) { /* unchanged */ }
body.library-page #preview .sheet footer:not([hidden]) { /* unchanged */ }
```

The base `#preview` side-sheet rule (margin, `min(36rem, ...)`) stays, so a dialog outside these two pages is unchanged. `body.ask-app #preview` wins on specificity over the `@media (max-width: 767px) #preview` rule.

- [ ] **Step 2: Open `?doc=` in the existing Ask dialog**

`applyLaunchDraft()` already runs after unlock, then `history.replaceState(null, "", "/")`. Capture `doc` before that wipe.

In `readLaunch()`, extend the stored object:

```javascript
pendingLaunch = {
  scope: typeof scope === "string" ? scope : "",
  q: typeof q === "string" ? q : "",
  doc: typeof params.get("doc") === "string" ? params.get("doc") : "",
  done: false,
};
```

At the end of `applyLaunchDraft()`, after `history.replaceState` and before `return changed`, add the call `openLaunchDoc();`. Define this next to `applyLaunchDraft`:

```javascript
function openLaunchDoc() {
  if (!pendingLaunch || !pendingLaunch.doc) return;
  const doc = pendingLaunch.doc;
  pendingLaunch.doc = "";
  if (!acceptScope(doc)) return;
  const lower = doc.toLowerCase();
  const editable = lower.endsWith(".md");
  const media = /\.(png|jpe?g|gif|webp|pdf)$/.test(lower);
  if (!editable && !media) return;
  openPreview(doc);
}
```

`openPreview` is unchanged in this task. A markdown hit still renders with `renderMarkdown`. An image or PDF still builds an `img` or `iframe` from `/api/media/`. Close, Escape, and the backdrop still close the dialog. Ask about this file and Open in Library stay in the footer.

- [ ] **Step 3: Bump the shell cache**

In `src/static/sw.js` set `const CACHE = "ask-northwind-v21";`.

In `AGENTS.md`, change the cache sentence from `ask-northwind-v20` to `ask-northwind-v21`. Leave the rest of that bullet as it is. Task 2 bumps it again.

- [ ] **Step 4: Prove the dialog covers the viewport**

Run, from the repo root:

```bash
.cursor/skills/verify-ask-northwind/verify-ask launch
.cursor/skills/verify-ask-northwind/verify-ask doctor
.cursor/skills/verify-ask-northwind/verify-ask unlock
```

Expected: `ready` and a pid, then `ok`, then `unlock ok`. Doctor must show the listener is `/tmp/ask-northwind-verify/app/server.py` on `127.0.0.1:8765`.

Write a tiny PNG only inside the copy:

```bash
/Users/vijay/Projects/rag-app/src/.venv/bin/python -c '
from pathlib import Path
png = bytes.fromhex("89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4890000000a49444154789c63000100000500010d0a2db40000000049454e44ae426082")
Path("/tmp/ask-northwind-verify/app/docs/sample.png").write_bytes(png)
'
```

Drive `http://127.0.0.1:8765/?doc=pto_policy.md` in the browser after the unlock cookie is set (submit `#lock-form` with password `verify-northwind` if the tab is still locked). Then:

- `#preview` is open.
- Its `getBoundingClientRect()` width equals `window.innerWidth` and its height equals `window.innerHeight` (allow 1px).
- `#preview-body` contains rendered text from the PTO note (a heading or the words `PTO`), not the raw `##` source as the only content.
- The address bar path is `/` (the query was cleared).
- Close (`#preview-close`) closes it. Open the same URL again. Press Escape. The dialog closes.
- Set the viewport to 390×844. Open `/?doc=pto_policy.md` again. The dialog still matches the visual viewport width and height.
- Open `/?doc=sample.png`. `#preview-body` contains one `img` whose `src` contains `/api/media/sample.png`. The dialog is still fullscreen.
- Open `http://127.0.0.1:8765/library`, unlock if needed, and preview `pto_policy.md` from the file row. `#preview` is fullscreen there too. `#preview-edit` reads `Edit in Library`. Click it: the dialog closes and `#editor` is visible with `#draft` filled. Do not Approve.

Then:

```bash
.cursor/skills/verify-ask-northwind/verify-ask cleanup
```

Expected: `cleaned`. `src/sessions.json` is unchanged. Do not leave the verify server running.

- [ ] **Step 5: Commit**

```bash
git add src/static/app.css src/static/index.html src/static/sw.js AGENTS.md
git commit -m "feat: open Ask documents fullscreen"
```

---

### Task 2: Edit a Markdown note in the Ask dialog

**Files:**
- Modify: `src/static/index.html` (dialog markup, preview script)
- Modify: `src/static/app.css` (Ask footer, draft textarea, status line)
- Modify: `src/static/sw.js` (`CACHE` to `ask-northwind-v22`)
- Modify: `AGENTS.md`, `src/README.md`, `src/LEARNING.md` (the sentences that say the Ask file view is read-only, plus the cache name)

**Interfaces:**
- Consumes: `openPreview(name)`, `previewName`, `previewGen`, `encPath`, `showLock`, `SESSION_ENDED`, `OFFLINE_MSG` (`"Not connected. Use Reconnect."`), `setRebuildLock`, `rebuilding`, `checkRebuildOnLoad`, `acceptScope` from Task 1's page. Review JSON from `GET /api/review`: `{ proposals: [{ target, op, body, format_error }], unreadable }`. `op` is `upsert`, `delete`, or `rename`.
- Produces: edit mode inside `#preview`. Live file bytes change only after `#preview-approve` gets a 200 from `POST /api/review/{path}/approve`.

- [ ] **Step 1: Add the edit controls to the dialog**

Replace the Ask `#preview` footer in `src/static/index.html` so the dialog is:

```html
<dialog id="preview" closedby="any" aria-labelledby="preview-title">
  <div class="sheet">
    <header>
      <div>
        <h2 id="preview-title">Document</h2>
        <p id="preview-meta"></p>
      </div>
      <button type="button" class="ghost" id="preview-close">Close</button>
    </header>
    <div id="preview-body" class="preview-body"></div>
    <textarea id="preview-draft" hidden spellcheck="false" aria-label="Draft markdown"></textarea>
    <footer>
      <div id="preview-view-actions">
        <button type="button" id="preview-ask">Ask about this file</button>
        <button type="button" id="preview-edit" hidden>Edit</button>
        <a id="preview-library" href="/library">Open in Library</a>
      </div>
      <div id="preview-edit-actions" hidden>
        <button type="button" id="preview-format">Format</button>
        <button type="button" id="preview-save">Save draft</button>
        <button type="button" id="preview-approve">Approve</button>
        <button type="button" class="ghost" id="preview-reload">Reload live file</button>
        <button type="button" class="ghost" id="preview-done">Done</button>
      </div>
      <p id="preview-status"></p>
    </footer>
  </div>
</dialog>
```

Wire the new elements next to the existing `previewAsk` constants:

```javascript
const previewEdit = document.getElementById("preview-edit");
const previewDraft = document.getElementById("preview-draft");
const previewViewActions = document.getElementById("preview-view-actions");
const previewEditActions = document.getElementById("preview-edit-actions");
const previewFormat = document.getElementById("preview-format");
const previewSave = document.getElementById("preview-save");
const previewApprove = document.getElementById("preview-approve");
const previewReload = document.getElementById("preview-reload");
const previewDone = document.getElementById("preview-done");
const previewStatus = document.getElementById("preview-status");
```

State next to `previewGen`:

```javascript
let previewMode = "view";
let previewBaseline = "";
let previewLive = "";
let previewBusy = false;
```

- [ ] **Step 2: Style the Ask editor inside the fullscreen sheet**

Append to `src/static/app.css`:

```css
body.ask-app #preview .sheet footer {
  flex-wrap: wrap;
  justify-content: flex-end;
  align-items: center;
  padding-bottom: max(0.8rem, env(safe-area-inset-bottom));
}
body.ask-app #preview-view-actions,
body.ask-app #preview-edit-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.45rem;
  justify-content: flex-end;
}
body.ask-app #preview-edit-actions[hidden],
body.ask-app #preview-view-actions[hidden],
body.ask-app #preview-draft[hidden],
body.ask-app #preview-body[hidden],
body.ask-app #preview-edit[hidden] {
  display: none !important;
}
body.ask-app #preview-draft {
  flex: 1;
  min-height: 0;
  width: auto;
  margin: 0;
  resize: none;
  border: 0;
  border-radius: 0;
  background: var(--bg);
  font-family: "JetBrains Mono", ui-monospace, monospace;
  font-size: 0.85rem;
  line-height: 1.5;
  padding-block: 1.35rem 2.75rem;
  padding-inline: max(1.25rem, calc((100% - var(--library-rail, 72rem)) / 2));
}
body.ask-app #preview-status {
  flex-basis: 100%;
  margin: 0;
  color: var(--muted);
  font-size: 0.78rem;
}
body.ask-app #preview-status.err { color: var(--err); }
body.ask-app #preview-status:empty { display: none; }
```

`#draft` in the Library panel is a different element. Do not restyle it.

- [ ] **Step 3: View mode, edit mode, and the dirty guard**

Add these functions in the Ask script. `detail` matches Library:

```javascript
function detail(body, fallback) {
  if (!body) return fallback;
  if (typeof body.detail === "string") return body.detail;
  return fallback;
}

function isMarkdownName(name) {
  return String(name).toLowerCase().endsWith(".md");
}

function setPreviewStatus(text, kind) {
  previewStatus.textContent = text || "";
  previewStatus.className = kind === "err" ? "err" : "";
}

function previewDirty() {
  return previewMode === "edit" && previewDraft.value !== previewBaseline;
}

function confirmDiscard() {
  if (!previewDirty()) return true;
  return window.confirm("Discard unsaved edits?");
}

function showPreviewView() {
  previewMode = "view";
  previewDraft.hidden = true;
  previewBody.hidden = false;
  previewViewActions.hidden = false;
  previewEditActions.hidden = true;
}

function showPreviewEdit() {
  previewMode = "edit";
  previewBody.hidden = true;
  previewDraft.hidden = false;
  previewViewActions.hidden = true;
  previewEditActions.hidden = false;
  syncPreviewEditLock();
}

function syncPreviewEditLock() {
  const locked = rebuilding || previewBusy;
  previewFormat.disabled = locked;
  previewSave.disabled = locked;
  previewApprove.disabled = locked;
  previewReload.disabled = locked;
}
```

In `setRebuildLock`, after `syncComposer()`, call `syncPreviewEditLock()` when `previewFormat` exists.

Replace the Close listener:

```javascript
previewClose.addEventListener("click", () => {
  if (!confirmDiscard()) return;
  previewMode = "view";
  previewDialog.close();
});
```

Add a cancel listener so Escape and the backdrop use the same sentence. `close()` itself does not fire `cancel`, so the button path above is separate:

```javascript
previewDialog.addEventListener("cancel", (event) => {
  if (!previewDirty()) return;
  event.preventDefault();
  if (window.confirm("Discard unsaved edits?")) {
    previewMode = "view";
    previewDialog.close();
  }
});
```

In `showLock`, set `previewMode = "view"` before `previewDialog.close()` so a session end does not prompt.

```javascript
window.addEventListener("beforeunload", (event) => {
  if (!previewDirty()) return;
  event.preventDefault();
  event.returnValue = "";
});
```

- [ ] **Step 4: Load the note, then the pending draft**

At the start of `openPreview`, after the empty-name return:

```javascript
if (previewDialog.open && previewMode === "edit" && name === previewName) return;
if (previewDialog.open && !confirmDiscard()) return;
```

Then, still before the fetch, reset the chrome:

```javascript
showPreviewView();
previewLive = "";
previewBaseline = "";
previewDraft.value = "";
previewEdit.hidden = true;
setPreviewStatus("");
```

Keep the rest of `openPreview`. On a markdown 200, store the body and reveal Edit:

```javascript
previewLive = typeof body.body === "string" ? body.body : "";
previewMeta.textContent = "View only";
previewEdit.hidden = false;
previewBody.innerHTML = renderMarkdown(previewLive, { sourceFile: name, mermaid: "draw" });
drawAnswer(previewBody);
```

On 404, image, PDF, and any non-ok response, `previewEdit.hidden` stays true. Image and PDF paths must not fetch `/api/review/`.

Edit click:

```javascript
async function startPreviewEdit() {
  if (previewMode === "edit" || !isMarkdownName(previewName)) return;
  if (!navigator.onLine) { setPreviewStatus(OFFLINE_MSG, "err"); return; }
  const name = previewName;
  const gen = previewGen;
  try {
    const res = await fetch("/api/review");
    if (gen !== previewGen || name !== previewName) return;
    if (res.status === 401) { showLock(SESSION_ENDED); return; }
    const body = await res.json().catch(() => ({}));
    if (!res.ok) {
      setPreviewStatus(detail(body, "Could not load the draft."), "err");
      return;
    }
    const pending = (body.proposals || []).find((item) => item && item.target === name);
    if (pending && pending.op === "delete") {
      setPreviewStatus("Reject the pending delete in Library before editing this file.", "err");
      return;
    }
    if (pending && pending.op === "rename") {
      setPreviewStatus("Finish or reject the pending move in Library before editing this file.", "err");
      return;
    }
    let text = previewLive;
    let status = "";
    let kind = "";
    if (pending && pending.op === "upsert" && typeof pending.body === "string") {
      text = pending.body;
      status = pending.format_error || "Pending draft. Approve to write the live file.";
      if (pending.format_error) kind = "err";
    }
    previewBaseline = text;
    previewDraft.value = text;
    showPreviewEdit();
    setPreviewStatus(status, kind);
    previewDraft.focus();
  } catch (err) {
    if (gen !== previewGen) return;
    setPreviewStatus(OFFLINE_MSG, "err");
  }
}
```

Done returns to the rendered live body already in `#preview-body`. It does not write:

```javascript
previewDone.addEventListener("click", () => {
  if (!confirmDiscard()) return;
  showPreviewView();
  setPreviewStatus("");
});
```

`askAboutFile` must refuse a dirty editor. At its start:

```javascript
if (!confirmDiscard()) return;
previewMode = "view";
```

The function already closes the dialog and pins the file. Keep that.

- [ ] **Step 5: Save, format, reload, and approve**

```javascript
async function putPreviewDraft() {
  const res = await fetch("/api/review/" + encPath(previewName), {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body: previewDraft.value }),
  });
  const body = await res.json().catch(() => ({}));
  if (res.status === 401) showLock(SESSION_ENDED);
  return { res, body };
}

function showPreviewError(res, body, fallback) {
  if (res.status === 401) { showLock(SESSION_ENDED); return; }
  const message = detail(body, fallback);
  if (res.status === 409 && message.indexOf("rebuilding") !== -1) {
    setRebuildLock(true);
    setPreviewStatus("Index is rebuilding. Try again when it finishes.", "err");
    startRebuildPoll();
    return;
  }
  setPreviewStatus(message, "err");
}

previewSave.addEventListener("click", async () => {
  if (previewMode !== "edit" || !isMarkdownName(previewName) || previewBusy) return;
  if (!navigator.onLine) { setPreviewStatus(OFFLINE_MSG, "err"); return; }
  previewBusy = true;
  syncPreviewEditLock();
  try {
    const { res, body } = await putPreviewDraft();
    if (res.status === 401) return;
    if (!res.ok) { showPreviewError(res, body, "Save failed"); return; }
    previewBaseline = previewDraft.value;
    setPreviewStatus("Draft saved. Approve to write the live file.", "");
  } catch (err) {
    setPreviewStatus(OFFLINE_MSG, "err");
  } finally {
    previewBusy = false;
    syncPreviewEditLock();
  }
});

previewFormat.addEventListener("click", async () => {
  if (previewMode !== "edit" || !isMarkdownName(previewName) || previewBusy) return;
  if (!navigator.onLine) { setPreviewStatus(OFFLINE_MSG, "err"); return; }
  previewBusy = true;
  syncPreviewEditLock();
  const name = previewName;
  const gen = previewGen;
  try {
    const saved = await putPreviewDraft();
    if (saved.res.status === 401) return;
    if (!saved.res.ok) { showPreviewError(saved.res, saved.body, "Save failed"); return; }
    previewBaseline = previewDraft.value;
    const res = await fetch("/api/review/" + encPath(name) + "/format", { method: "POST" });
    if (gen !== previewGen || name !== previewName) return;
    const body = await res.json().catch(() => ({}));
    if (res.status === 401) { showLock(SESSION_ENDED); return; }
    if (!res.ok) { showPreviewError(res, body, "Formatting failed"); return; }
    const next = body.proposal && typeof body.proposal.body === "string" ? body.proposal.body : previewDraft.value;
    previewDraft.value = next;
    previewBaseline = next;
    setPreviewStatus("Formatted. Approve to write the live file.", "");
  } catch (err) {
    if (gen === previewGen) setPreviewStatus(OFFLINE_MSG, "err");
  } finally {
    previewBusy = false;
    syncPreviewEditLock();
  }
});

previewReload.addEventListener("click", async () => {
  if (previewMode !== "edit" || !previewName || previewBusy) return;
  if (!navigator.onLine) { setPreviewStatus(OFFLINE_MSG, "err"); return; }
  const name = previewName;
  const gen = previewGen;
  try {
    const res = await fetch("/api/docs/" + encPath(name));
    if (gen !== previewGen || name !== previewName) return;
    if (res.status === 401) { showLock(SESSION_ENDED); return; }
    const body = await res.json().catch(() => ({}));
    if (res.status === 404) { setPreviewStatus("No live file yet.", "err"); return; }
    if (!res.ok) { showPreviewError(res, body, "Could not load the live file."); return; }
    previewDraft.value = typeof body.body === "string" ? body.body : "";
    previewLive = previewDraft.value;
    setPreviewStatus("Loaded the live file. Save draft to keep it.", "");
  } catch (err) {
    if (gen === previewGen) setPreviewStatus(OFFLINE_MSG, "err");
  }
});

previewApprove.addEventListener("click", async () => {
  if (previewMode !== "edit" || !isMarkdownName(previewName) || previewBusy) return;
  if (!navigator.onLine) { setPreviewStatus(OFFLINE_MSG, "err"); return; }
  previewBusy = true;
  syncPreviewEditLock();
  const name = previewName;
  const gen = previewGen;
  try {
    const saved = await putPreviewDraft();
    if (saved.res.status === 401) return;
    if (!saved.res.ok) { showPreviewError(saved.res, saved.body, "Save failed"); return; }
    previewBaseline = previewDraft.value;
    const res = await fetch("/api/review/" + encPath(name) + "/approve", { method: "POST" });
    if (gen !== previewGen || name !== previewName) return;
    const body = await res.json().catch(() => ({}));
    if (res.status === 401) { showLock(SESSION_ENDED); return; }
    if (!res.ok) { showPreviewError(res, body, "Approve failed"); return; }
    showPreviewView();
    setPreviewStatus("Approved. Re-index to update search.", "");
    const live = await fetch("/api/docs/" + encPath(name));
    if (gen !== previewGen || name !== previewName) return;
    const liveBody = await live.json().catch(() => ({}));
    if (live.ok) {
      previewLive = typeof liveBody.body === "string" ? liveBody.body : "";
      previewMeta.textContent = "View only";
      previewEdit.hidden = false;
      previewBody.innerHTML = renderMarkdown(previewLive, { sourceFile: name, mermaid: "draw" });
      drawAnswer(previewBody);
    }
    checkRebuildOnLoad();
  } catch (err) {
    if (gen === previewGen) setPreviewStatus(OFFLINE_MSG, "err");
  } finally {
    previewBusy = false;
    syncPreviewEditLock();
  }
});

previewEdit.addEventListener("click", () => { startPreviewEdit(); });
```

A 409 whose detail is `The live file changed since this draft was staged.` is not a rebuild. `showPreviewError` only calls `setRebuildLock(true)` when the message contains `rebuilding`. Show the conflict sentence in `#preview-status` and leave the textarea in place.

A 422 from Format leaves the textarea on the text just saved. The server leaves that proposal as the hand edit. Status is the response detail (on the verify copy, `No model is configured.`).

- [ ] **Step 6: Docs and cache**

`src/static/sw.js`: `const CACHE = "ask-northwind-v22";`

`AGENTS.md` key-files bullet: `ask-northwind-v22`.

`AGENTS.md` Library and Re-index paragraph, replace:

`The file name opens a read-only view (`GET /api/docs/{path}` for Markdown, `GET /api/media/{path}` for an image or PDF).`

with:

`The file name opens that file fullscreen (`GET /api/docs/{path}` for Markdown, `GET /api/media/{path}` for an image or PDF). Markdown can be edited in that dialog. Save draft and Format write a review proposal. Approve writes the live file. Search still changes only on Re-index. An image or PDF stays view-only.`

`src/README.md`, replace `Opening a source file shows that file read-only.` with `Opening a source file shows that file fullscreen. Markdown can be edited there; Save draft stages a review proposal, and Approve writes the live file. Search still changes only on Re-index.`

`src/LEARNING.md`, replace `The file name opens that markdown read-only.` with `The file name opens that file fullscreen. Markdown can be edited in the dialog through the review queue. An image or PDF stays view-only.`

Do not edit files under `docs/superpowers/specs/`.

- [ ] **Step 7: Prove edit, the gate, and the refusals**

Launch, doctor, and unlock the isolated app the same way as Task 1. Drive only `http://127.0.0.1:8765`. Record proof under `/tmp/ask-northwind-verify/evidence/ask-fullscreen-edit/`.

Desktop viewport first (1280×800), then repeat the open-and-edit click at 390×844.

1. Open `/?doc=pto_policy.md`. Dialog is fullscreen. `#preview-edit` is visible and reads `Edit`. `#preview-draft` is hidden.
2. Click Edit. `#preview-draft` is visible, its value starts with the live heading (`#`), and `#preview-body` is hidden. The footer shows Format, Save draft, Approve, Reload live file, and Done.
3. Append a unique line `Ask edit probe.` Save draft. Status is `Draft saved. Approve to write the live file.` `GET /api/review` on the copy includes an upsert for `pto_policy.md` whose body contains that line. `GET /api/docs/pto_policy.md` does not contain that line. The file under `/tmp/ask-northwind-verify/app/docs/pto_policy.md` does not contain it either.
4. Click Approve. Status is `Approved. Re-index to update search.` The dialog returns to the rendered note and the new line is visible. `GET /api/docs/pto_policy.md` contains the line. `#index-note` is visible and links to `/library`. No request in the network log is `POST /api/reindex`.
5. Format without a model: Edit again, click Format. Status is `No model is configured.` The textarea keeps the saved text. The live file is unchanged from the last approve.
6. Reload: change the textarea without saving, click Reload live file. The textarea matches the live file. Status is `Loaded the live file. Save draft to keep it.`
7. Dirty guard: type `unsaved` in the textarea. Stub `window.confirm` to return `false`. Press Escape. The dialog stays open and the textarea still contains `unsaved`. Stub `confirm` to return `true`. Press Escape. The dialog closes.
8. Pending delete: from the unlocked page, `fetch("/api/docs/pto_policy.md", { method: "DELETE" })`. Open the note, click Edit. Status is `Reject the pending delete in Library before editing this file.` `#preview-draft` stays hidden. `GET /api/review` still shows `op: "delete"`. Then `fetch("/api/review/pto_policy.md", { method: "DELETE" })` so the proposal is gone. Do not Approve the delete.
9. Pending rename: from the unlocked page, `fetch("/api/review/pto_policy.md/rename", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ dest: "pto_policy_moved.md" }) })`. Click Edit. Status is `Finish or reject the pending move in Library before editing this file.` The proposal's `op` is still `rename`. Reject it with `fetch("/api/review/pto_policy.md", { method: "DELETE" })`.
10. Image: write `sample.png` into the copy's `docs/` as in Task 1. Open `/?doc=sample.png`. `#preview-edit` is hidden. `#preview-draft` is hidden.
11. Library regression: on `/library`, preview the note, click Edit in Library, and confirm `#draft` is the review textarea on that page. Do not Approve unless you are cleaning the probe line. Restore `pto_policy.md` on the copy if a later check needs the original text. The user's `src/docs/pto_policy.md` must be byte-identical to before this task.
12. Cleanup with `verify-ask cleanup`.

- [ ] **Step 8: Commit**

```bash
git add src/static/index.html src/static/app.css src/static/sw.js AGENTS.md src/README.md src/LEARNING.md
git commit -m "feat: edit a note from the Ask preview"
```

---

## Self-review

- Spec coverage: fullscreen Ask dialog is Task 1. In-page Markdown edit, Save draft, Format, Approve, and the live-file gate are Task 2. Images and PDFs stay view-only in Task 2. Library's editor is not replaced. No re-index call is added.
- Placeholder scan: every new function and the CSS block are written out. Format's expected verify-copy error is the real server string `No model is configured.`
- Consistency: `openLaunchDoc` runs from `applyLaunchDraft`. Edit mode uses `#preview-draft`, never Library's `#draft`. Status sentences match the steps that assert them. Cache name ends at `ask-northwind-v22`.
- Review focus: the five cases are the assertions in Task 2 Step 7 (items 3, 4, 8, 9, 10, and 7).

## Execution handoff

Plan file for this session is the plan that was just written. After approval, copy it to `docs/superpowers/plans/2026-11-16-ask-fullscreen-edit.md` as the first implementation step so the repo keeps it with the other plans. Do not commit that copy by itself if the feature commits already include it; one docs commit with Task 1 or Task 2 is enough.

Recommended execution: **Native**. The two tasks edit the same dialog, and Task 2's markup assumes Task 1's fullscreen selectors. A fresh subagent on Task 2 would re-learn the dialog and can easily drop the dirty guard or the Library-only CSS. One implementer, then one review of the branch, matches that coupling. Browser proof is on the isolated verify app, not port 8000.

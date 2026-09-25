# Multi-turn Ask

Ask is one question, one answer, then the next question erases both. A follow-up such as “what about contractors?” is searched as those three words, and the model never sees the PTO answer it is referring to. The page should hold a conversation: each new answer is written with the earlier questions and answers in view, search still hits the right policy, and New chat starts a fresh session.

The standard shape for this, used by history-aware retrievers (LlamaIndex condense-plus-context, and the conversational-RAG surveys that call it CQR), is two steps. First rewrite the follow-up into one standalone search question so the embedder is not handed a pronoun. Then generate the answer from the fresh excerpts plus the conversation. Stuffing the previous answer into the embedding is the worse version: the answer is long and pulls the vector off the user’s intent. Skipping the rewrite and only stuffing history into the prompt leaves retrieval on the raw follow-up, which is how “what about contractors?” misses PTO.

This demo keeps that shape visible. The first question is still searched exactly as typed. A follow-up shows the rewritten search line. There is no second HTML route, no server-side transcript, and no list of old chats.

## Decisions

- The browser holds one thread for the tab in `sessionStorage` under `ask-northwind-chat`. Reload keeps it. New chat deletes it. The server stores nothing. Two tabs are two sessions. CLI stays one question: `answer(question)` with no history.
- A turn is committed only when it finishes with answer text. That includes the explicit no-information sentence. A generation failure, a disconnect, or a rebuild error stays on screen and is not sent next time.
- The client sends `{ "question", "history" }` where `history` is completed pairs, `{"role": "user"|"assistant", "content"}`, and does not include the question being asked. `{ "cancel": true }` aborts the in-flight answer and does not start a new one.
- The socket reads the next message while an answer is running, so New chat can cancel. Sends are serialized with one lock. One answer at a time remains: a second question gets `already answering`.
- `normalize_history` drops non-objects, roles other than `user` and `assistant`, empty text, a leading assistant turn, and a trailing user turn. It keeps strict user/assistant pairs. Each message is clipped to `HISTORY_MESSAGE_CHARS` (1500). The kept list is at most `HISTORY_MAX_MESSAGES` (8, four pairs) and `HISTORY_MAX_CHARS` (6000), dropping oldest pairs first. The UI still shows the whole thread.
- Empty history retrieves the typed question. No rewrite call. `eval.py` calls `retrieve()` itself, so its three columns are unchanged.
- Non-empty history: `complete()` rewrites with `REWRITE_MAX_TOKENS` (80). The prompt says to fold in the referent, to leave a new topic unchanged, and to output only the question. `clean_rewrite` keeps the first line, strips one pair of wrapping quotes, and rejects empty or anything longer than `RETRIEVAL_QUERY_MAX` (400). Any failure, including a missing key or a truncation error, uses the fallback: the previous user question, a space, and the current question, clipped to 400 characters with the current question kept intact.
- The answer prompt gains a `Conversation so far` block of the normalized turns, then the same numbered excerpts and `Question:` line. Empty history omits the block, so the user message matches today’s single-turn prompt. `SYSTEM_PROMPT` tells the model that earlier `[n]` markers belong to earlier turns, and that this turn’s facts come only from the excerpts below. Answers still stream with `max_tokens` 500.
- The rewrite prompt receives that same normalized history, assistant turns included. The retrieving status includes `"query"` only when the search text differs from the typed question. The turn shows `Follow-up searched as: …`.
- Follow-up status stages are `rewriting` (“Reading the conversation…”) then `retrieving` (“Searching documents…”) then `generating` (“Writing answer…”). A first question skips `rewriting`. The no-information sentence, committed as the assistant turn, is exactly `I don't have information about that in the available documents.`
- `{ "cancel": true }` while nothing is running is ignored. Socket close clears the busy state, same as today, so a dead connection cannot leave Ask disabled. A reload mid-answer keeps only turns already committed.

## Ask page

The lede adds that a follow-up keeps this conversation and New chat starts over. The stale-index note stays.

The permanent Pipeline and Answer panels go away. The thread is a stack of turns. Each turn shows the question (label “You”), then “Northwind”, then an open Excerpts disclosure (ranking line, chunk cards, and the follow-up search line when present), then the markdown answer. Starting the next question closes older disclosures. The latest stays open. Restoring a session opens only the last one.

The composer sticks to the bottom of the viewport: status line, a textarea, Ask, New chat, and the connection state. Enter sends. Shift+Enter inserts a newline. The box clears after a send. Before any committed turn the placeholder is `How many PTO days do I get per year?`; after that it is `Ask a follow-up…`. New chat is disabled when the thread is empty. During an answer it stays available, sends cancel, clears the thread immediately, and Ask stays disabled until `{"type":"cancelled"}`. When nothing is running, New chat only clears the local thread.

User text, search lines, and chunk previews are `textContent`. Answer markdown still uses the existing escape-and-render path. Styles for the answer body move from `#answer` to `.turn .answer`, including `text-transform: none` on headings. Ask-only layout is scoped to `#ask-form` so Library is left alone. Service worker cache becomes `ask-northwind-v9`.

## What this does not do

No account, no server log, no sidebar of past conversations, no auto-reindex, no new route, no new dependency, no pytest. General query expansion (several searches, hypothetical documents) stays out. `retrieve()` itself is not changed. A topic change without New chat still works when the rewriter leaves the question alone, but the answer prompt can still see the recent window; New chat is the clean reset.

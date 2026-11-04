"""Rewrite one Markdown file into the shape the chunker can split.

The chunker only breaks on a `#` title and `##` headings. A document that
is one long block, or that hides its topics under `###`, becomes a sliding
window of unrelated sentences. This pass asks the same model Ask uses to
restore that outline without changing the facts. A result that is not that
outline is rejected, so a chatty or truncated reply never replaces a draft.
"""

from __future__ import annotations

import re

from llm import LLMConfigError, LLMTruncatedError, complete

FORMAT_MAX_TOKENS = 4096

FORMAT_SYSTEM = """You rewrite one internal company Markdown document so a heading-based chunker can split it.

Rules:
- Return only the Markdown document. No preamble.
- The first line is one title: "# Title".
- Use ## for every section. Do not use ### or deeper headings.
- Keep each ## section to one topic. If a section is longer than about 3000 characters, split it into further ## sections at a topic boundary.
- Do not add, remove, or change facts. Numbers, names, dates, durations, and conditions stay as written. Do not paraphrase sentences.
- You may add ## headings where the source has none. Heading text must be a label for the text already there, not a new claim.
- Keep lists as lists.
- Keep any token that starts with NWSHIELD exactly as written. Those stand for a diagram or an embedded file."""


_FENCE = re.compile(r"```mermaid[^\n]*\n[\s\S]*?```", re.IGNORECASE)
_EMBED = re.compile(r"!\[[^\]]*\]\([^)\s]+\)|!\[\[[^\]]+\]\]")


def shield_layout(text: str, rewrite) -> str:
    """Pull closed mermaid fences and embeds out, then splice the originals back.

    The formatter rewrites headings. A missing placeholder is a bad result,
    so the diagram the user wrote is not replaced by a paraphrase.
    """
    saved: list[str] = []

    def take(pattern: re.Pattern[str], source: str) -> str:
        def repl(match: re.Match[str]) -> str:
            saved.append(match.group(0))
            return f"\n\nNWSHIELD{len(saved) - 1}\n\n"

        return pattern.sub(repl, source)

    shielded = take(_EMBED, take(_FENCE, text))
    if not saved:
        return rewrite(text)
    rewritten = rewrite(shielded)
    for index, original in enumerate(saved):
        token = f"NWSHIELD{index}"
        if token not in rewritten:
            raise FormatError("Formatter dropped a diagram or an embedded file.")
        rewritten = rewritten.replace(token, original, 1)
    if "NWSHIELD" in rewritten:
        raise FormatError("Formatter dropped a diagram or an embedded file.")
    return rewritten


class FormatError(ValueError):
    """The model reply is not a single # / ## document."""


def validate_rag_markdown(text: str) -> str:
    cleaned = (text or "").strip()
    fence = re.match(
        r"^```(?:markdown|md)?\s*\n(.*)\n```$",
        cleaned,
        re.DOTALL | re.IGNORECASE,
    )
    if fence:
        cleaned = fence.group(1).strip()
    if not cleaned:
        raise FormatError("Formatter returned an empty document.")
    lines = cleaned.splitlines()
    content = [line for line in lines if line.strip()]
    if not content or not content[0].startswith("# ") or content[0].startswith("##"):
        raise FormatError("Formatter result must start with a single # title.")
    titles = [
        line
        for line in lines
        if line.startswith("# ") and not line.startswith("##")
    ]
    if len(titles) != 1:
        raise FormatError("Formatter result must contain exactly one # title.")
    if any(line.startswith("###") for line in lines):
        raise FormatError("Formatter result must not use ### headings.")
    if not cleaned.endswith("\n"):
        cleaned += "\n"
    return cleaned


def format_markdown(text: str) -> str:
    def rewrite(body: str) -> str:
        try:
            raw = complete(FORMAT_SYSTEM, body, max_tokens=FORMAT_MAX_TOKENS)
        except LLMTruncatedError as exc:
            raise FormatError("Formatting hit the token limit.") from exc
        except LLMConfigError:
            raise
        return validate_rag_markdown(raw)

    return shield_layout(text, rewrite)


if __name__ == "__main__":
    import sys

    source = sys.stdin.read() if len(sys.argv) < 2 else open(sys.argv[1], encoding="utf-8").read()
    try:
        sys.stdout.write(format_markdown(source))
    except (FormatError, LLMConfigError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from exc

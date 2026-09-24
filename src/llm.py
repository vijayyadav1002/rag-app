"""
LLM transport only: turn (system, user) into text.

Generation policy (citations, "I don't know") lives in generate.py.
This file is the vendor boundary so the RAG prompt does not care whether
the model is Claude, a cloud OpenAI-compatible API, or Ollama on localhost.

Two wire formats, not a catalog of SDKs:
- anthropic — Anthropic Messages API
- openai    — Chat Completions (OpenAI, Ollama, vLLM, LM Studio, Groq,
              OpenRouter, xAI, …). LLM_BASE_URL points at any of them.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from dotenv import load_dotenv

# Shell exports take precedence (override=False). Missing file is a no-op.
load_dotenv(Path(__file__).parent / ".env")

MAX_TOKENS = 500

# Preset name → driver + defaults. "openai" means the protocol, not the company.
PROVIDERS = {
    "anthropic": {
        "driver": "anthropic",
        "base_url": None,
        "key_envs": ("LLM_API_KEY", "ANTHROPIC_API_KEY"),
        "default_model": "claude-haiku-4-5",
        "default_key": None,
    },
    "openai": {
        "driver": "openai",
        "base_url": "https://api.openai.com/v1",
        "key_envs": ("LLM_API_KEY", "OPENAI_API_KEY"),
        "default_model": "gpt-4o-mini",
        "default_key": None,
    },
    "ollama": {
        "driver": "openai",
        "base_url": "http://127.0.0.1:11434/v1",
        "key_envs": ("LLM_API_KEY",),
        "default_model": "llama3.2",
        "default_key": "ollama",
    },
    "xai": {
        "driver": "openai",
        "base_url": "https://api.x.ai/v1",
        "key_envs": ("LLM_API_KEY", "XAI_API_KEY"),
        "default_model": "grok-4.5",
        "default_key": None,
    },
}


class LLMConfigError(Exception):
    """Missing provider/key/model — fix env vars, don't retry."""


class LLMTruncatedError(Exception):
    """The model stopped because it hit max_tokens."""


@dataclass(frozen=True)
class Settings:
    provider: str
    driver: str
    model: str
    api_key: str
    base_url: str | None


def _cancelled(cancel: Event | None) -> bool:
    return cancel is not None and cancel.is_set()


def load_settings() -> Settings:
    raw = (os.environ.get("LLM_PROVIDER") or "").strip().lower()
    if not raw:
        if os.environ.get("ANTHROPIC_API_KEY"):
            raw = "anthropic"
        else:
            raise LLMConfigError(
                "Set LLM_PROVIDER to anthropic, openai, ollama, or xai "
                "(or set ANTHROPIC_API_KEY to keep the previous default)."
            )
    if raw not in PROVIDERS:
        known = ", ".join(sorted(PROVIDERS))
        raise LLMConfigError(f"Unknown LLM_PROVIDER={raw!r}. Use one of: {known}.")

    spec = PROVIDERS[raw]
    model = (os.environ.get("LLM_MODEL") or spec["default_model"]).strip()
    if not model:
        raise LLMConfigError("LLM_MODEL is empty.")

    api_key = ""
    for env_name in spec["key_envs"]:
        api_key = (os.environ.get(env_name) or "").strip()
        if api_key:
            break
    if not api_key and spec["default_key"]:
        api_key = spec["default_key"]
    if not api_key:
        needed = " or ".join(spec["key_envs"])
        raise LLMConfigError(f"No API key set. Export {needed}.")

    base_url = (os.environ.get("LLM_BASE_URL") or "").strip() or spec["base_url"]
    return Settings(
        provider=raw,
        driver=spec["driver"],
        model=model,
        api_key=api_key,
        base_url=base_url,
    )


def complete(system: str, user: str, max_tokens: int | None = None) -> str:
    settings = load_settings()
    limit = MAX_TOKENS if max_tokens is None else max_tokens
    if settings.driver == "anthropic":
        return _anthropic_complete(settings, system, user, limit)
    return _openai_complete(settings, system, user, limit)


def stream(
    system: str, user: str, cancel: Event | None = None
) -> Iterator[str]:
    settings = load_settings()
    if settings.driver == "anthropic":
        yield from _anthropic_stream(settings, system, user, cancel)
        return
    yield from _openai_stream(settings, system, user, cancel)


def _anthropic_client(settings: Settings):
    from anthropic import Anthropic

    kwargs: dict = {"api_key": settings.api_key}
    if settings.base_url:
        kwargs["base_url"] = settings.base_url
    return Anthropic(**kwargs)


def _openai_client(settings: Settings):
    from openai import OpenAI

    return OpenAI(api_key=settings.api_key, base_url=settings.base_url)


def _anthropic_complete(settings: Settings, system: str, user: str, max_tokens: int) -> str:
    response = _anthropic_client(settings).messages.create(
        model=settings.model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if response.stop_reason == "max_tokens":
        raise LLMTruncatedError("Formatting hit the token limit.")
    return response.content[0].text


def _anthropic_stream(
    settings: Settings, system: str, user: str, cancel: Event | None
) -> Iterator[str]:
    with _anthropic_client(settings).messages.stream(
        model=settings.model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": user}],
    ) as stream_cm:
        for text in stream_cm.text_stream:
            if _cancelled(cancel):
                return
            if text:
                yield text


def _openai_complete(settings: Settings, system: str, user: str, max_tokens: int) -> str:
    response = _openai_client(settings).chat.completions.create(
        model=settings.model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    choice = response.choices[0]
    if choice.finish_reason == "length":
        raise LLMTruncatedError("Formatting hit the token limit.")
    return choice.message.content or ""


def _openai_stream(
    settings: Settings, system: str, user: str, cancel: Event | None
) -> Iterator[str]:
    stream_cm = _openai_client(settings).chat.completions.create(
        model=settings.model,
        max_tokens=MAX_TOKENS,
        stream=True,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    for chunk in stream_cm:
        if _cancelled(cancel):
            return
        choice = chunk.choices[0] if chunk.choices else None
        text = choice.delta.content if choice is not None else None
        if text:
            yield text


if __name__ == "__main__":
    try:
        s = load_settings()
    except LLMConfigError as exc:
        print(f"config error: {exc}")
        raise SystemExit(1) from exc
    print(f"provider={s.provider} driver={s.driver} model={s.model}")
    print(f"base_url={s.base_url or '(sdk default)'}")
    print(f"api_key_set={'yes' if s.api_key else 'no'}")

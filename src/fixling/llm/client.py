"""LM Studio client: schema-constrained decisions, parallel sampling, embeddings (spec E2).

Every LLM decision in Fixling goes through :meth:`LLM.structured`: a fresh short prompt, a pydantic schema
enforced by LM Studio's constrained decoding, one repair retry, and an ``llm_call`` event for the audit log.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Protocol, TypeVar

import numpy as np
import openai
from pydantic import BaseModel, ValidationError

from fixling.config import ModelsCfg
from fixling.errors import LLMFormatError, LLMUnavailable, PromptTooLarge
from fixling.events.store import EventStore
from fixling.runs import RunStore

T = TypeVar("T", bound=BaseModel)
Messages = list[dict[str, str]]

CHARS_PER_TOKEN = 3.5
EMBED_BATCH = 64
BACKOFF_S = (1.0, 2.0, 4.0)
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"^\s*<think>.*\Z", re.DOTALL | re.IGNORECASE)
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL | re.IGNORECASE)


class LLMLike(Protocol):
    """What graph nodes depend on; implemented by :class:`LLM` and the test ``FakeLLM``."""

    def structured(
        self,
        run_id: str,
        node: str,
        schema: type[T],
        messages: Messages,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        seed: int | None = None,
    ) -> T: ...

    def sample_n(
        self, run_id: str, node: str, schema: type[T], messages: Messages, temperatures: list[float]
    ) -> list[T | Exception]: ...

    def embed(self, texts: list[str]) -> np.ndarray: ...

    def count_tokens(self, text: str) -> int: ...

    def health(self) -> None: ...


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN) + 1


def strip_reasoning(content: str) -> str:
    """Remove inline ``<think>`` blocks (and an unterminated one) from a completion."""
    content = _THINK_BLOCK.sub("", content)
    content = _THINK_OPEN.sub("", content)
    return content.strip()


def extract_json(content: str) -> str:
    """Tolerate a markdown fence around otherwise valid JSON."""
    m = _FENCE.match(content)
    return m.group(1) if m else content


def apply_thinking_switch(messages: Messages, style: str, thinking: bool) -> Messages:
    """Per-model hook: Qwen3-style models toggle reasoning with ``/think`` or ``/no_think`` in the user turn."""
    if style != "qwen3" or not messages:
        return messages
    out = [dict(m) for m in messages]
    for m in reversed(out):
        if m["role"] == "user":
            m["content"] = f"{m['content']}\n/{'think' if thinking else 'no_think'}"
            break
    return out


class LLM:
    def __init__(
        self,
        cfg: ModelsCfg,
        events: EventStore | None = None,
        runs: RunStore | None = None,
        *,
        budget_check: Callable[[str], None] | None = None,
        client: openai.OpenAI | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.events = events
        self.runs = runs
        self.budget_check = budget_check
        self._sleep = sleep
        self.client = client or openai.OpenAI(
            base_url=cfg.lmstudio.base_url,
            api_key=cfg.lmstudio.api_key,
            timeout=cfg.lmstudio.timeout_s,
            max_retries=0,
        )

    # ------------------------------------------------------------------ health
    def health(self) -> None:
        try:
            ids = {m.id for m in self.client.models.list().data}
        except (openai.APIConnectionError, openai.APITimeoutError) as exc:
            raise LLMUnavailable(
                f"LM Studio is not reachable at {self.cfg.lmstudio.base_url}. Run: lms server start --port 1234"
            ) from exc
        missing = [m for m in (self.cfg.main_model, self.cfg.embed_model) if m not in ids]
        if missing:
            hints = "; ".join(f"lms load <model-key> --identifier {m}" for m in missing)
            raise LLMUnavailable(f"Models not loaded in LM Studio: {', '.join(missing)}. Run: {hints}")

    # ------------------------------------------------------------------ chat
    def count_tokens(self, text: str) -> int:
        return estimate_tokens(text)

    def _call_with_retries(self, **kwargs: Any) -> Any:
        """Connection errors / 5xx: back off 1, 2, 4 s. These never count as node attempts."""
        last: Exception | None = None
        for attempt in range(len(BACKOFF_S) + 1):
            try:
                return self.client.chat.completions.create(**kwargs)
            except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as exc:
                last = exc
                if attempt < len(BACKOFF_S):
                    self._sleep(BACKOFF_S[attempt])
        assert last is not None
        raise LLMUnavailable(
            f"LM Studio request failed after {len(BACKOFF_S) + 1} tries: {last}. Is the server running (lms ps)?"
        ) from last

    def structured(
        self,
        run_id: str,
        node: str,
        schema: type[T],
        messages: Messages,
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
        seed: int | None = None,
    ) -> T:
        node_cfg = self.cfg.node(node)
        temp = temperature if temperature is not None else (node_cfg.temp_default())
        max_tok = max_tokens if max_tokens is not None else node_cfg.max_tokens
        messages = apply_thinking_switch(messages, self.cfg.thinking_style, node_cfg.thinking)

        prompt_tokens_est = estimate_tokens("".join(m["content"] for m in messages))
        if prompt_tokens_est > self.cfg.max_prompt_tokens:
            raise PromptTooLarge(
                f"{node}: prompt ~{prompt_tokens_est} tokens exceeds max_prompt_tokens={self.cfg.max_prompt_tokens}"
            )

        response_format = {
            "type": "json_schema",
            "json_schema": {"name": schema.__name__, "strict": True, "schema": schema.model_json_schema()},
        }
        convo = list(messages)
        error_note = ""
        for attempt in (1, 2):
            if self.budget_check:
                self.budget_check(run_id)
            started = time.monotonic()
            kwargs: dict[str, Any] = {
                "model": self.cfg.main_model,
                "messages": convo,
                "temperature": temp,
                "max_tokens": max_tok,
                "response_format": response_format,
            }
            if seed is not None:
                kwargs["seed"] = seed
            resp = self._call_with_retries(**kwargs)
            latency_ms = int((time.monotonic() - started) * 1000)
            choice = resp.choices[0]
            raw = choice.message.content or ""
            reasoning = getattr(choice.message, "reasoning", None) or getattr(choice.message, "reasoning_content", None)
            usage = getattr(resp, "usage", None)
            tokens_in = getattr(usage, "prompt_tokens", None) or prompt_tokens_est
            tokens_out = getattr(usage, "completion_tokens", None) or estimate_tokens(raw)
            content = extract_json(strip_reasoning(raw))

            parsed: T | None = None
            error: str | None = None
            if choice.finish_reason == "length":
                error = f"output was cut off at max_tokens={max_tok}"
            try:
                parsed = schema.model_validate_json(content)
                error = None
            except (ValidationError, ValueError) as exc:
                error = error or str(exc).splitlines()[0][:300]

            self._record(
                run_id,
                node,
                temp,
                attempt,
                parsed is not None,
                latency_ms,
                tokens_in,
                tokens_out,
                convo,
                raw,
                reasoning,
                error,
            )
            if parsed is not None:
                return parsed
            error_note = error or "unknown error"
            # One repair retry: show the model its own invalid output and the validation error.
            convo = [
                *messages,
                {"role": "assistant", "content": raw[:2000]},
                {
                    "role": "user",
                    "content": f"Your previous output was invalid JSON for the schema: {error_note}. "
                    "Return only valid JSON.",
                },
            ]
        raise LLMFormatError(f"{node}: model output did not match {schema.__name__}: {error_note}")

    def _record(
        self,
        run_id: str,
        node: str,
        temperature: float,
        attempt: int,
        ok: bool,
        latency_ms: int,
        tokens_in: int,
        tokens_out: int,
        prompt: Messages,
        raw: str,
        reasoning: str | None,
        error: str | None,
    ) -> None:
        if self.runs:
            self.runs.add_usage(run_id, tokens_in, tokens_out, latency_ms)
        if not self.events:
            return
        text = (
            "### PROMPT\n" + "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in prompt) + "\n\n### RESPONSE\n" + raw
        )
        if reasoning:
            text += "\n\n### REASONING (not fed back)\n" + str(reasoning)
        self.events.append(
            run_id,
            "llm_call",
            "llm",
            {
                "node": node,
                "model": self.cfg.main_model,
                "temperature": temperature,
                "attempt": attempt,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "latency_ms": latency_ms,
                "success": ok,
                "error": error,
            },
            node=node,
            artifact_text=text,
        )

    def sample_n(
        self, run_id: str, node: str, schema: type[T], messages: Messages, temperatures: list[float]
    ) -> list[T | Exception]:
        """N independent samples at different temperatures, ``max_parallel`` at a time (continuous batching)."""

        def one(t: float) -> T | Exception:
            try:
                return self.structured(run_id, node, schema, messages, temperature=t)
            except Exception as exc:  # returned to the caller, which decides per candidate
                return exc

        with ThreadPoolExecutor(max_workers=max(1, self.cfg.lmstudio.max_parallel)) as pool:
            return list(pool.map(one, temperatures))

    # ------------------------------------------------------------------ embeddings
    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, 0), dtype=np.float32)
        vectors: list[list[float]] = []
        for i in range(0, len(texts), EMBED_BATCH):
            batch = texts[i : i + EMBED_BATCH]
            resp = self._embed_batch(batch)
            vectors.extend(d.embedding for d in sorted(resp.data, key=lambda d: d.index))
        arr = np.asarray(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        return np.asarray(arr / np.where(norms == 0, 1.0, norms), dtype=np.float32)

    def _embed_batch(self, batch: list[str]) -> Any:
        last: Exception | None = None
        for attempt in range(len(BACKOFF_S) + 1):
            try:
                return self.client.embeddings.create(model=self.cfg.embed_model, input=batch)
            except (openai.APIConnectionError, openai.APITimeoutError, openai.InternalServerError) as exc:
                last = exc
                if attempt < len(BACKOFF_S):
                    self._sleep(BACKOFF_S[attempt])
        raise LLMUnavailable(f"LM Studio embeddings failed: {last}") from last


__all__ = ["LLM", "LLMLike", "estimate_tokens", "extract_json", "strip_reasoning"]

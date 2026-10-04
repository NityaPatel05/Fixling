from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from fixling.config import Config
from fixling.db import Database
from fixling.errors import BudgetExceeded, LLMFormatError, LLMUnavailable, PromptTooLarge
from fixling.events.store import EventStore
from fixling.llm.client import LLM, apply_thinking_switch, extract_json, strip_reasoning
from fixling.models import FileRanking
from fixling.runs import RunStore
from tests.fakes import Router, Seq, chat_body, connect_error

GOOD = json.dumps({"files": ["a.py"], "reason": "because"})
MSGS = [{"role": "system", "content": "sys"}, {"role": "user", "content": "rank files"}]


class Harness:
    def __init__(self, cfg: Config, tmp: Path, routes: dict[str, Any]) -> None:
        self.db = Database(tmp / "t.sqlite")
        self.events = EventStore(self.db, tmp / "runs", observation_chars=10_000)
        self.runs = RunStore(self.db)
        self.run_id = self.runs.create_run(repo="o/r") or ""
        self.sleeps: list[float] = []
        self.llm = LLM(cfg.models, self.events, self.runs, client=Router(routes).client(), sleep=self.sleeps.append)


def make(cfg: Config, tmp_path: Path, chat: Seq | None = None, **routes: Any) -> Harness:
    all_routes = dict(routes)
    if chat is not None:
        all_routes["/chat/completions"] = chat
    return Harness(cfg, tmp_path, all_routes)


def test_request_uses_json_schema_and_node_sampling(cfg: Config, tmp_path: Path) -> None:
    chat = Seq(chat_body(GOOD))
    h = make(cfg, tmp_path, chat)
    out = h.llm.structured(h.run_id, "localize", FileRanking, MSGS)
    assert out == FileRanking(files=["a.py"], reason="because")
    body = chat.bodies()[0]
    assert body["model"] == "fixling-main"
    assert body["temperature"] == 0.1 and body["max_tokens"] == 600  # config/models.yaml localize node
    rf = body["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["name"] == "FileRanking"
    assert set(rf["json_schema"]["schema"]["properties"]) == {"files", "reason"}
    assert body["messages"] == MSGS


def test_explicit_overrides_and_seed(cfg: Config, tmp_path: Path) -> None:
    chat = Seq(chat_body(GOOD))
    h = make(cfg, tmp_path, chat)
    h.llm.structured(h.run_id, "localize", FileRanking, MSGS, temperature=0.9, max_tokens=50, seed=7)
    body = chat.bodies()[0]
    assert (body["temperature"], body["max_tokens"], body["seed"]) == (0.9, 50, 7)


def test_think_block_and_code_fence_are_stripped(cfg: Config, tmp_path: Path) -> None:
    wrapped = "<think>let me think {not json}</think>\n```json\n" + GOOD + "\n```"
    h = make(cfg, tmp_path, Seq(chat_body(wrapped, reasoning="hidden chain of thought")))
    assert h.llm.structured(h.run_id, "localize", FileRanking, MSGS).files == ["a.py"]


def test_strip_helpers() -> None:
    assert strip_reasoning("<think>a</think>X") == "X"
    assert strip_reasoning("<think>never closed {") == ""
    assert extract_json("```\n{}\n```") == "{}"
    assert extract_json('{"a": 1}') == '{"a": 1}'


def test_invalid_json_gets_one_repair_retry_with_error_feedback(cfg: Config, tmp_path: Path) -> None:
    chat = Seq(chat_body("not json at all"), chat_body(GOOD))
    h = make(cfg, tmp_path, chat)
    out = h.llm.structured(h.run_id, "localize", FileRanking, MSGS)
    assert out.files == ["a.py"] and chat.call_count == 2
    retry = chat.bodies()[1]["messages"]
    assert retry[:2] == MSGS
    assert retry[-2] == {"role": "assistant", "content": "not json at all"}
    assert "invalid JSON for the schema" in retry[-1]["content"] and "Return only valid JSON" in retry[-1]["content"]


def test_two_invalid_outputs_raise_format_error(cfg: Config, tmp_path: Path) -> None:
    chat = Seq(chat_body('{"files": "nope"}'))
    h = make(cfg, tmp_path, chat)
    with pytest.raises(LLMFormatError):
        h.llm.structured(h.run_id, "localize", FileRanking, MSGS)
    assert chat.call_count == 2


def test_truncated_output_is_a_format_failure(cfg: Config, tmp_path: Path) -> None:
    h = make(cfg, tmp_path, Seq(chat_body(GOOD[:20], finish="length")))
    with pytest.raises(LLMFormatError, match="cut off"):
        h.llm.structured(h.run_id, "localize", FileRanking, MSGS)


def test_prompt_too_large_never_calls_the_server(cfg: Config, tmp_path: Path) -> None:
    chat = Seq(chat_body(GOOD))
    h = make(cfg, tmp_path, chat)
    with pytest.raises(PromptTooLarge):
        h.llm.structured(h.run_id, "localize", FileRanking, [{"role": "user", "content": "x" * 40_000}])
    assert chat.call_count == 0


def test_connection_errors_back_off_then_succeed(cfg: Config, tmp_path: Path) -> None:
    chat = Seq(connect_error, httpx2.Response(503, json={"error": "busy"}), chat_body(GOOD))
    h = make(cfg, tmp_path, chat)
    assert h.llm.structured(h.run_id, "localize", FileRanking, MSGS).files == ["a.py"]
    assert h.sleeps == [1.0, 2.0]


def test_connection_errors_exhausted_raise_unavailable(cfg: Config, tmp_path: Path) -> None:
    chat = Seq(connect_error)
    h = make(cfg, tmp_path, chat)
    with pytest.raises(LLMUnavailable, match="lms ps"):
        h.llm.structured(h.run_id, "localize", FileRanking, MSGS)
    assert chat.call_count == 4 and h.sleeps == [1.0, 2.0, 4.0]


def test_llm_call_event_and_usage_recorded(cfg: Config, tmp_path: Path) -> None:
    h = make(cfg, tmp_path, Seq(chat_body(GOOD, reasoning="secret reasoning")))
    h.llm.structured(h.run_id, "localize", FileRanking, MSGS)
    (ev,) = h.events.list(h.run_id)
    assert ev.type == "llm_call" and ev.source == "llm" and ev.node == "localize"
    assert ev.payload["tokens_in"] == 120 and ev.payload["tokens_out"] == 30 and ev.payload["success"] is True
    assert "rank files" in ev.payload["text"] and GOOD in ev.payload["text"]
    assert "REASONING" in ev.payload["text"]  # logged for audit, never fed back
    assert h.runs.usage(h.run_id)[:2] == (120, 30)


def test_budget_check_runs_before_each_call(cfg: Config) -> None:
    def stop(run_id: str) -> None:
        raise BudgetExceeded("wall clock")

    chat = Seq(chat_body(GOOD))
    llm = LLM(cfg.models, budget_check=stop, client=Router({"/chat/completions": chat}).client())
    with pytest.raises(BudgetExceeded):
        llm.structured("r", "localize", FileRanking, MSGS)
    assert chat.call_count == 0


def test_sample_n_preserves_order_and_returns_errors(cfg: Config, tmp_path: Path) -> None:
    def respond(request: httpx2.Request) -> dict[str, Any]:
        t = json.loads(request.content)["temperature"]
        if t == 0.5:
            return chat_body("garbage")
        return chat_body(json.dumps({"files": [f"t{t}.py"], "reason": "r"}))

    h = make(cfg, tmp_path, Seq(respond))
    res = h.llm.sample_n(h.run_id, "repair", FileRanking, MSGS, [0.2, 0.5, 0.9])
    assert isinstance(res[0], FileRanking) and res[0].files == ["t0.2.py"]
    assert isinstance(res[1], LLMFormatError)
    assert isinstance(res[2], FileRanking) and res[2].files == ["t0.9.py"]


def models_body(*ids: str) -> dict[str, Any]:
    return {"object": "list", "data": [{"id": i, "object": "model"} for i in ids]}


def test_health_missing_models_has_actionable_hint(cfg: Config, tmp_path: Path) -> None:
    h = make(cfg, tmp_path, **{"/models": Seq(models_body("fixling-main"))})
    with pytest.raises(LLMUnavailable, match="fixling-embed") as ei:
        h.llm.health()
    assert "lms load" in str(ei.value)


def test_health_ok(cfg: Config, tmp_path: Path) -> None:
    make(cfg, tmp_path, **{"/models": Seq(models_body("fixling-main", "fixling-embed"))}).llm.health()


def test_health_server_down(cfg: Config, tmp_path: Path) -> None:
    h = make(cfg, tmp_path, **{"/models": Seq(connect_error)})
    with pytest.raises(LLMUnavailable, match="lms server start"):
        h.llm.health()


def test_embed_batches_and_normalises(cfg: Config, tmp_path: Path) -> None:
    def respond(request: httpx2.Request) -> dict[str, Any]:
        inputs = json.loads(request.content)["input"]
        data = [
            {"object": "embedding", "index": i, "embedding": [3.0, 4.0, float(len(t))]} for i, t in enumerate(inputs)
        ]
        return {
            "object": "list",
            "model": "fixling-embed",
            "data": data,
            "usage": {"prompt_tokens": 1, "total_tokens": 1},
        }

    emb = Seq(respond)
    h = make(cfg, tmp_path, **{"/embeddings": emb})
    vecs = h.llm.embed(["a" * (i % 5) for i in range(130)])
    assert emb.call_count == 3  # 64 + 64 + 2
    assert vecs.shape == (130, 3)
    assert abs(float((vecs**2).sum(axis=1).max()) - 1.0) < 1e-5
    assert h.llm.embed([]).shape[0] == 0


def test_thinking_switch_only_for_qwen3_style() -> None:
    msgs = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]
    assert apply_thinking_switch(msgs, "none", True) == msgs
    assert apply_thinking_switch(msgs, "qwen3", False)[-1]["content"] == "u\n/no_think"
    assert apply_thinking_switch(msgs, "qwen3", True)[-1]["content"] == "u\n/think"
    assert msgs[-1]["content"] == "u"  # input not mutated

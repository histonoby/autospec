from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from autospec.config import Settings
from autospec.core.context import ArtifactStore, CancelToken, JobCancelled
from autospec.core.events import EventEmitter, MemorySink
from autospec.core.llm import (
    ContextBudgetExceeded,
    FakeLLMClient,
    FakeLLMMismatch,
    LLMInfraError,
    OllamaClient,
    SchemaValidationFailed,
    compress_for_retry,
    retry_temperature,
)
from autospec.core.schemas import TestVerdict

VERDICT = TestVerdict(test_name="t", suspect=False, req_id="REQ-001", reason="ok")


def _bound(client: Any, tmp_path: Path) -> tuple[Any, MemorySink]:
    mem = MemorySink()
    ev = EventEmitter("j1", [mem])
    ev.current_step = "s"
    client.bind(ev, ArtifactStore(tmp_path / "art", ev), CancelToken())
    return client, mem


def test_no_think_suffix_added_once(tmp_path: Path) -> None:
    llm, _ = _bound(FakeLLMClient(Settings(), [("s", "a"), ("s", "b")]), tmp_path)
    llm.chat([{"role": "user", "content": "hi"}])
    llm.chat([{"role": "user", "content": "hi\n/no_think"}], think=False)
    assert llm.calls[0]["messages"][-1]["content"].endswith("/no_think")
    assert llm.calls[1]["messages"][-1]["content"].count("/no_think") == 1


def test_think_true_has_no_suffix(tmp_path: Path) -> None:
    llm, _ = _bound(FakeLLMClient(Settings(), [("s", "a")]), tmp_path)
    llm.chat([{"role": "user", "content": "hi"}], think=True)
    assert "/no_think" not in llm.calls[0]["messages"][-1]["content"]


def test_compress_keeps_only_four_messages() -> None:
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "SPEC"},
            {"role": "assistant", "content": "a1"}, {"role": "user", "content": "e1"},
            {"role": "assistant", "content": "a2"}, {"role": "user", "content": "e2"}]
    out = compress_for_retry(msgs, "a3", "e3")
    assert [m["content"] for m in out] == ["S", "SPEC", "a3", "e3"]


def test_retry_temperature_rises_and_caps() -> None:
    s = Settings()
    assert retry_temperature(s, 0.2, 0) == 0.2
    assert retry_temperature(s, 0.2, 1) == 0.35
    assert retry_temperature(s, 0.2, 10) == 0.7


def test_schema_retry_compresses_and_raises_temperature(tmp_path: Path) -> None:
    llm, mem = _bound(FakeLLMClient(Settings(), [("s", "not json"), ("s", '{"x": 1}'), ("s", VERDICT)]), tmp_path)
    out = llm.chat([{"role": "system", "content": "S"}, {"role": "user", "content": "SPEC"}], schema=TestVerdict)
    assert out == VERDICT
    assert [c["temperature"] for c in llm.calls] == [0.2, 0.35, 0.5]
    assert len(llm.calls[2]["messages"]) == 4 and llm.calls[2]["messages"][1]["content"].startswith("SPEC")
    assert len(mem.of_type("llm.request")) == 3
    assert len(list((tmp_path / "art" / "llm").glob("*.json"))) == 3


def test_schema_retry_gives_up_after_two_retries(tmp_path: Path) -> None:
    llm, _ = _bound(FakeLLMClient(Settings(), [("s", "x")] * 3), tmp_path)
    with pytest.raises(SchemaValidationFailed):
        llm.chat([{"role": "user", "content": "q"}], schema=TestVerdict)


def test_budget_exceeded_is_not_sent(tmp_path: Path) -> None:
    s = Settings()
    s.ollama.num_ctx = 9000
    llm, mem = _bound(FakeLLMClient(s, []), tmp_path)
    with pytest.raises(ContextBudgetExceeded):
        llm.chat([{"role": "user", "content": "x" * 10000}])
    assert llm.calls == [] and any(e.level == "warn" for e in mem.events)


def test_fake_llm_rejects_unexpected_step(tmp_path: Path) -> None:
    llm, _ = _bound(FakeLLMClient(Settings(), [("other", "a")]), tmp_path)
    with pytest.raises(FakeLLMMismatch):
        llm.chat([{"role": "user", "content": "q"}])


def test_cancel_checked_before_call(tmp_path: Path) -> None:
    llm, _ = _bound(FakeLLMClient(Settings(), [("s", "a")]), tmp_path)
    assert llm.cancel is not None
    llm.cancel.set()
    with pytest.raises(JobCancelled):
        llm.chat([{"role": "user", "content": "q"}])


# --- OllamaClient with a stubbed transport --------------------------------------------------------------

class _Transport:
    def __init__(self, replies: list[Any]) -> None:
        self.replies = replies
        self.payloads: list[dict[str, Any]] = []

    def __call__(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        self.payloads.append(json.loads(json.dumps(payload)))
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def _http_error(code: int, body: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(body.encode()))  # type: ignore[arg-type]


def test_ollama_payload_and_thinking_strip(tmp_path: Path) -> None:
    client, _ = _bound(OllamaClient(Settings()), tmp_path)
    t = _Transport([{"message": {"content": "<think>hmm</think>answer"}}])
    client._post = t  # type: ignore[method-assign]
    assert client.chat([{"role": "user", "content": "q"}], schema=None) == "answer"
    p = t.payloads[0]
    assert p["think"] is False and p["options"]["num_ctx"] == 65536 and p["stream"] is False
    assert p["messages"][-1]["content"].endswith("/no_think")


def test_ollama_schema_sends_format(tmp_path: Path) -> None:
    client, _ = _bound(OllamaClient(Settings()), tmp_path)
    t = _Transport([{"message": {"content": VERDICT.model_dump_json()}}])
    client._post = t  # type: ignore[method-assign]
    assert client.chat([{"role": "user", "content": "q"}], schema=TestVerdict) == VERDICT
    assert t.payloads[0]["format"]["title"] == "TestVerdict"


def test_ollama_think_unsupported_falls_back(tmp_path: Path) -> None:
    client, mem = _bound(OllamaClient(Settings()), tmp_path)
    t = _Transport([_http_error(400, '{"error":"model does not support thinking"}'), {"message": {"content": "ok"}}])
    client._post = t  # type: ignore[method-assign]
    assert client.chat([{"role": "user", "content": "q"}], think=True) == "ok"
    assert "think" not in t.payloads[1]


def test_ollama_connection_errors_retry_then_infra_error(tmp_path: Path) -> None:
    s = Settings()
    s.ollama.connect_retries = 3
    s.ollama.backoff_base_sec = 0
    client, _ = _bound(OllamaClient(s), tmp_path)
    t = _Transport([urllib.error.URLError("refused")] * 4)
    client._post = t  # type: ignore[method-assign]
    with pytest.raises(LLMInfraError):
        client.chat([{"role": "user", "content": "q"}])
    assert len(t.payloads) == 4


def test_ollama_missing_model_is_infra_error(tmp_path: Path) -> None:
    client, _ = _bound(OllamaClient(Settings()), tmp_path)
    client._post = _Transport([_http_error(404, '{"error":"model not found"}')])  # type: ignore[method-assign]
    with pytest.raises(LLMInfraError, match="モデル"):
        client.chat([{"role": "user", "content": "q"}])

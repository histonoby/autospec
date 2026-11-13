"""LLM clients (§6.4): OllamaClient and FakeLLMClient share retry / budget / logging logic."""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, ValidationError

from .textutil import estimate_tokens, strip_thinking

if TYPE_CHECKING:
    from ..config import Settings
    from .context import ArtifactStore, CancelToken
    from .events import EventEmitter
    from .registry import ResourceLocks

Message = dict[str, Any]


class LLMError(RuntimeError):
    pass


class ContextBudgetExceeded(LLMError):
    pass


class LLMInfraError(LLMError):
    """Ollama unreachable / model missing: not the code's fault (job error=infra_error)."""


class SchemaValidationFailed(LLMError):
    pass


class LLMClient(Protocol):
    def chat(self, messages: list[Message], *, think: bool = False,
             schema: type[BaseModel] | None = None, temperature: float | None = None,
             tools: list[dict[str, Any]] | None = None) -> Any: ...


def compress_for_retry(messages: Sequence[Message], last_response: str, error_text: str) -> list[Message]:
    """Keep only [system, first user, previous assistant, error user] (§6.4, I5)."""
    system = [m for m in messages if m.get("role") == "system"][:1]
    first_user = [m for m in messages if m.get("role") == "user"][:1]
    return [*system, *first_user, {"role": "assistant", "content": last_response},
            {"role": "user", "content": error_text}]


def retry_temperature(settings: Settings, base: float | None, attempt: int) -> float:
    b = settings.ollama.temperature if base is None else base
    p = settings.pipeline
    return round(min(b + p.retry_temperature_step * attempt, max(p.retry_temperature_max, b)), 3)


def _parse_json_model(schema: type[BaseModel], text: str) -> BaseModel:
    try:
        return schema.model_validate_json(text)
    except ValidationError:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m and m.group(0) != text:
            return schema.model_validate_json(m.group(0))
        raise


class BaseLLMClient:
    model_name = "base"

    def __init__(self, settings: Settings, events: EventEmitter | None = None,
                 artifacts: ArtifactStore | None = None, cancel: CancelToken | None = None,
                 locks: ResourceLocks | None = None) -> None:
        self.settings = settings
        self.events = events
        self.artifacts = artifacts
        self.cancel = cancel
        self.locks = locks
        self.schema_retries = 2

    def bind(self, events: EventEmitter, artifacts: ArtifactStore, cancel: CancelToken) -> None:
        self.events, self.artifacts, self.cancel = events, artifacts, cancel

    # -- overridable -------------------------------------------------------
    def _complete(self, messages: list[Message], *, think: bool, schema: type[BaseModel] | None,
                  temperature: float, tools: list[dict[str, Any]] | None) -> str:
        raise NotImplementedError

    # -- shared ----------------------------------------------------------------
    def _prepare(self, messages: Sequence[Message], think: bool) -> list[Message]:
        out = [dict(m) for m in messages]
        if not think and self.settings.ollama.no_think_suffix:
            for m in reversed(out):
                if m.get("role") == "user":
                    if not str(m.get("content", "")).rstrip().endswith("/no_think"):
                        m["content"] = str(m.get("content", "")).rstrip() + "\n/no_think"
                    break
        return out

    def budget_tokens(self) -> int:
        return self.settings.ollama.num_ctx - self.settings.pipeline.output_reserve_tokens

    def _check_budget(self, messages: Sequence[Message]) -> None:
        text = "".join(str(m.get("content", "")) for m in messages)
        tokens = estimate_tokens(text, self.settings.pipeline.chars_per_token)
        budget = self.budget_tokens()
        if tokens > budget:
            msg = f"コンテキスト予算超過のため送信しません（概算 {tokens} > 予算 {budget} トークン）"
            if self.events:
                self.events.warn(msg, tokens=tokens, budget=budget)
            raise ContextBudgetExceeded(msg)

    def _call(self, messages: list[Message], think: bool, schema: type[BaseModel] | None,
              temperature: float, tools: list[dict[str, Any]] | None) -> str:
        if self.cancel:
            self.cancel.check()
        self._check_budget(messages)
        log_name = self.artifacts.next_llm_log() if self.artifacts else None
        last_user = next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "")
        if self.events:
            self.events.emit("llm.request", f"LLM 呼び出し（think={think}, temp={temperature}）",
                             data={"preview": last_user[:500], "schema": schema.__name__ if schema else None,
                                   "log": log_name})
        started = time.monotonic()
        holder = self.events.job_id if self.events else "llm"
        if self.locks:
            self.locks.acquire(["llm"], timeout=self.settings.ollama.timeout_sec * 4, holder=holder)
        try:
            raw = self._complete(messages, think=think, schema=schema, temperature=temperature, tools=tools)
        finally:
            if self.locks:
                self.locks.release(holder, ["llm"])
        text = strip_thinking(raw) if "</think>" in raw else raw
        if self.artifacts and log_name:
            self.artifacts.write(log_name, {
                "model": self.model_name, "step": self.events.current_step if self.events else None,
                "think": think, "temperature": temperature, "schema": schema.__name__ if schema else None,
                "messages": messages, "response": raw, "elapsed_sec": round(time.monotonic() - started, 2),
            }, announce=False)
        if self.events:
            self.events.emit("llm.response", f"LLM 応答 {len(raw)} 文字",
                             data={"preview": text[:500], "log": log_name})
        return text

    def chat(self, messages: list[Message], *, think: bool = False,
             schema: type[BaseModel] | None = None, temperature: float | None = None,
             tools: list[dict[str, Any]] | None = None) -> Any:
        base_temp = self.settings.ollama.temperature if temperature is None else temperature
        convo = self._prepare(messages, think)
        if schema is None:
            return self._call(convo, think, None, base_temp, tools)
        last_err = ""
        for attempt in range(self.schema_retries + 1):
            temp = retry_temperature(self.settings, base_temp, attempt)
            text = self._call(convo, think, schema, temp, tools)
            try:
                return _parse_json_model(schema, text)
            except ValidationError as e:
                last_err = str(e)[:1500]
                if self.events:
                    self.events.warn(f"構造化出力の検証に失敗（{attempt + 1}/{self.schema_retries + 1}）",
                                     error=last_err[:500])
                convo = self._prepare(compress_for_retry(
                    convo, text,
                    "前回の出力は JSON スキーマの検証に失敗しました。エラー:\n" + last_err +
                    "\nスキーマに厳密に従った JSON だけを出力してください。"), think)
        raise SchemaValidationFailed(f"{schema.__name__}: {last_err}")


class OllamaClient(BaseLLMClient):
    def __init__(self, settings: Settings, **kw: Any) -> None:
        super().__init__(settings, **kw)
        self.model_name = settings.ollama.model
        self._send_think = settings.ollama.send_think

    def _post(self, path: str, payload: dict[str, Any], timeout: float) -> dict[str, Any]:
        req = urllib.request.Request(
            self.settings.ollama.url.rstrip("/") + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - configured local URL
            return json.loads(resp.read().decode("utf-8"))

    def _complete(self, messages: list[Message], *, think: bool, schema: type[BaseModel] | None,
                  temperature: float, tools: list[dict[str, Any]] | None) -> str:
        cfg = self.settings.ollama
        payload: dict[str, Any] = {
            "model": cfg.model, "messages": messages, "stream": False,
            "options": {"num_ctx": cfg.num_ctx, "temperature": temperature},
        }
        if self._send_think:
            payload["think"] = think
        if schema is not None:
            payload["format"] = schema.model_json_schema()
        if tools:
            payload["tools"] = tools
        attempts = max(1, cfg.connect_retries)
        for i in range(attempts + 1):
            if self.cancel:
                self.cancel.check()
            try:
                data = self._post("/api/chat", payload, cfg.timeout_sec)
                msg = data.get("message") or {}
                if tools and msg.get("tool_calls"):
                    return json.dumps({"tool_calls": msg["tool_calls"], "content": msg.get("content", "")})
                return str(msg.get("content", ""))
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")
                if e.code == 400 and "think" in body and "think" in payload:
                    payload.pop("think")
                    self._send_think = False
                    if self.events:
                        self.events.warn("モデルが think パラメータ非対応のため外して再送します")
                    continue
                if e.code == 404 or "not found" in body:
                    raise LLMInfraError(f"Ollama: モデルが見つかりません ({cfg.model}): {body[:300]}") from e
                if i >= attempts:
                    raise LLMInfraError(f"Ollama HTTP {e.code}: {body[:300]}") from e
            except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
                if i >= attempts:
                    raise LLMInfraError(f"Ollama に接続できません ({cfg.url}): {e}") from e
                if self.events:
                    self.events.warn(f"Ollama 接続エラー、再試行します（{i + 1}/{attempts}）: {e}")
            time.sleep(cfg.backoff_base_sec * (2 ** i))
        raise LLMInfraError("Ollama: retries exhausted")

    def health(self) -> dict[str, Any]:
        try:
            req = urllib.request.Request(self.settings.ollama.url.rstrip("/") + "/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310
                tags = json.loads(resp.read().decode("utf-8"))
            names = [m.get("name", "") for m in tags.get("models", [])]
            model = self.settings.ollama.model
            has = any(n == model or n.split(":")[0] == model or n == f"{model}:latest" for n in names)
            return {"ollama": True, "model": model, "model_available": has, "models": names}
        except Exception as e:  # noqa: BLE001
            return {"ollama": False, "error": str(e)}


class FakeLLMMismatch(BaseException):
    """Raised (as BaseException so steps cannot swallow it) when a test's script is violated."""


FakeResponse = str | BaseModel | Callable[[list[Message]], "str | BaseModel"]


class FakeLLMClient(BaseLLMClient):
    """Return pre-registered responses in order: [(expected_step, response), ...] (§20)."""

    model_name = "fake"

    def __init__(self, settings: Settings, script: Sequence[tuple[str, FakeResponse]] = (), **kw: Any) -> None:
        super().__init__(settings, **kw)
        self.script: list[tuple[str, FakeResponse]] = list(script)
        self.calls: list[dict[str, Any]] = []

    def add(self, step: str, response: FakeResponse) -> FakeLLMClient:
        self.script.append((step, response))
        return self

    def _complete(self, messages: list[Message], *, think: bool, schema: type[BaseModel] | None,
                  temperature: float, tools: list[dict[str, Any]] | None) -> str:
        step = self.events.current_step if self.events else None
        self.calls.append({"step": step, "messages": messages, "think": think,
                           "schema": schema.__name__ if schema else None, "temperature": temperature})
        if not self.script:
            raise FakeLLMMismatch(f"unexpected LLM call from step {step!r} (script exhausted)")
        expected, resp = self.script.pop(0)
        if expected != step:
            raise FakeLLMMismatch(f"LLM call from step {step!r}, script expected {expected!r}")
        if callable(resp) and not isinstance(resp, BaseModel):
            resp = resp(messages)
        if isinstance(resp, BaseModel):
            return resp.model_dump_json()
        return str(resp)

    @property
    def remaining(self) -> int:
        return len(self.script)

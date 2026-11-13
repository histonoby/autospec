"""PipelineRunner: executes steps, persists state per step, supports jump_to / always_run / resume / cancel."""

from __future__ import annotations

import traceback
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from .context import JobCancelled, JobContext
from .events import now_iso
from .llm import FakeLLMMismatch, LLMInfraError
from .step import Step, StepResult, StepStatus


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    WAITING_APPROVAL = "waiting_approval"
    INTERRUPTED = "interrupted"
    WAITING_DEVICE = "waiting_device"  # reserved for part 2 (§6.9)


PersistFn = Callable[[int, JobStatus, list[dict[str, Any]]], None]


class PipelineRunner:
    def __init__(self, steps: list[Step], ctx: JobContext, persist: PersistFn | None = None) -> None:
        self.steps = steps
        self.ctx = ctx
        self.persist = persist
        self.records: list[dict[str, Any]] = list(ctx.state.get("_steps", []))
        if len(self.records) != len(steps):
            self.records = [{"idx": i, "name": s.label, "status": "pending", "started_at": None,
                             "ended_at": None, "message": ""} for i, s in enumerate(steps)]

    def _save(self, idx: int, status: JobStatus) -> None:
        self.ctx.state["_steps"] = self.records
        self.ctx.state["_current_step"] = idx
        if self.persist:
            self.persist(idx, status, self.records)

    def _index_of(self, name: str, current: int) -> int:
        for i in range(current, -1, -1):
            if self.steps[i].name == name:
                return i
        for i, s in enumerate(self.steps):
            if s.name == name:
                return i
        raise KeyError(f"jump target not in pipeline: {name}")

    def _run_one(self, idx: int) -> StepResult:
        step = self.steps[idx]
        ev = self.ctx.events
        ev.current_step = step.name
        rec = self.records[idx]
        if step.should_skip(self.ctx):
            rec.update(status=StepStatus.SKIPPED.value, started_at=now_iso(), ended_at=now_iso(), message="skip")
            ev.emit("step.end", f"{step.label}: スキップ", data={"status": "skipped", "idx": idx})
            return StepResult.skipped()
        rec.update(status="running", started_at=now_iso(), ended_at=None, message="")
        ev.emit("step.start", f"{step.label} 開始", data={"idx": idx})
        try:
            res = step.run(self.ctx)
        except FakeLLMMismatch:
            raise
        except JobCancelled:
            rec.update(status="cancelled", ended_at=now_iso(), message="cancelled")
            ev.emit("step.end", f"{step.label}: キャンセル", level="warn", data={"status": "cancelled", "idx": idx})
            raise
        except LLMInfraError as e:
            res = StepResult.failed(str(e), error="infra_error")
        except Exception as e:  # noqa: BLE001 - a step crash becomes a FAILED step, never a dead worker
            res = StepResult.failed(f"{type(e).__name__}: {e}", error="exception",
                                    traceback=traceback.format_exc()[-4000:])
        rec.update(status=res.status.value, ended_at=now_iso(), message=res.message[:2000])
        level = "error" if res.status == StepStatus.FAILED else "info"
        ev.emit("step.end", f"{step.label}: {res.status.value}" + (f" — {res.message}" if res.message else ""),
                level=level, data={"status": res.status.value, "idx": idx, "jump_to": res.jump_to,
                                   "error": res.error, **({"traceback": res.data["traceback"]}
                                                          if "traceback" in res.data else {})})
        return res

    def run(self, start: int = 0) -> JobStatus:
        ctx, ev = self.ctx, self.ctx.events
        max_jumps = int(ctx.opt("max_jumps", ctx.settings.pipeline.max_jumps))
        ev.emit("job.status", "running", data={"status": JobStatus.RUNNING.value, "from_step": start})
        status = JobStatus.SUCCEEDED
        idx = start
        try:
            while idx < len(self.steps):
                if ctx.cancel.is_set():
                    status = JobStatus.CANCELLED
                    break
                res = self._run_one(idx)
                if res.status == StepStatus.FAILED:
                    status = JobStatus.FAILED
                    ctx.state["_error"] = res.error or "step_failed"
                    ctx.state["_error_message"] = res.message
                    self._save(idx, status)
                    break
                if res.status == StepStatus.WAITING_APPROVAL:
                    self._save(idx, JobStatus.WAITING_APPROVAL)
                    ev.emit("job.status", "waiting_approval",
                            data={"status": JobStatus.WAITING_APPROVAL.value, "step": idx})
                    return JobStatus.WAITING_APPROVAL
                if res.jump_to:
                    jumps = int(ctx.state.get("_jumps", 0)) + 1
                    ctx.state["_jumps"] = jumps
                    if jumps > max_jumps:
                        status = JobStatus.FAILED
                        ctx.state["_error"] = "max_jumps"
                        ctx.state["_error_message"] = f"jump_to の上限 {max_jumps} 回を超えました"
                        ev.emit("log", ctx.state["_error_message"], level="error")
                        self._save(idx, status)
                        break
                    target = self._index_of(res.jump_to, idx)
                    ev.log(f"{self.steps[idx].name} → {res.jump_to} へ戻ります（{jumps}/{max_jumps}）")
                    self._save(target, JobStatus.RUNNING)
                    idx = target
                    continue
                idx += 1
                self._save(idx, JobStatus.RUNNING)
        except JobCancelled:
            status = JobStatus.CANCELLED
        if status == JobStatus.CANCELLED:
            ctx.state["_error"] = "cancelled"
        if status in (JobStatus.FAILED, JobStatus.CANCELLED):
            self._run_always(idx + 1)
        ev.current_step = None
        self._save(idx, status)
        ev.emit("job.status", status.value, level="error" if status == JobStatus.FAILED else "info",
                data={"status": status.value, "error": ctx.state.get("_error"),
                      "message": ctx.state.get("_error_message")})
        return status

    def _run_always(self, start: int) -> None:
        for i in range(start, len(self.steps)):
            if self.steps[i].always_run:
                try:
                    self._run_one(i)
                except JobCancelled:
                    pass

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from autospec.config import Settings
from autospec.core.context import ArtifactStore, CancelToken, JobContext
from autospec.core.events import EventEmitter, MemorySink
from autospec.core.llm import FakeLLMClient
from autospec.core.registry import LockTimeout, Registry, ResourceLocks
from autospec.core.runner import JobStatus, PipelineRunner
from autospec.core.sandbox import LocalSandbox
from autospec.core.step import Step, StepResult, StepStatus
from autospec.core.workspace import (
    PathViolation,
    Workspace,
    commit_paths,
    create_workspace,
    restore_paths,
    safe_join,
)


def make_ctx(tmp_path: Path, state: dict[str, Any] | None = None, options: dict[str, Any] | None = None) -> JobContext:
    s = Settings()
    mem = MemorySink()
    ev = EventEmitter("j", [mem])
    tok = CancelToken()
    return JobContext(job_id="j", workspace=Workspace("w", tmp_path), workdir=tmp_path, profile=None, inputs={},
                      options=options or {}, state=state if state is not None else {},
                      llm=FakeLLMClient(s), events=ev, sandbox=LocalSandbox(tok),
                      artifacts=ArtifactStore(tmp_path / "art", ev), cancel=tok, settings=s, job_dir=tmp_path / "job",
                      extra={"mem": mem})


class Count(Step):
    name = "count"

    def run(self, ctx: JobContext) -> StepResult:
        ctx.state["count"] = ctx.state.get("count", 0) + 1
        return StepResult.done()


class JumpBack(Step):
    name = "jump_back"

    def run(self, ctx: JobContext) -> StepResult:
        if ctx.state.get("count", 0) < int(self.params.get("until", 3)):
            return StepResult.jump("count")
        return StepResult.done()


class Fail(Step):
    name = "fail"

    def run(self, ctx: JobContext) -> StepResult:
        return StepResult.failed("boom", error="x")


class Crash(Step):
    name = "crash"

    def run(self, ctx: JobContext) -> StepResult:
        raise RuntimeError("kaboom")


class Cleanup(Step):
    name = "cleanup"
    always_run = True

    def run(self, ctx: JobContext) -> StepResult:
        ctx.state["cleaned"] = True
        return StepResult.done()


class Wait(Step):
    name = "wait"

    def run(self, ctx: JobContext) -> StepResult:
        if ctx.state.get("approved"):
            return StepResult.done()
        return StepResult(StepStatus.WAITING_APPROVAL)


class CancelMe(Step):
    name = "cancel_me"

    def run(self, ctx: JobContext) -> StepResult:
        ctx.cancel.set()
        ctx.cancel.check()
        return StepResult.done()


def test_jump_to_loops_until_done(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    assert PipelineRunner([Count(), JumpBack(until=3)], ctx).run() == JobStatus.SUCCEEDED
    assert ctx.state["count"] == 3 and ctx.state["_jumps"] == 2


def test_max_jumps_stops_infinite_loop(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path, options={"max_jumps": 4})
    assert PipelineRunner([Count(), JumpBack(until=999)], ctx).run() == JobStatus.FAILED
    assert ctx.state["_error"] == "max_jumps"


def test_always_run_after_failure_and_crash(tmp_path: Path) -> None:
    for bad in (Fail(), Crash()):
        ctx = make_ctx(tmp_path, state={})
        assert PipelineRunner([Count(), bad, Count(), Cleanup()], ctx).run() == JobStatus.FAILED
        assert ctx.state["cleaned"] is True and ctx.state["count"] == 1


def test_waiting_approval_then_resume_from_step(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    saved: list[tuple[int, str]] = []
    steps = [Count(), Wait(), Count()]
    assert PipelineRunner(steps, ctx, lambda i, st, _r: saved.append((i, st.value))).run() == JobStatus.WAITING_APPROVAL
    assert ctx.state["_current_step"] == 1 and ctx.state["count"] == 1
    ctx.state["approved"] = True
    assert PipelineRunner(steps, ctx).run(ctx.state["_current_step"]) == JobStatus.SUCCEEDED
    assert ctx.state["count"] == 2


def test_cancel_runs_always_steps(tmp_path: Path) -> None:
    ctx = make_ctx(tmp_path)
    assert PipelineRunner([CancelMe(), Count(), Cleanup()], ctx).run() == JobStatus.CANCELLED
    assert ctx.state.get("cleaned") is True and "count" not in ctx.state


def test_registry_builds_pipeline_from_yaml(tmp_path: Path) -> None:
    reg = Registry()
    reg.register_step(Count)
    reg.register_step(JumpBack)
    (tmp_path / "p.yaml").write_text("name: demo\nsteps:\n  - count\n  - step: jump_back\n    params: {until: 2}\n")
    reg.load_pipeline_yaml(tmp_path / "p.yaml")
    steps = reg.build("demo")
    assert [s.name for s in steps] == ["count", "jump_back"] and steps[1].params == {"until": 2}
    with pytest.raises(KeyError):
        reg.build("nope")


def test_resource_locks() -> None:
    locks = ResourceLocks()
    locks.acquire(["llm"], 1, "a")
    with pytest.raises(LockTimeout):
        locks.acquire(["llm"], 0.2, "b")
    locks.release("a")
    locks.acquire(["llm", "uart:/dev/x"], 1, "b")
    assert locks.holder_of("uart:/dev/x") == "b"


# --- workspace / git -----------------------------------------------------------------------------------


def test_path_traversal_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(PathViolation):
        safe_join(tmp_path, "../etc/passwd")
    with pytest.raises(PathViolation):
        safe_join(tmp_path, "/etc/passwd")
    assert safe_join(tmp_path, "a/../b") == (tmp_path / "b").resolve()


def test_workspace_name_validation(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_workspace(tmp_path, "../bad")


def test_create_worktree_commit_and_restore(tmp_path: Path) -> None:
    ws = create_workspace(tmp_path / "root", "demo")
    (ws.root / "a.txt").write_text("v0")
    commit_paths(ws.root, ["a.txt"], "add a")
    wt, branch = ws.create_worktree("j1", tmp_path / "wts")
    (wt / "a.txt").write_text("v1")
    sha1 = commit_paths(wt, ["a.txt"], "r1")
    (wt / "a.txt").write_text("v2")
    commit_paths(wt, ["a.txt"], "r2")
    restore_paths(wt, sha1, ["a.txt"])
    assert (wt / "a.txt").read_text() == "v1"
    assert (ws.root / "a.txt").read_text() == "v0"  # main tree untouched until merge
    commit_paths(wt, ["a.txt"], "restore")
    ws.merge(branch, "merge job")
    assert (ws.root / "a.txt").read_text() == "v1"
    ws.remove_worktree("j1", tmp_path / "wts")
    assert not (tmp_path / "wts" / "job-j1").exists()


def test_commit_paths_never_adds_unrelated_files(tmp_path: Path) -> None:
    ws = create_workspace(tmp_path, "demo")
    (ws.root / "mine.txt").write_text("x")
    (ws.root / "user_secret.txt").write_text("y")
    commit_paths(ws.root, ["mine.txt"], "c")
    status = subprocess.run(["git", "status", "--porcelain"], cwd=ws.root, capture_output=True, text=True).stdout
    assert "user_secret.txt" in status

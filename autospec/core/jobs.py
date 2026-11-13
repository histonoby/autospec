"""Job records (file based in M1: DATA_DIR/jobs/<id>/job.json; the SQLite store replaces it in M2)."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import Settings
from .context import ArtifactStore, CancelToken, JobContext
from .events import EventEmitter, JsonlFileSink, Sink, now_iso
from .llm import BaseLLMClient, OllamaClient
from .profiles import resolve_profile
from .registry import LOCKS, Registry
from .runner import JobStatus, PipelineRunner
from .sandbox import DockerSandbox, LocalSandbox, Sandbox
from .workspace import Workspace


@dataclass
class JobRecord:
    id: str
    workspace: str
    workspace_root: str
    pipeline: str
    profile: str | None
    inputs: dict[str, Any]
    options: dict[str, Any]
    state: dict[str, Any] = field(default_factory=dict)
    status: str = JobStatus.QUEUED.value
    current_step: int = 0
    error: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)

    def job_dir(self, settings: Settings) -> Path:
        return settings.jobs_dir() / self.id

    def save(self, settings: Settings) -> None:
        d = self.job_dir(settings)
        d.mkdir(parents=True, exist_ok=True)
        self.updated_at = now_iso()
        tmp = d / "job.json.tmp"
        tmp.write_text(json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, d / "job.json")

    @classmethod
    def load(cls, settings: Settings, job_id: str) -> JobRecord:
        f = settings.jobs_dir() / job_id / "job.json"
        if not f.is_file():
            raise FileNotFoundError(f"job not found: {job_id} ({f})")
        return cls(**json.loads(f.read_text(encoding="utf-8")))


def new_job_id(settings: Settings) -> str:
    day = datetime.now().strftime("%Y%m%d")
    jobs = settings.jobs_dir()
    jobs.mkdir(parents=True, exist_ok=True)
    n = 1 + sum(1 for p in jobs.glob(f"j_{day}_*") if p.is_dir())
    while (jobs / f"j_{day}_{n:04d}").exists():
        n += 1
    (jobs / f"j_{day}_{n:04d}").mkdir()
    return f"j_{day}_{n:04d}"


def make_sandbox(settings: Settings, cancel: CancelToken, image: str | None, mounts: Sequence[Path]) -> Sandbox:
    if settings.sandbox.mode == "docker":
        return DockerSandbox(image or "ubuntu:24.04", cancel, extra_mounts=mounts)
    return LocalSandbox(cancel)


def build_context(settings: Settings, rec: JobRecord, registry: Registry, *, llm: BaseLLMClient | None = None,
                  sinks: Sequence[Sink] = (), interactive: bool = False,
                  cancel: CancelToken | None = None) -> JobContext:
    job_dir = rec.job_dir(settings)
    events = EventEmitter(rec.id, [JsonlFileSink(job_dir / "events.log"), *sinks])
    artifacts = ArtifactStore(job_dir / "artifacts", events)
    cancel = cancel or CancelToken()
    ws = Workspace(rec.workspace, Path(rec.workspace_root))
    workdir = Path(rec.state.get("workdir") or rec.workspace_root)
    profile = None
    if rec.profile or rec.pipeline != "analyze":
        hint = [str(x) for x in (rec.inputs.get("impl_path"), rec.inputs.get("interface_path")) if x]
        profile = resolve_profile(registry.profiles, rec.profile, workdir if workdir.exists() else None, hint)
    if llm is None:
        llm = OllamaClient(settings, events=events, artifacts=artifacts, cancel=cancel, locks=LOCKS)
    else:
        llm.bind(events, artifacts, cancel)
    sandbox = make_sandbox(settings, cancel, profile.sandbox_image if profile else None,
                           [job_dir, settings.harness_dir, settings.test_support_dir])
    return JobContext(job_id=rec.id, workspace=ws, workdir=workdir, profile=profile, inputs=rec.inputs,
                      options=rec.options, state=rec.state, llm=llm, events=events, sandbox=sandbox,
                      artifacts=artifacts, cancel=cancel, settings=settings, job_dir=job_dir,
                      interactive=interactive)


def run_job(settings: Settings, rec: JobRecord, registry: Registry, *, llm: BaseLLMClient | None = None,
            sinks: Sequence[Sink] = (), start: int | None = None, interactive: bool = False,
            cancel: CancelToken | None = None) -> tuple[JobStatus, JobContext]:
    ctx = build_context(settings, rec, registry, llm=llm, sinks=sinks, interactive=interactive, cancel=cancel)
    steps = registry.build(rec.pipeline, [tuple(x) for x in rec.options.get("extra_steps", [])])

    def persist(idx: int, status: JobStatus, _records: list[dict[str, Any]]) -> None:
        rec.current_step = idx
        rec.status = status.value
        rec.state = ctx.state
        rec.error = ctx.state.get("_error")
        rec.save(settings)

    rec.status = JobStatus.RUNNING.value
    rec.save(settings)
    status = PipelineRunner(steps, ctx, persist).run(rec.current_step if start is None else start)
    return status, ctx

"""FastAPI app (§13): REST + SSE over the SQLite store, one worker thread, the built Web UI as static files.

M2 scope: local use on the machine that runs it (127.0.0.1). Token auth, merge/discard and worktree
isolation arrive with M3.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from ..config import REPO_ROOT, Settings
from ..core.jobs import JobRecord, new_job_id
from ..core.llm import OllamaClient
from ..core.runner import JobStatus
from ..core.workspace import PathViolation, Workspace, create_workspace, git, safe_join, validate_name
from ..pipelines import build_registry
from ..steps.approval import record_decision
from .db import TERMINAL, Store
from .worker import LLMFactory, Worker, record_event

WEB_DIST = REPO_ROOT / "web" / "dist"
MAX_FILE_BYTES = 1_000_000
SSE_POLL_SEC = 0.4
RESUMABLE = {"interrupted", "failed", "cancelled"}


# --- request bodies ----------------------------------------------------------------------------------------

class WorkspaceIn(BaseModel):
    name: str
    mode: Literal["empty", "import"] = "empty"
    source_path: str | None = None


class JobIn(BaseModel):
    workspace: str
    pipeline: str = "implement"
    profile: str | None = None
    inputs: dict[str, Any]
    options: dict[str, Any] = Field(default_factory=dict)


class ApproveIn(BaseModel):
    target: str
    decision: Literal["approve", "reject"]
    comment: str = ""
    edited_content: str | None = None


class ResumeIn(BaseModel):
    from_step: int | None = None


# --- app ------------------------------------------------------------------------------------------------------

def create_app(settings: Settings, *, llm_factory: LLMFactory | None = None, start_worker: bool = True,
               web_dist: Path | None = WEB_DIST) -> FastAPI:
    registry = build_registry(settings)
    store = Store(settings.data_dir / "autospec.db")
    worker = Worker(settings, store, registry, llm_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        for job_id in store.mark_interrupted():
            record_event(settings, store, job_id, "job.status", "interrupted（サーバー再起動時に実行中だった）",
                         level="warn", data={"status": "interrupted"})
        if start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="AutoSpec", version="0.2.0", lifespan=lifespan)
    app.state.store = store
    app.state.worker = worker
    app.state.settings = settings

    def job_or_404(job_id: str) -> JobRecord:
        rec = store.job(job_id)
        if rec is None:
            raise HTTPException(404, f"job not found: {job_id}")
        return rec

    def ws_or_404(name: str) -> Workspace:
        row = store.workspace(name)
        if row is None:
            raise HTTPException(404, f"workspace not found: {name}")
        return Workspace(row["name"], Path(row["path"]))

    def requeue(rec: JobRecord) -> None:
        rec.status = JobStatus.QUEUED.value
        rec.error = None
        rec.save(settings)  # job.json stays in sync for `autospec gate --job` / CLI resume
        store.save_job(rec)
        worker.wake()

    # --- system -----------------------------------------------------------------------------------------
    @app.get("/api/health")
    def health() -> dict[str, Any]:
        info: dict[str, Any] = OllamaClient(settings).health()
        info.pop("models", None)
        info.update(aider=shutil.which(settings.aider.bin), git=shutil.which("git"),
                    docker=shutil.which("docker"), running_job=worker.running_job,
                    sandbox=settings.sandbox.mode)
        return info

    @app.get("/api/profiles")
    def profiles() -> list[dict[str, Any]]:
        return [{"name": p.name, "display_name": p.display_name, "extensions": p.extensions}
                for p in registry.profiles.values()]

    @app.get("/api/pipelines")
    def pipelines() -> list[str]:
        return sorted(registry.pipelines)

    @app.get("/api/samples")
    def samples() -> list[dict[str, Any]]:
        c_sample = REPO_ROOT / "tests" / "fixtures" / "c_sample"
        if not c_sample.is_dir():
            return []
        return [{"name": "c-sample", "title": "C サンプル（sat_add16 / clamp_int）", "source_path": str(c_sample),
                 "profile": "c", "inputs": {"spec_path": "spec.md", "interface_path": "sat.h",
                                            "impl_path": "src/sat.c", "test_path": "tests/test_sat.c"}}]

    # --- workspaces -----------------------------------------------------------------------------------
    @app.get("/api/workspaces")
    def list_workspaces() -> list[dict[str, Any]]:
        return store.workspaces()

    @app.post("/api/workspaces", status_code=201)
    def add_workspace(body: WorkspaceIn) -> dict[str, Any]:
        try:
            validate_name(body.name)
        except ValueError as e:
            raise HTTPException(400, "名前は英数字・_・- の 1〜64 文字にしてください") from e
        if store.workspace(body.name):
            raise HTTPException(409, f"workspace already exists: {body.name}")
        source = Path(body.source_path).expanduser() if body.source_path else None
        if body.mode == "import" and (source is None or not source.is_dir()):
            raise HTTPException(400, f"source_path が見つかりません: {body.source_path}")
        try:
            ws = create_workspace(settings.workspaces_root, body.name, body.mode, source)
        except FileExistsError as e:
            raise HTTPException(409, str(e)) from e
        store.add_workspace(ws.name, ws.root)
        return {"name": ws.name, "path": str(ws.root)}

    @app.get("/api/workspaces/{name}")
    def workspace_detail(name: str) -> dict[str, Any]:
        ws = ws_or_404(name)
        head = git(["log", "-1", "--format=%h %s (%cr)"], ws.root, check=False).strip()
        cfg = ws.root / ".autospec" / "workspace.yaml"
        return {"name": ws.name, "path": str(ws.root), "branch": ws.default_branch() if ws.is_repo() else None,
                "head": head, "workspace_yaml": cfg.read_text(encoding="utf-8") if cfg.is_file() else None,
                "jobs": store.jobs(workspace=name, limit=50)}

    @app.get("/api/workspaces/{name}/tree")
    def workspace_tree(name: str, path: str = "") -> list[dict[str, Any]]:
        ws = ws_or_404(name)
        d = _safe(ws.root, path)
        if not d.is_dir():
            raise HTTPException(404, f"not a directory: {path}")
        out = []
        for p in sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name)):
            if p.name == ".git" or p.name.startswith(".aider"):
                continue
            out.append({"name": p.name, "path": str(p.relative_to(ws.root.resolve())),
                        "type": "dir" if p.is_dir() else "file", "size": p.stat().st_size if p.is_file() else None})
        return out

    @app.get("/api/workspaces/{name}/file", response_class=PlainTextResponse)
    def workspace_file(name: str, path: str) -> str:
        return _read_text(_safe(ws_or_404(name).root, path))

    # --- jobs ------------------------------------------------------------------------------------------
    @app.post("/api/jobs", status_code=201)
    def create_job(body: JobIn) -> dict[str, Any]:
        ws = ws_or_404(body.workspace)
        if body.pipeline not in registry.pipelines:
            raise HTTPException(400, f"unknown pipeline: {body.pipeline}")
        if body.profile and body.profile not in registry.profiles:
            raise HTTPException(400, f"unknown profile: {body.profile}")
        inputs = {k: v for k, v in body.inputs.items() if v not in (None, "")}
        if not (inputs.get("spec_text") or inputs.get("spec_path")):
            raise HTTPException(400, "仕様書（spec_text か spec_path）を指定してください")
        for key in ("impl_path", "test_path"):
            if not inputs.get(key):
                raise HTTPException(400, f"{key} を指定してください")
        for key in ("spec_path", "interface_path", "impl_path", "test_path", "requirements_path"):
            raw = inputs.get(key)
            for v in raw if isinstance(raw, list) else [raw]:
                if v:
                    _safe(ws.root, str(v))
        options = {"workdir_mode": "inplace", **body.options}
        rec = JobRecord(id=new_job_id(settings), workspace=ws.name, workspace_root=str(ws.root),
                        pipeline=body.pipeline, profile=body.profile, inputs=inputs, options=options)
        rec.save(settings)
        store.save_job(rec)
        record_event(settings, store, rec.id, "job.status", "queued", data={"status": "queued"})
        worker.wake()
        return {"id": rec.id}

    @app.get("/api/jobs")
    def list_jobs(workspace: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        return store.jobs(workspace=workspace, status=status)

    @app.get("/api/jobs/{job_id}")
    def job_detail(job_id: str) -> dict[str, Any]:
        rec = job_or_404(job_id)
        state = {k: v for k, v in rec.state.items() if k != "_steps"}
        steps = store.steps(job_id)
        if not steps:
            steps = [{"idx": i, "name": s.label, "status": "pending", "started_at": None, "ended_at": None,
                      "message": ""} for i, s in enumerate(registry.build(rec.pipeline))]
        return {"id": rec.id, "workspace": rec.workspace, "pipeline": rec.pipeline, "profile": rec.profile,
                "inputs": rec.inputs, "options": rec.options, "status": rec.status,
                "current_step": rec.current_step, "error": rec.error, "created_at": rec.created_at,
                "updated_at": rec.updated_at, "steps": steps, "approvals": store.approvals(job_id),
                "state": state, "model": settings.ollama.model}

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(job_id: str, request: Request, after: int | None = Query(None)) -> EventSourceResponse:
        job_or_404(job_id)
        # EventSource reconnects to the same URL (?after=N) and adds Last-Event-ID: continue from the later one
        header = request.headers.get("last-event-id") or ""
        last = max(after or 0, int(header) if header.isdigit() else 0)

        async def gen() -> AsyncIterator[dict[str, str]]:
            cursor = last
            while True:
                batch = store.events(job_id, cursor, 500)
                for ev in batch:
                    cursor = ev["id"]
                    yield {"id": str(ev["id"]), "data": json.dumps(ev, ensure_ascii=False)}
                if len(batch) == 500:
                    continue
                rec = store.job(job_id)
                if rec is None or (rec.status in TERMINAL and not store.events(job_id, cursor, 1)):
                    yield {"event": "end", "data": rec.status if rec else "gone"}
                    return
                if await request.is_disconnected():
                    return
                await asyncio.sleep(SSE_POLL_SEC)

        return EventSourceResponse(gen(), ping=15)

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, Any]:
        rec = job_or_404(job_id)
        if rec.status in TERMINAL:
            raise HTTPException(409, f"job is already {rec.status}")
        if rec.status == JobStatus.RUNNING.value:
            worker.cancel(job_id)
            record_event(settings, store, job_id, "log", "キャンセルを要求しました（実行中のプロセスを停止します）",
                         level="warn")
            return {"status": "cancelling"}
        rec.status = JobStatus.CANCELLED.value
        rec.error = "cancelled"
        rec.state["_error"] = "cancelled"
        rec.save(settings)
        store.save_job(rec)
        record_event(settings, store, job_id, "job.status", "cancelled", level="warn", data={"status": "cancelled"})
        return {"status": "cancelled"}

    @app.post("/api/jobs/{job_id}/resume")
    def resume_job(job_id: str, body: ResumeIn | None = None) -> dict[str, Any]:
        rec = job_or_404(job_id)
        if rec.status not in RESUMABLE:
            raise HTTPException(409, f"{rec.status} のジョブは再開できません（{', '.join(sorted(RESUMABLE))} のみ）")
        n_steps = len(registry.build(rec.pipeline))
        if body and body.from_step is not None:
            if not 0 <= body.from_step < n_steps:
                raise HTTPException(400, f"from_step は 0〜{n_steps - 1}")
            rec.current_step = body.from_step
        for key in ("_error", "_error_message"):
            rec.state.pop(key, None)
        requeue(rec)
        record_event(settings, store, job_id, "job.status", f"queued（再開: ステップ {rec.current_step} から）",
                     data={"status": "queued", "from_step": rec.current_step})
        return {"status": "queued", "from_step": rec.current_step}

    @app.post("/api/jobs/{job_id}/approve")
    def approve_job(job_id: str, body: ApproveIn) -> dict[str, Any]:
        rec = job_or_404(job_id)
        if rec.status != JobStatus.WAITING_APPROVAL.value:
            raise HTTPException(409, f"承認待ちではありません（{rec.status}）")
        record_decision(rec.state, body.target, body.decision, body.comment, body.edited_content)
        store.resolve_approval(job_id, body.target, body.decision, body.comment, body.edited_content)
        requeue(rec)
        record_event(settings, store, job_id, "log", f"{body.target}: {body.decision}"
                     + (f" — {body.comment}" if body.comment else ""), data={"target": body.target})
        return {"status": "queued"}

    @app.get("/api/jobs/{job_id}/artifacts")
    def list_artifacts(job_id: str) -> list[dict[str, Any]]:
        rec = job_or_404(job_id)
        root = rec.job_dir(settings) / "artifacts"
        if not root.is_dir():
            return []
        return [{"name": str(p.relative_to(root)), "size": p.stat().st_size}
                for p in sorted(root.rglob("*")) if p.is_file()]

    @app.get("/api/jobs/{job_id}/artifacts/{name:path}", response_class=PlainTextResponse)
    def get_artifact(job_id: str, name: str) -> str:
        rec = job_or_404(job_id)
        return _read_text(_safe(rec.job_dir(settings) / "artifacts", name))

    # --- Web UI --------------------------------------------------------------------------------------------
    if web_dist is not None and (web_dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
    else:
        @app.get("/", response_class=HTMLResponse)
        def no_ui() -> str:
            return ("<h1>AutoSpec API</h1><p>Web UI のビルド（web/dist）が見つかりません。"
                    "<code>cd web && npm ci && npm run build</code> を実行してください。API は <a href='/docs'>/docs</a>。</p>")

    return app


def _safe(root: Path, rel: str) -> Path:
    try:
        return safe_join(root, rel or ".")
    except PathViolation as e:
        raise HTTPException(400, str(e)) from e


def _read_text(p: Path) -> str:
    if not p.is_file():
        raise HTTPException(404, "file not found")
    if p.stat().st_size > MAX_FILE_BYTES:
        raise HTTPException(413, f"1MB を超えるファイルは表示しません（{p.stat().st_size} bytes）")
    try:
        return p.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        raise HTTPException(415, "テキストではないファイルです") from e

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from autospec.config import Settings
from autospec.core.events import MemorySink
from autospec.core.jobs import JobRecord, new_job_id, run_job
from autospec.core.llm import FakeLLMClient, FakeResponse
from autospec.core.runner import JobStatus
from autospec.pipelines import build_registry

TESTS_DIR = Path(__file__).resolve().parent
FIXTURES = TESTS_DIR / "fixtures"
FAKE_AIDER = TESTS_DIR / "fake_aider.py"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    s = Settings()
    s.paths.data_dir = str(tmp_path / "data")
    s.paths.workspaces_root = str(tmp_path / "workspaces")
    s.aider.bin = str(FAKE_AIDER)
    s.ollama.backoff_base_sec = 0
    FAKE_AIDER.chmod(0o755)
    return s


@pytest.fixture
def c_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws" / "c_sample"
    shutil.copytree(FIXTURES / "c_sample", ws)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=ws, check=True)
    return ws


@dataclass
class Run:
    status: JobStatus
    rec: JobRecord
    ctx: Any
    llm: FakeLLMClient
    events: MemorySink
    aider_argv: list[list[str]]

    @property
    def state(self) -> dict[str, Any]:
        return self.rec.state

    def artifact(self, name: str) -> Path:
        return self.ctx.artifacts.path(name)

    def messages(self, type_: str) -> list[str]:
        return [e.message for e in self.events.of_type(type_)]


@pytest.fixture
def run_impl(settings: Settings, c_workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    def _run(script: list[tuple[str, FakeResponse]], aider_calls: list[dict[str, Any]],
             options: dict[str, Any] | None = None, interactive: bool = False) -> Run:
        plan = tmp_path / "aider_plan.json"
        argv_log = tmp_path / "aider_argv.jsonl"
        plan.write_text(json.dumps({"calls": aider_calls, "counter": str(tmp_path / "aider_counter"),
                                    "argv_log": str(argv_log)}))
        monkeypatch.setenv("FAKE_AIDER_PLAN", str(plan))
        reg = build_registry(settings)
        rec = JobRecord(id=new_job_id(settings), workspace="c_sample", workspace_root=str(c_workspace),
                        pipeline="implement", profile="c",
                        inputs={"spec_path": "spec.md", "interface_path": "sat.h", "impl_path": "src/sat.c",
                                "test_path": "tests/test_sat.c"},
                        options={"max_rounds": 3, **(options or {})})
        llm = FakeLLMClient(settings, script)
        mem = MemorySink()
        status, ctx = run_job(settings, rec, reg, llm=llm, sinks=[mem], interactive=interactive)
        argv = [json.loads(x) for x in argv_log.read_text().splitlines()] if argv_log.exists() else []
        return Run(status, rec, ctx, llm, mem, argv)

    return _run

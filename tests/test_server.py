"""M2: API + SQLite + worker + SSE, driven end to end with FakeLLM and the fake aider (§19 M2)."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sse_starlette")

from fastapi.testclient import TestClient  # noqa: E402

from autospec.cli import main as cli_main  # noqa: E402
from autospec.config import Settings  # noqa: E402
from autospec.core.llm import FakeLLMClient, FakeResponse  # noqa: E402
from autospec.core.schemas import TestVerdict  # noqa: E402
from autospec.server.api import create_app  # noqa: E402
from autospec.server.db import Store  # noqa: E402

from .conftest import FIXTURES  # noqa: E402
from .helpers import GOOD_IMPL, GOOD_TEST, ONE_FAIL_IMPL, REQS, STUB, audit_ok, fenced  # noqa: E402

pytestmark = pytest.mark.skipif(not shutil.which("cc"), reason="C compiler required")

JOB: dict[str, Any] = {"workspace": "demo", "pipeline": "implement", "profile": "c",
       "inputs": {"spec_path": "spec.md", "interface_path": "sat.h", "impl_path": "src/sat.c",
                  "test_path": "tests/test_sat.c"},
       "options": {"max_rounds": 3}}
BASE: list[tuple[str, FakeResponse]] = [("requirements_extract", REQS), ("test_gen", fenced(GOOD_TEST)), ("red_check", fenced(STUB))]


class Server:
    def __init__(self, settings: Settings, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.settings = settings
        self.tmp = tmp
        self.monkeypatch = monkeypatch
        self.llm = FakeLLMClient(settings, [])

    def aider(self, calls: list[dict[str, Any]]) -> None:
        plan = self.tmp / "aider_plan.json"
        plan.write_text(json.dumps({"calls": calls, "counter": str(self.tmp / "aider_counter"),
                                    "argv_log": str(self.tmp / "aider_argv.jsonl")}))
        self.monkeypatch.setenv("FAKE_AIDER_PLAN", str(plan))

    def script(self, script: list[tuple[str, FakeResponse]]) -> None:
        self.llm.script.extend(script)

    def client(self) -> TestClient:
        app = create_app(self.settings, llm_factory=lambda rec: self.llm, web_dist=None)
        app.state.worker.poll_sec = 0.05
        return TestClient(app)


@pytest.fixture
def server(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Server:
    return Server(settings, tmp_path, monkeypatch)


def make_workspace(c: TestClient) -> None:
    r = c.post("/api/workspaces", json={"name": "demo", "mode": "import", "source_path": str(FIXTURES / "c_sample")})
    assert r.status_code == 201, r.text


def wait_status(c: TestClient, job_id: str, want: set[str], timeout: float = 60) -> dict[str, Any]:
    deadline = time.time() + timeout
    job: dict[str, Any] = {}
    while time.time() < deadline:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] in want:
            return job
        time.sleep(0.05)
    raise AssertionError(f"status {job.get('status')} never reached {want}: {job.get('state', {}).get('_error_message')}")


def sse(c: TestClient, url: str, headers: dict[str, str] | None = None) -> Iterator[tuple[str, str, str]]:
    """Yield (event, id, data) from a finished job's SSE stream (the server closes it with `end`)."""
    with c.stream("GET", url, headers=headers or {}) as resp:
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/event-stream")
        event, eid = "message", ""
        data: list[str] = []
        for line in resp.iter_lines():
            if line == "":
                if data:
                    yield event, eid, "\n".join(data)
                event, eid, data = "message", "", []
            elif line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("id:"):
                eid = line[3:].strip()
            elif line.startswith("data:"):
                data.append(line[5:].lstrip())


def test_job_runs_from_api_and_streams_events(server: Server) -> None:
    server.script([*BASE, ("audit", audit_ok())])
    server.aider([{"write": GOOD_IMPL, "stdout": "aider: editing src/sat.c"}])
    with server.client() as c:
        assert c.get("/api/profiles").json()[0]["name"]
        make_workspace(c)
        assert c.post("/api/workspaces", json={"name": "demo"}).status_code == 409
        job_id = c.post("/api/jobs", json=JOB).json()["id"]
        job = wait_status(c, job_id, {"succeeded", "failed"})
        assert job["status"] == "succeeded", job["state"].get("_error_message")
        assert job["state"]["gate_passed"] is True
        assert [s["status"] for s in job["steps"]] == ["done"] * len(job["steps"])
        assert c.get("/api/workspaces").json()[0]["last_status"] == "succeeded"

        events = list(sse(c, f"/api/jobs/{job_id}/events"))
        assert events[-1][0] == "end" and events[-1][2] == "succeeded"
        msgs = [json.loads(d) for e, _, d in events if e == "message"]
        types = [m["type"] for m in msgs]
        assert types[0] == "job.status" and "step.start" in types and "gate.result" in types
        assert any(m["type"] == "aider.output" and "editing src/sat.c" in m["message"] for m in msgs)
        ids = [m["id"] for m in msgs]
        assert ids == sorted(ids) and len(set(ids)) == len(ids)

        # reload in the middle: ?after= and Last-Event-ID both continue after that id
        mid = ids[len(ids) // 2]
        after = [json.loads(d)["id"] for e, _, d in sse(c, f"/api/jobs/{job_id}/events?after={mid}") if e == "message"]
        assert after == [i for i in ids if i > mid]
        hdr = [json.loads(d)["id"] for e, _, d in sse(c, f"/api/jobs/{job_id}/events", {"Last-Event-ID": str(mid)})
               if e == "message"]
        assert hdr == after

        names = {a["name"] for a in c.get(f"/api/jobs/{job_id}/artifacts").json()}
        assert {"requirements.json", "audit_report.md", "diff.patch"} <= names
        assert "REQ-001" in c.get(f"/api/jobs/{job_id}/artifacts/audit_report.md").text
        assert c.get(f"/api/jobs/{job_id}/artifacts/../job.json").status_code in (400, 404)
        tree = {e["name"] for e in c.get("/api/workspaces/demo/tree").json()}
        assert {"spec.md", "src", "tests"} <= tree and ".git" not in tree
        assert "sat_add16" in c.get("/api/workspaces/demo/file", params={"path": "sat.h"}).text


def test_cancel_stops_the_aider_process(server: Server) -> None:
    pid_file = server.tmp / "aider.pid"
    server.script(BASE)
    server.aider([{"stdout": "thinking...", "sleep": 120, "pid_file": str(pid_file)}])
    with server.client() as c:
        make_workspace(c)
        job_id = c.post("/api/jobs", json=JOB).json()["id"]
        deadline = time.time() + 60
        while not pid_file.exists() and time.time() < deadline:
            time.sleep(0.05)
        pid = int(pid_file.read_text())
        assert c.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "cancelling"
        job = wait_status(c, job_id, {"cancelled", "failed", "succeeded"}, timeout=20)
        assert job["status"] == "cancelled"
        with pytest.raises(ProcessLookupError):
            for _ in range(50):
                os.kill(pid, 0)
                time.sleep(0.1)
        assert c.post(f"/api/jobs/{job_id}/cancel").status_code == 409


def test_restart_marks_running_job_interrupted_and_resume_continues(server: Server) -> None:
    server.script([*BASE, ("audit", audit_ok())])
    server.aider([{"write": GOOD_IMPL}])
    with server.client() as c:
        make_workspace(c)
        job_id = c.post("/api/jobs", json=JOB).json()["id"]
        wait_status(c, job_id, {"succeeded"})
    # simulate a crash in the middle of `implement`: the row says running at step 6
    db = server.settings.data_dir / "autospec.db"
    store = Store(db)
    store.set_status(job_id, "running", current_step=6)
    store.close()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE steps SET status='running' WHERE job_id=? AND idx=6", (job_id,))
    server.script([("audit", audit_ok())])
    with server.client() as c:
        job = c.get(f"/api/jobs/{job_id}").json()
        assert job["status"] == "interrupted"
        assert job["steps"][6]["status"] == "interrupted"
        assert c.post(f"/api/jobs/{job_id}/resume", json={}).json() == {"status": "queued", "from_step": 6}
        job = wait_status(c, job_id, {"succeeded", "failed"})
        assert job["status"] == "succeeded", job["state"].get("_error_message")
        assert c.post(f"/api/jobs/{job_id}/resume").status_code == 409


def test_test_dispute_waits_for_approval_in_the_ui(server: Server) -> None:
    verdict = TestVerdict(test_name="test_REQ002_add_saturates", suspect=True, req_id="REQ-002", reason="期待値が逆")
    server.script([*BASE, ("implement", verdict), ("audit", audit_ok())])
    server.aider([{"write": ONE_FAIL_IMPL}] * 3 + [{"write": GOOD_IMPL}])
    with server.client() as c:
        make_workspace(c)
        job_id = c.post("/api/jobs", json={**JOB, "options": {"max_rounds": 5, "suspect_after_rounds": 3}}).json()["id"]
        job = wait_status(c, job_id, {"waiting_approval", "failed", "succeeded"})
        assert job["status"] == "waiting_approval", job["state"].get("_error_message")
        assert job["approvals"][0]["target"] == "test_dispute" and job["approvals"][0]["decision"] is None
        assert job["state"]["approval_content"]["test_dispute"]["reason"] == "期待値が逆"
        r = c.post(f"/api/jobs/{job_id}/approve", json={"target": "test_dispute", "decision": "approve",
                                                         "comment": "テストは正しい"})
        assert r.json() == {"status": "queued"}
        job = wait_status(c, job_id, {"succeeded", "failed"})
        assert job["status"] == "succeeded", job["state"].get("_error_message")
        assert job["approvals"][0]["decision"] == "approve"
        assert len(job["state"]["rounds"]) == 4


def test_input_validation(server: Server) -> None:
    with server.client() as c:
        make_workspace(c)
        bad: list[tuple[dict[str, Any], int]] = [
            ({**JOB, "inputs": {**JOB["inputs"], "impl_path": "../../etc/passwd"}}, 400),
            ({**JOB, "inputs": {"impl_path": "a.c", "test_path": "t.c"}}, 400),
            ({**JOB, "profile": "cobol"}, 400),
            ({**JOB, "workspace": "nope"}, 404),
        ]
        for body, code in bad:
            assert c.post("/api/jobs", json=body).status_code == code, body
        assert c.post("/api/workspaces", json={"name": "a b"}).status_code == 400
        assert c.get("/api/workspaces/demo/file", params={"path": "../x"}).status_code == 400


def test_serve_refuses_non_loopback_host(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli_main(["serve", "--host", "0.0.0.0"]) == 2
    assert "127.0.0.1" in capsys.readouterr().err


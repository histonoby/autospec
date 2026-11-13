"""Integration: FakeLLM + fake aider + real C compiler through the whole `implement` pipeline (§19 M1)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autospec.core.runner import JobStatus
from autospec.core.schemas import TestVerdict

from .helpers import (
    ALWAYS_PASS_TEST,
    BROKEN_IMPL,
    GOOD_IMPL,
    GOOD_TEST,
    ONE_FAIL_IMPL,
    REQS,
    STUB,
    TEST_MISSING_REQ002,
    TWO_FAIL_IMPL,
    audit_ok,
    audit_with_fake_name,
    fenced,
)

pytestmark = pytest.mark.skipif(not shutil.which("cc"), reason="C compiler required")

BUILD_NOISE = "\nビルド例:\n```bash\ncc -o t src/sat.c tests/test_sat.c\n```\n"


def base_script(test_code: str = GOOD_TEST) -> list:
    return [("requirements_extract", REQS), ("test_gen", fenced(test_code, after=BUILD_NOISE)),
            ("red_check", fenced(STUB))]


def test_happy_path_and_artifacts(run_impl) -> None:
    r = run_impl([*base_script(), ("audit", audit_ok())], [{"write": GOOD_IMPL}])
    assert r.status == JobStatus.SUCCEEDED, r.state.get("_error_message")
    assert r.state["gate_passed"] and r.state["audit"]["overall_status"] == "PASS"
    assert r.state["audit"]["score"] == 100.0
    assert r.state["red_check"]["status"] == "red"
    assert r.llm.remaining == 0
    for name in ("requirements.json", "coverage.json", "audit_result.json", "audit_report.md", "rounds.json",
                 "diff.patch", "llm/0001.json"):
        assert r.artifact(name).exists(), name
    report = r.artifact("audit_report.md").read_text()
    assert "ok \\| 境界値も確認<br>2行目" in report  # L3
    test_file = r.ctx.workdir / "tests" / "test_sat.c"
    assert "```" not in test_file.read_text() and "cc -o t" not in test_file.read_text()  # B1
    assert (r.ctx.workdir / "tests" / "autospec_test.h").exists()
    assert not os.access(test_file, os.W_OK)  # lock_tests
    assert (r.rec.job_dir(r.ctx.settings) / "events.log").exists()  # L4
    status = subprocess.run(["git", "status", "--porcelain"], cwd=r.ctx.workdir, capture_output=True, text=True)
    assert status.stdout.strip() == ""  # everything committed, aider cache excluded


def test_aider_command_line(run_impl) -> None:
    r = run_impl([*base_script(), ("audit", audit_ok())], [{"write": GOOD_IMPL}])
    argv = r.aider_argv[0]
    for flag in ("--no-suggest-shell-commands", "--no-auto-commits", "--yes-always", "--auto-test"):
        assert flag in argv
    assert argv[argv.index("--edit-format") + 1] == "whole"
    assert argv[argv.index("--model") + 1].startswith("ollama_chat/")
    reads = [argv[i + 1] for i, a in enumerate(argv) if a == "--read"]
    assert reads == ["spec.md", "sat.h", "tests/test_sat.c"]
    assert argv[-1] == "src/sat.c"
    assert argv[argv.index("--message") + 1].rstrip().endswith("/no_think")
    test_cmd = argv[argv.index("--test-cmd") + 1]
    assert "-m autospec gate --job" in test_cmd and test_cmd.endswith(" full")


def test_gate_subcommand_runs_for_job(run_impl) -> None:
    r = run_impl([*base_script(), ("audit", audit_ok())], [{"write": GOOD_IMPL}])
    argv = r.aider_argv[0]
    test_cmd = argv[argv.index("--test-cmd") + 1]
    proc = subprocess.run(test_cmd, shell=True, capture_output=True, text=True, cwd=r.ctx.workdir, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "PASSED: 3 FAILED: 0" in proc.stdout


def test_coverage_gap_sends_back_to_test_gen(run_impl) -> None:
    script = [("requirements_extract", REQS), ("test_gen", fenced(TEST_MISSING_REQ002)),
              ("test_gen", fenced(GOOD_TEST)), ("red_check", fenced(STUB)), ("audit", audit_ok())]
    r = run_impl(script, [{"write": GOOD_IMPL}])
    assert r.status == JobStatus.SUCCEEDED
    assert r.state["coverage_retries"] == 1 and r.state["coverage"]["missing_must"] == []
    second_prompt = [c for c in r.llm.calls if c["step"] == "test_gen"][1]["messages"][-1]["content"]
    assert "REQ-002" in second_prompt and "前回の指摘" in second_prompt


def test_red_check_detects_always_passing_tests(run_impl) -> None:
    script = [("requirements_extract", REQS), ("test_gen", fenced(ALWAYS_PASS_TEST)), ("red_check", fenced(STUB)),
              ("test_gen", fenced(GOOD_TEST)), ("red_check", fenced(STUB)), ("audit", audit_ok())]
    r = run_impl(script, [{"write": GOOD_IMPL}])
    assert r.status == JobStatus.SUCCEEDED
    assert r.state["red_check_retries"] == 1 and r.state["red_check"]["status"] == "red"
    assert "検査していません" in [c for c in r.llm.calls if c["step"] == "test_gen"][1]["messages"][-1]["content"]
    # the implementation file was restored after the stub run (scaffold, then aider's version)
    assert (r.ctx.workdir / "src" / "sat.c").read_text() == GOOD_IMPL


def test_test_gen_retries_on_extraction_error_and_syntax_error(run_impl) -> None:
    script = [("requirements_extract", REQS), ("test_gen", "コードはありません"),
              ("test_gen", fenced(GOOD_TEST.replace("CHECK_EQ_INT(clamp_int(2, 0, 3), 2);", "CHECK_EQ_INT(clamp_int(2, 0, 3) 2);"))),
              ("test_gen", fenced(GOOD_TEST)), ("red_check", fenced(STUB)), ("audit", audit_ok())]
    r = run_impl(script, [{"write": GOOD_IMPL}])
    assert r.status == JobStatus.SUCCEEDED
    calls = [c for c in r.llm.calls if c["step"] == "test_gen"]
    assert [c["temperature"] for c in calls] == [0.2, 0.35, 0.5]  # I6
    assert len(calls[2]["messages"]) == 4 and "error" in calls[2]["messages"][-1]["content"]  # I5 + B3


def test_best_round_is_kept_when_later_round_regresses(run_impl) -> None:
    calls = [{"write": TWO_FAIL_IMPL}, {"write": ONE_FAIL_IMPL}, {"write": BROKEN_IMPL}]
    r = run_impl([*base_script(), ("audit", audit_ok())], calls, options={"max_rounds": 3})
    assert r.status == JobStatus.SUCCEEDED  # pipeline completes; gate result is reported, not hidden
    assert r.state["gate_passed"] is False and r.state["best_round"] == 2
    assert [x["failures"] for x in r.state["rounds"]][:2] == [2, 1]
    assert (r.ctx.workdir / "src" / "sat.c").read_text() == ONE_FAIL_IMPL
    assert r.state["audit"]["overall_status"] == "WARN"
    log = subprocess.run(["git", "log", "--format=%s"], cwd=r.ctx.workdir, capture_output=True, text=True).stdout
    assert "restore best round 2" in log and "round 3" in log


def test_round_two_error_digest_is_passed_to_aider(run_impl) -> None:
    r = run_impl([*base_script(), ("audit", audit_ok())], [{"write": ONE_FAIL_IMPL}, {"write": GOOD_IMPL}])
    assert r.state["gate_passed"] and len(r.state["rounds"]) == 2
    msg2 = r.aider_argv[1][r.aider_argv[1].index("--message") + 1]
    assert "test_REQ002_add_saturates" in msg2 and "PASSED: 2 FAILED: 1" in msg2


def test_aider_infra_failure_does_not_consume_rounds(run_impl) -> None:
    calls = [{"write": None, "exit": 1, "stdout": "litellm.APIConnectionError: OllamaException - "
                                                 "model 'local-coder-64k' not found"}]
    r = run_impl(base_script(), calls)
    assert r.status == JobStatus.FAILED
    assert r.state["_error"] == "infra_error"
    assert r.state.get("rounds", []) == []
    assert len(r.aider_argv) == 1


def test_audit_with_nonexistent_function_is_warn(run_impl) -> None:
    r = run_impl([*base_script(), ("audit", audit_with_fake_name())], [{"write": GOOD_IMPL}])
    assert r.state["gate_passed"] is True
    audit = json.loads(r.artifact("audit_result.json").read_text())
    assert audit["overall_status"] == "WARN"
    req3 = next(x for x in audit["requirements"] if x["req_id"] == "REQ-003")
    assert req3["unverified"] and req3["missing_impl"] == ["clamp_value_fast"]
    assert "❓" in r.artifact("audit_report.md").read_text()


def test_test_tampering_by_aider_fails_the_job(run_impl) -> None:
    r = run_impl(base_script(), [{"write": GOOD_IMPL, "tamper": "tests/test_sat.c"}])
    assert r.status == JobStatus.FAILED and r.state["_error"] == "test_tampered"


def test_repeated_failure_triggers_test_review_and_cli_aborts(run_impl) -> None:
    verdict = TestVerdict(test_name="test_REQ002_add_saturates", suspect=True, req_id="REQ-002",
                          reason="仕様と期待値が逆")
    script = [*base_script(), ("implement", verdict)]
    r = run_impl(script, [{"write": ONE_FAIL_IMPL}], options={"max_rounds": 5, "suspect_after_rounds": 3})
    assert r.status == JobStatus.FAILED and r.state["_error"] == "test_dispute"
    assert len(r.state["rounds"]) == 3
    assert r.artifact("test_review_1.json").exists()


def test_test_review_not_suspect_continues(run_impl) -> None:
    verdict = TestVerdict(test_name="test_REQ002_add_saturates", suspect=False, req_id="REQ-002", reason="テストは正しい")
    script = [*base_script(), ("implement", verdict), ("audit", audit_ok())]
    calls = [{"write": ONE_FAIL_IMPL}] * 3 + [{"write": GOOD_IMPL}]
    r = run_impl(script, calls, options={"max_rounds": 5, "suspect_after_rounds": 3})
    assert r.status == JobStatus.SUCCEEDED and r.state["gate_passed"] and len(r.state["rounds"]) == 4


def test_worktree_mode_keeps_workspace_untouched(run_impl, c_workspace: Path) -> None:
    r = run_impl([*base_script(), ("audit", audit_ok())], [{"write": GOOD_IMPL}],
                 options={"workdir_mode": "worktree"})
    assert r.status == JobStatus.SUCCEEDED
    assert r.ctx.workdir != c_workspace and (r.ctx.workdir / "src" / "sat.c").read_text() == GOOD_IMPL
    assert not (c_workspace / "src" / "sat.c").exists()
    branches = subprocess.run(["git", "branch"], cwd=c_workspace, capture_output=True, text=True).stdout
    assert f"autospec/job-{r.rec.id}" in branches


def test_reuse_tests_skips_generation(run_impl, c_workspace: Path) -> None:
    (c_workspace / "tests").mkdir()
    (c_workspace / "tests" / "test_sat.c").write_text(GOOD_TEST)
    script = [("requirements_extract", REQS), ("red_check", fenced(STUB)), ("audit", audit_ok())]
    r = run_impl(script, [{"write": GOOD_IMPL}], options={"reuse_tests": True})
    assert r.status == JobStatus.SUCCEEDED and r.llm.remaining == 0


def test_cli_returns_nonzero_when_job_fails(tmp_path: Path, c_workspace: Path) -> None:
    env = {**os.environ, "AUTOSPEC_PATHS_DATA_DIR": str(tmp_path / "data"),
           "AUTOSPEC_OLLAMA_URL": "http://127.0.0.1:9", "AUTOSPEC_OLLAMA_CONNECT_RETRIES": "1",
           "AUTOSPEC_OLLAMA_BACKOFF_BASE_SEC": "0"}
    proc = subprocess.run([sys.executable, "-m", "autospec", "run", "implement", "--lang", "c", "--spec", "spec.md",
                           "--interface", "sat.h", "--impl", "src/sat.c", "--test", "tests/test_sat.c"],
                          cwd=c_workspace, env=env, capture_output=True, text=True, timeout=120)
    assert proc.returncode == 1
    assert "infra_error" in proc.stdout
    logs = list((tmp_path / "data" / "jobs").glob("*/events.log"))
    assert logs and logs[0].read_text().strip()

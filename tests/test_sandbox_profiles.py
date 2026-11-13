from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from autospec.config import Settings, load_settings
from autospec.core.context import CancelToken
from autospec.core.profiles import ProfileError, load_profiles, render_command, render_text, resolve_profile
from autospec.core.sandbox import TIMEOUT_RC, LocalSandbox
from autospec.core.textutil import extract_error_digest

# --- sandbox (B2) ------------------------------------------------------------------------------------


def test_infinite_loop_is_killed_and_digest_has_timeout(tmp_path: Path) -> None:
    start = time.monotonic()
    run = LocalSandbox().run("echo start; while true; do :; done", cwd=tmp_path, timeout=1)
    assert run.timed_out and run.returncode == TIMEOUT_RC
    assert time.monotonic() - start < 10
    digest = extract_error_digest(run.output, ["error:"], 2500, timed_out=run.timed_out, timeout_sec=1)
    assert "[TIMEOUT]" in digest and "start" in digest


def test_child_processes_are_killed_with_the_group(tmp_path: Path) -> None:
    marker = tmp_path / "child_alive"
    cmd = f"(sleep 2; touch {marker}) & sleep 30"
    run = LocalSandbox().run(cmd, cwd=tmp_path, timeout=1)
    assert run.timed_out
    time.sleep(2.5)
    assert not marker.exists()


def test_cancel_token_stops_command(tmp_path: Path) -> None:
    tok = CancelToken()
    threading.Timer(0.5, tok.set).start()
    run = LocalSandbox(tok).run("sleep 30", cwd=tmp_path, timeout=60)
    assert run.cancelled


def test_timeout_is_mandatory(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        LocalSandbox().run("true", cwd=tmp_path, timeout=0)


def test_on_line_streams_output(tmp_path: Path) -> None:
    lines: list[str] = []
    LocalSandbox().run(["bash", "-c", "echo a; echo b >&2"], cwd=tmp_path, timeout=10, on_line=lines.append)
    assert sorted(lines) == ["a", "b"]


# --- profiles ------------------------------------------------------------------------------------------


def test_all_five_profiles_load() -> None:
    profiles = load_profiles(Settings().profiles_dir)
    assert set(profiles) == {"c", "cpp", "python", "web", "verilog"}
    for p in profiles.values():
        assert "pid_init" not in p.test_instructions  # L2
        assert p.code_fence_tags and p.error_patterns
    assert "// TODO" in profiles["web"].scaffold and "# TODO" not in profiles["web"].scaffold  # B4
    assert "assert.h" not in profiles["c"].test_instructions.replace("`assert.h` は使わない", "")  # I1


def test_invalid_profile_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "bad.yaml").write_text("name: bad\ndisplay_name: x\nextensions: []\ntest_instructions: x\n"
                                       "scaffold: x\nstub_instructions: x\ngates: {syntax: 'true'}\n"
                                       "code_fence_tags: [x]\nerror_patterns: []\n")
    with pytest.raises(ProfileError, match="full"):
        load_profiles(tmp_path)


def test_command_rendering_quotes_paths_with_spaces() -> None:
    cmd = render_command("cc {impl} {test} -o {build_dir}/t", {"impl": ["/a b/x.c"], "test": "/c d/t.c",
                                                               "build_dir": "/tmp/x y"})
    assert cmd == "cc '/a b/x.c' '/c d/t.c' -o '/tmp/x y'/t"


def test_text_rendering_keeps_unknown_braces() -> None:
    assert render_text('#include "{if_name}"\nint f(void) { return 0; }', {"if_name": "a.h"}) == \
        '#include "a.h"\nint f(void) { return 0; }'


def test_workspace_override(tmp_path: Path) -> None:
    (tmp_path / ".autospec").mkdir()
    (tmp_path / ".autospec" / "workspace.yaml").write_text("profile: python\ngates:\n  full: 'make test'\n")
    profiles = load_profiles(Settings().profiles_dir)
    p = resolve_profile(profiles, None, tmp_path)
    assert p.name == "python" and p.gates["full"] == "make test"
    assert profiles["python"].gates["full"] != "make test"  # original untouched


def test_profile_guess_by_extension() -> None:
    profiles = load_profiles(Settings().profiles_dir)
    assert resolve_profile(profiles, None, None, ["src/a.py"]).name == "python"


def test_test_name_regex_uses_compact_id() -> None:
    p = load_profiles(Settings().profiles_dir)["c"]
    assert p.test_name_regex("REQ-001") == r"test_REQ001_\w+"


# --- config ---------------------------------------------------------------------------------------------


def test_env_overrides_and_harness_dir_is_absolute(tmp_path: Path) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text("ollama:\n  model: foo\npaths:\n  harness_dir: ./harness\n")
    s = load_settings(cfg, env={"AUTOSPEC_OLLAMA_NUM_CTX": "1234", "AUTOSPEC_SANDBOX_MODE": "docker",
                                "AIDER_BIN": "/opt/aider"})
    assert s.ollama.model == "foo" and s.ollama.num_ctx == 1234 and s.sandbox.mode == "docker"
    assert s.aider.bin == "/opt/aider"
    assert s.harness_dir == (tmp_path / "harness").resolve()

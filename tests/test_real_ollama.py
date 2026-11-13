"""Manual acceptance test with the real Ollama model and aider (pytest -m ollama).

Uses the normal settings resolution (AUTOSPEC_CONFIG or ./config.yaml), so run it on the machine that has
the model, e.g.:  AUTOSPEC_CONFIG=$PWD/config.yaml .venv/bin/pytest -m ollama -s
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from autospec.config import load_settings
from autospec.core.llm import OllamaClient

from .conftest import FIXTURES

pytestmark = pytest.mark.ollama


@pytest.fixture(scope="module")
def real_settings():  # type: ignore[no-untyped-def]
    s = load_settings()
    health = OllamaClient(s).health()
    if not health.get("model_available"):
        pytest.skip(f"Ollama model not available: {health}")
    if not shutil.which(s.aider.bin):
        pytest.skip(f"aider not found: {s.aider.bin}")
    return s


def test_c_sample_end_to_end(real_settings, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURES / "c_sample", ws)
    subprocess.run(["git", "init", "-q"], cwd=ws, check=True)
    env = {**os.environ, "AUTOSPEC_PATHS_DATA_DIR": str(tmp_path / "data")}
    proc = subprocess.run([sys.executable, "-m", "autospec", "run", "implement", "--lang", "c", "--spec", "spec.md",
                           "--interface", "sat.h", "--impl", "src/sat.c", "--test", "tests/test_sat.c", "--quiet"],
                          cwd=ws, env=env, capture_output=True, text=True, timeout=7200)
    print(proc.stdout[-6000:])
    assert proc.returncode in (0, 2), proc.stdout[-3000:] + proc.stderr[-2000:]
    job = next((tmp_path / "data" / "jobs").glob("*/job.json"))
    state = json.loads(job.read_text())["state"]
    assert state["gate_passed"] is True
    assert state["coverage"]["missing_must"] == []

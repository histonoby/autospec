"""Helpers shared by steps: path resolution, gate execution, file hashing, prompt fragments."""

from __future__ import annotations

import hashlib
import os
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..core.context import JobContext
from ..core.profiles import Profile
from ..core.textutil import count_failures, extract_error_digest, failed_test_names


def profile_of(ctx: JobContext) -> Profile:
    if ctx.profile is None:
        raise RuntimeError("this step needs a language profile")
    return ctx.profile


def paths(ctx: JobContext) -> dict[str, Any]:
    p = ctx.state.get("paths")
    if not isinstance(p, dict):
        raise RuntimeError("paths are not prepared (prepare_workspace must run first)")
    return p


def abs_list(ctx: JobContext, rels: list[str] | str | None) -> list[Path]:
    if not rels:
        return []
    if isinstance(rels, str):
        rels = [rels]
    return [ctx.wpath(r) for r in rels]


def read_rel(ctx: JobContext, rel: str | None) -> str:
    if not rel:
        return ""
    p = ctx.wpath(rel)
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


def read_many(ctx: JobContext, rels: list[str]) -> str:
    return "\n\n".join(read_rel(ctx, r) for r in rels)


def make_writable(p: Path) -> None:
    if p.exists():
        p.chmod(p.stat().st_mode | stat.S_IWUSR)


def write_rel(ctx: JobContext, rel: str, content: str) -> Path:
    p = ctx.wpath(rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    make_writable(p)
    p.write_text(content, encoding="utf-8")
    return p


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def fence_tag(ctx: JobContext) -> str:
    return profile_of(ctx).code_fence_tags[0]


def gate_values(ctx: JobContext) -> dict[str, Any]:
    p = paths(ctx)
    return {
        "interface": str(ctx.wpath(p["interface"])) if p.get("interface") else "",
        "interface_dir": str(ctx.wpath(p["interface"]).parent) if p.get("interface") else str(ctx.workdir),
        "impl": [str(x) for x in abs_list(ctx, p.get("impl"))],
        "test": [str(x) for x in abs_list(ctx, p.get("tests"))],
        "spec": str(ctx.wpath(p["spec"])) if p.get("spec") else "",
        "workdir": str(ctx.workdir),
        "harness": str(ctx.settings.harness_dir),
        "build_dir": str(ctx.build_dir),
        "python": sys.executable,
    }


@dataclass
class GateOutcome:
    gate: str
    returncode: int
    output: str
    digest: str
    rank: tuple[int, int]
    failed_tests: list[str] = field(default_factory=list)
    timed_out: bool = False
    command: str = ""

    @property
    def passed(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "returncode": self.returncode, "timed_out": self.timed_out,
                "rank": list(self.rank), "failures": self.rank[1], "failed_tests": self.failed_tests,
                "digest": self.digest}


def run_gate(ctx: JobContext, gate: str, *, quiet: bool = False) -> GateOutcome:
    prof = profile_of(ctx)
    cmd = prof.render_gate(gate, gate_values(ctx))
    timeout = prof.timeout_for(gate, ctx.settings.sandbox.gate_timeout_sec)
    run = ctx.sandbox.run(cmd, cwd=ctx.workdir, timeout=timeout,
                          env={"PYTHONPATH": str(ctx.workdir), "PYTHONDONTWRITEBYTECODE": "1"})
    if run.cancelled:
        ctx.cancel.check()
    out = run.output
    digest = extract_error_digest(out, prof.error_patterns, ctx.settings.pipeline.error_digest_chars,
                                  timed_out=run.timed_out, timeout_sec=timeout)
    rank = count_failures(out, 1 if run.timed_out else run.returncode, prof.failure_count_pattern,
                          prof.error_patterns)
    outcome = GateOutcome(gate=gate, returncode=run.returncode, output=out, digest=digest, rank=rank,
                          failed_tests=failed_test_names(out, prof.failed_test_pattern),
                          timed_out=run.timed_out, command=cmd)
    status = "PASS" if outcome.passed else ("TIMEOUT" if run.timed_out else "FAILED")
    if not quiet:
        ctx.events.emit("gate.result", f"{gate} gate {status}" + ("" if outcome.passed else f" (失敗 {rank[1]})"),
                        level="info" if outcome.passed else "warn",
                        data={"gate": gate, "returncode": run.returncode, "timed_out": run.timed_out,
                              "output_tail": digest[-1500:], "failures": rank[1]})
    return outcome


def requirements_text(ctx: JobContext) -> str:
    reqs = (ctx.state.get("requirements") or {}).get("requirements") or []
    lines = []
    for r in reqs:
        crit = "; ".join(r.get("acceptance_criteria", []))
        lines.append(f"- {r['req_id']} [{r.get('priority', 'must')}] {r.get('title', '')}: "
                     f"{r.get('description', '')}" + (f"（受入基準: {crit}）" if crit else ""))
    return "\n".join(lines) or "（要件一覧なし）"


def chmod_readonly(p: Path) -> None:
    if p.exists():
        p.chmod(stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def rel_to_workdir(ctx: JobContext, p: Path) -> str:
    return os.path.relpath(p.resolve(), ctx.workdir.resolve())

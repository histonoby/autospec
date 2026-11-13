"""prepare_workspace / finalize (§8.5)."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..core.context import JobContext
from ..core.step import Step, StepResult
from ..core.workspace import commit_paths, diff, safe_join
from .common import paths, profile_of, read_rel, rel_to_workdir, write_rel


def _as_list(v: object) -> list[str]:
    if not v:
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


class PrepareWorkspace(Step):
    name = "prepare_workspace"

    def run(self, ctx: JobContext) -> StepResult:
        ws = ctx.workspace
        if ctx.state.get("paths") and ctx.state.get("workdir"):
            ctx.workdir = Path(ctx.state["workdir"])
            return StepResult.done("準備済み（再開）")
        ws.ensure_repo()
        ws.exclude_locally([".aider*"])  # aider's repo-map cache must not show up as an untracked change
        mode = ctx.opt("workdir_mode", "inplace")
        root = ws.root.resolve()

        def rel_in_ws(value: str) -> str:
            return str(safe_join(root, Path(value).expanduser()).relative_to(root))

        inp = ctx.inputs
        spec: str | None = rel_in_ws(inp["spec_path"]) if inp.get("spec_path") else None
        interface: str | None = rel_in_ws(inp["interface_path"]) if inp.get("interface_path") else None
        impls = [rel_in_ws(x) for x in _as_list(inp.get("impl_path"))]
        tests = [rel_in_ws(x) for x in _as_list(inp.get("test_path"))]
        if mode == "worktree":
            workdir, branch = ws.create_worktree(ctx.job_id, ctx.settings.data_dir / "worktrees")
            for rel in [x for x in (spec, interface) if x] + impls + tests:
                if (root / rel).exists() and not (workdir / rel).exists():
                    (workdir / rel).parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(root / rel, workdir / rel)
        else:
            workdir, branch = root, ws.default_branch()
        ctx.workdir = workdir
        ctx.state.update(workdir=str(workdir), branch=branch, workdir_mode=mode)

        if inp.get("spec_text") and not spec:
            spec = ".autospec/spec.md"
            write_rel(ctx, spec, str(inp["spec_text"]))
        if not spec or not ctx.wpath(spec).exists():
            return StepResult.failed(f"仕様書が見つかりません: {inp.get('spec_path')}")
        if ctx.opt("require_interface", True) and (not interface or not ctx.wpath(interface).exists()):
            return StepResult.failed(f"インターフェース定義が見つかりません: {inp.get('interface_path')}")
        if not impls or not tests:
            return StepResult.failed("impl_path と test_path を指定してください")
        ctx.state["paths"] = {"spec": spec, "interface": interface, "impl": impls, "tests": tests}

        prof = profile_of(ctx)
        for impl in impls:
            if not read_rel(ctx, impl).strip():
                if_name = Path(interface).name if interface else ""
                if_content = read_rel(ctx, interface)
                write_rel(ctx, impl, prof.render(prof.scaffold, if_name=if_name, if_content=if_content,
                                                 impl_name=Path(impl).name, impl_stem=Path(impl).stem))
                ctx.events.log(f"実装ファイルを雛形で作成しました: {impl}")
        for t in tests:
            ctx.wpath(t).parent.mkdir(parents=True, exist_ok=True)

        inputs = [x for x in (spec, interface) if x]
        snap = commit_paths(workdir, [*inputs, *impls], f"autospec: inputs for job {ctx.job_id}")
        ctx.state["base_commit"] = snap
        ctx.state["touched_files"] = sorted({*impls, *tests, *inputs})
        ctx.events.log(f"作業ディレクトリ: {workdir}（{mode}, branch {branch}）")
        return StepResult.done(f"workdir={workdir}")


class Finalize(Step):
    name = "finalize"

    def run(self, ctx: JobContext) -> StepResult:
        p = paths(ctx)
        touched = list(dict.fromkeys(ctx.state.get("touched_files", [])))
        for extra in ctx.state.get("support_copies", []):
            if extra not in touched:
                touched.append(extra)
        head = commit_paths(ctx.workdir, touched, f"autospec: job {ctx.job_id} result")
        base = ctx.state.get("base_commit")
        patch = diff(ctx.workdir, base) if base else ""
        ctx.artifacts.write("diff.patch", patch)
        ctx.state["final_commit"] = head
        files = sorted({*p.get("impl", []), *p.get("tests", [])})
        return StepResult.done(f"コミット {head[:10]}、差分 {len(patch.splitlines())} 行",
                               files=[rel_to_workdir(ctx, ctx.wpath(f)) for f in files])

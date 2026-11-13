"""integrity_check (§8.5, I10)."""

from __future__ import annotations

from ..core.context import JobContext
from ..core.step import Step, StepResult
from .testgen import verify_hashes


class IntegrityCheck(Step):
    name = "integrity_check"

    def run(self, ctx: JobContext) -> StepResult:
        if not ctx.state.get("test_hashes"):
            return StepResult.skipped("ロック済みテストがありません")
        bad = verify_hashes(ctx)
        if bad:
            return StepResult.failed(f"テストファイルが改変されています: {', '.join(bad)}", error="test_tampered")
        return StepResult.done(f"{len(ctx.state['test_hashes'])} ファイルのハッシュ一致")

"""approval: pause the job until a human decides (§8.5). Decisions are stored in state['approvals']."""

from __future__ import annotations

from typing import Any

from ..core.context import JobContext
from ..core.step import Step, StepResult, StepStatus


def record_decision(state: dict[str, Any], target: str, decision: str, comment: str = "",
                    edited_content: str | None = None) -> None:
    approvals = state.setdefault("approvals", {})
    entry = approvals.setdefault(target, {"rejects": 0})
    entry.update(decision=decision, comment=comment, edited_content=edited_content)
    if decision == "reject":
        entry["rejects"] = int(entry.get("rejects", 0)) + 1


class Approval(Step):
    name = "approval"

    def should_skip(self, ctx: JobContext) -> bool:
        target = self.params.get("target", "")
        return bool(ctx.opt("auto_approve", False)) or target in ctx.opt("auto_approve_targets", [])

    def run(self, ctx: JobContext) -> StepResult:
        target = str(self.params.get("target", "default"))
        entry = (ctx.state.get("approvals") or {}).get(target) or {}
        decision = entry.get("decision")
        if decision == "approve":
            entry["consumed"] = True
            ctx.events.emit("approval.resolved", f"{target}: 承認", data={"target": target, "decision": "approve"})
            return StepResult.done("承認済み", edited=entry.get("edited_content") is not None)
        if decision == "reject":
            limit = int(ctx.opt("approval_max_rejects", ctx.settings.pipeline.approval_max_rejects))
            ctx.events.emit("approval.resolved", f"{target}: 却下", data={"target": target, "decision": "reject",
                                                                        "comment": entry.get("comment")})
            if int(entry.get("rejects", 0)) > limit:
                return StepResult.failed(f"{target} が {limit} 回を超えて却下されました", error="rejected")
            entry["decision"] = None
            ctx.state.setdefault("reject_comments", {})[target] = entry.get("comment", "")
            back = self.params.get("redo_step")
            if back:
                return StepResult.jump(str(back), f"却下コメントを反映して {back} をやり直します")
            return StepResult.failed(f"{target} が却下されました: {entry.get('comment', '')}", error="rejected")
        if not ctx.interactive:
            return StepResult.failed(f"承認が必要です（{target}）。CLI では autospec approve で承認後に resume してください",
                                     error="approval_required")
        ctx.events.emit("approval.required", f"{target} の承認待ち",
                        data={"target": target, "content": ctx.state.get("approval_content", {}).get(target)})
        return StepResult(StepStatus.WAITING_APPROVAL, f"{target} の承認待ち")

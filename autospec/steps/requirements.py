"""requirements_extract (§8.5)."""

from __future__ import annotations

import json

from ..core.context import JobContext
from ..core.schemas import RequirementList
from ..core.step import Step, StepResult
from .common import paths, read_rel

SYSTEM = "あなたは組込み・業務ソフトウェアの要求分析の専門家です。仕様書から検証可能な要件を漏れなく抽出します。"

PROMPT = """次の仕様書から要件一覧を抽出してください。

# 仕様書
{spec}

# ルール
- 仕様書に要件ID（例: REQ-001）があればそのIDを使うこと。無ければ REQ-001 から連番を振ること。
- 1要件は1つの検証可能な振る舞いにすること。受入基準は具体的な入力と期待値で書くこと。
- priority は must（仕様が明示的に要求）/ should（推奨）/ may（任意）。
- 仕様が曖昧な点は assumptions に置いた前提を、判断できない点は open_questions に書くこと。
"""


class RequirementsExtract(Step):
    name = "requirements_extract"

    def run(self, ctx: JobContext) -> StepResult:
        if ctx.state.get("requirements"):
            return StepResult.done("抽出済み（再利用）")
        given = ctx.inputs.get("requirements_path")
        if given:
            data = RequirementList.model_validate(json.loads(ctx.wpath(given).read_text(encoding="utf-8")))
        else:
            spec = read_rel(ctx, paths(ctx)["spec"])
            messages = [{"role": "system", "content": SYSTEM},
                        {"role": "user", "content": PROMPT.format(spec=spec)}]
            data = ctx.llm.chat(messages, think=bool(ctx.opt("think_requirements", True)), schema=RequirementList)
        seen: set[str] = set()
        for r in data.requirements:
            if r.req_id in seen:
                return StepResult.failed(f"要件IDが重複しています: {r.req_id}")
            seen.add(r.req_id)
        dumped = data.model_dump()
        ctx.state["requirements"] = dumped
        ctx.artifacts.write("requirements.json", dumped)
        must = sum(1 for r in data.requirements if r.priority == "must")
        return StepResult.done(f"要件 {len(data.requirements)} 件（must {must} 件）")

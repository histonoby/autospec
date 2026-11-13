"""audit (§8.5, §8.6): the LLM judges each requirement; overall status and score are computed mechanically."""

from __future__ import annotations

import re
from typing import Any

from ..core.context import JobContext
from ..core.schemas import AuditedRequirement, AuditLLMOutput, AuditReport
from ..core.step import Step, StepResult
from ..core.textutil import md_cell
from .common import paths, read_many, read_rel, requirements_text

SYSTEM = "あなたは厳格なソフトウェア監査人です。仕様書・テスト結果・実装を照合し、要件ごとに根拠付きで判定します。"

PROMPT = """次の実装が仕様を満たしているか、要件ごとに監査してください。

# 仕様書
{spec}

# 要件一覧
{requirements}

# インターフェース定義
{interface}

# 実装
{impl}

# テスト
{tests}

# ゲート（ビルド＋テスト実行）の結果: {gate_status}
```
{gate_output}
```

# 網羅確認（要件IDごとのテスト有無）
{coverage}

# ルール
- 要件一覧のすべての要件について status を PASS / FAIL / PARTIAL で判定すること。
- implemented_in には実装の関数名だけを、verified_by にはテスト名だけを、実際にコードに書かれている綴りで挙げること。
- 判定の根拠を note に書くこと。総合判定やスコアは書かないこと（機械的に計算します）。
"""

_SCORE = {"PASS": 1.0, "PARTIAL": 0.5, "FAIL": 0.0}


def _symbol(name: str) -> str:
    name = name.strip().strip("`").strip()
    name = re.sub(r"\(.*\)$", "", name).strip()
    for sep in ("::", ":", "#", "."):
        if sep in name and not re.match(r"^[\w-]+\.(c|h|cpp|hpp|py|ts|sv|v)$", name):
            name = name.split(sep)[-1]
    return name.strip()


def _exists(name: str, text: str, files: list[str]) -> bool:
    sym = _symbol(name)
    if not sym:
        return False
    if any(f.endswith(sym) for f in files):
        return True
    return re.search(rf"(?<![\w]){re.escape(sym)}(?![\w])", text) is not None


def build_report(llm_out: AuditLLMOutput, requirements: list[dict[str, Any]], *, gate_passed: bool,
                 impl_text: str, test_text: str, impl_files: list[str], test_files: list[str],
                 coverage: dict[str, Any], gate_digest: str) -> AuditReport:
    prio = {r["req_id"]: r.get("priority", "must") for r in requirements}
    items: list[AuditedRequirement] = []
    seen: set[str] = set()
    for it in llm_out.requirements:
        seen.add(it.req_id)
        missing_impl = [n for n in it.implemented_in if not _exists(n, impl_text, impl_files)]
        missing_tests = [n for n in it.verified_by if not _exists(n, test_text, test_files)]
        unverified = bool(missing_impl or missing_tests) or (it.status == "PASS" and not it.verified_by)
        items.append(AuditedRequirement(**it.model_dump(), priority=prio.get(it.req_id, "should"),
                                        missing_impl=missing_impl, missing_tests=missing_tests,
                                        unverified=unverified))
    for r in requirements:
        if r["req_id"] not in seen:
            items.append(AuditedRequirement(req_id=r["req_id"], description=r.get("title", ""), status="FAIL",
                                            implemented_in=[], verified_by=[], note="監査結果に含まれていません",
                                            priority=r.get("priority", "must"), unverified=True))
    reasons: list[str] = []
    if not gate_passed:
        reasons.append("ゲート未通過")
    not_pass = [i.req_id for i in items if i.priority == "must" and i.status != "PASS"]
    if not_pass:
        reasons.append("must 要件が PASS でない: " + ", ".join(not_pass))
    unv = [i.req_id for i in items if i.unverified]
    if unv:
        reasons.append("UNVERIFIED（挙げた名前がコードに無い／根拠なし）: " + ", ".join(unv))
    score = round(100 * sum(_SCORE[i.status] for i in items) / len(items), 1) if items else 0.0
    return AuditReport(overall_status="WARN" if reasons else "PASS", score=score, gate_passed=gate_passed,
                       requirements=items, potential_risks=llm_out.potential_risks, summary=llm_out.summary,
                       coverage=coverage, gate_digest=gate_digest, reasons=reasons)


def render_markdown(report: AuditReport, job_id: str) -> str:
    lines = [f"# 監査レポート（{job_id}）", "",
             f"- 総合判定: **{report.overall_status}**",
             f"- スコア: {report.score}",
             f"- ゲート: {'PASS' if report.gate_passed else 'FAILED'}"]
    for r in report.reasons:
        lines.append(f"- 理由: {md_cell(r)}")
    lines += ["", "## 要件ごとの判定", "",
              "| 要件ID | 優先度 | 判定 | 実装 | 検証テスト | 備考 |", "| :-- | :-- | :-- | :-- | :-- | :-- |"]
    for i in report.requirements:
        status = i.status + (" / UNVERIFIED" if i.unverified else "")
        impl = ", ".join(n + (" ❓" if n in i.missing_impl else "") for n in i.implemented_in)
        tests = ", ".join(n + (" ❓" if n in i.missing_tests else "") for n in i.verified_by)
        lines.append("| " + " | ".join(md_cell(x) for x in (i.req_id, i.priority, status, impl, tests, i.note)) + " |")
    lines += ["", "❓ = コード中に見つからない名前", "", "## 要約", "", report.summary, ""]
    if report.potential_risks:
        lines += ["## 潜在リスク", ""] + [f"- {r}" for r in report.potential_risks] + [""]
    cov = report.coverage or {}
    if cov:
        lines += ["## 網羅確認", "",
                  f"- must 要件でテストが無いもの: {', '.join(cov.get('missing_must', [])) or 'なし'}",
                  f"- その他でテストが無いもの: {', '.join(cov.get('missing_other', [])) or 'なし'}", ""]
    if report.gate_digest:
        lines += ["## ゲート出力（要約）", "", "```", report.gate_digest, "```", ""]
    return "\n".join(lines)


class Audit(Step):
    name = "audit"

    def run(self, ctx: JobContext) -> StepResult:
        p = paths(ctx)
        impl_text = read_many(ctx, p["impl"])
        test_text = read_many(ctx, p["tests"])
        gate_passed = bool(ctx.state.get("gate_passed", False))
        gate_output = str(ctx.state.get("gate_output", ""))
        coverage = ctx.state.get("coverage") or {}
        cov_text = "\n".join(f"- {r['req_id']}: {', '.join(r['tests']) or 'テストなし'}"
                             for r in coverage.get("requirements", [])) or "（なし）"
        prompt = PROMPT.format(spec=read_rel(ctx, p["spec"]), requirements=requirements_text(ctx),
                               interface=read_rel(ctx, p.get("interface")), impl=impl_text, tests=test_text,
                               gate_status="PASS" if gate_passed else "FAILED",
                               gate_output=gate_output or "(出力なし)", coverage=cov_text)
        llm_out = ctx.llm.chat([{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
                               think=bool(ctx.opt("think_audit", True)), schema=AuditLLMOutput)
        reqs = (ctx.state.get("requirements") or {}).get("requirements") or []
        report = build_report(llm_out, reqs, gate_passed=gate_passed, impl_text=impl_text + read_rel(ctx, p.get("interface")),
                              test_text=test_text, impl_files=p["impl"], test_files=p["tests"],
                              coverage=coverage, gate_digest=gate_output)
        ctx.artifacts.write("audit_result.json", report.model_dump())
        ctx.artifacts.write("audit_report.md", render_markdown(report, ctx.job_id))
        ctx.state["audit"] = {"overall_status": report.overall_status, "score": report.score,
                              "reasons": report.reasons}
        return StepResult.done(f"監査 {report.overall_status}（スコア {report.score}）")

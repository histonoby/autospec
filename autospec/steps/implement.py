"""implement: Aider loop (§9) with per-round commits, best-round restore and test-dispute escalation."""

from __future__ import annotations

import re
import shlex
import sys
from typing import Any

from ..core.context import JobCancelled, JobContext
from ..core.schemas import TestVerdict
from ..core.step import Step, StepResult, StepStatus
from ..core.workspace import commit_paths, restore_paths
from .common import paths, profile_of, read_many, read_rel, requirements_text, run_gate
from .testgen import verify_hashes

INFRA_PATTERNS = [
    r"APIConnectionError", r"Connection refused", r"Failed to establish a new connection",
    r"OllamaException", r"litellm\.NotFoundError", r"model ['\"]?[\w.:/-]+['\"]? not found",
    r"command not found", r"No such file or directory: '?aider", r"Unknown model",
]

PROMPT = """{impl_list} を実装してください。

- 仕様書: {spec}
- インターフェース定義: {interface}
- テスト（読み取り専用・変更禁止）: {tests}

# 要件一覧
{requirements}

# ルール
- テストファイルとインターフェース定義は変更しないこと。編集してよいのは {impl_list} だけです。
- インターフェースで宣言されたすべての関数を、仕様書どおりに実装すること。
- テスト専用の分岐やテスト名による条件分岐を書かないこと。
{feedback}"""


def _aider_argv(ctx: JobContext, editable: list[str], reads: list[str], message: str) -> list[str]:
    s = ctx.settings
    aider_dir = ctx.job_dir / "aider"
    aider_dir.mkdir(parents=True, exist_ok=True)
    test_cmd = shlex.join([sys.executable, "-m", "autospec", "gate", "--job", ctx.job_id,
                           "--data-dir", str(s.data_dir), "full"])
    argv = [s.aider.bin, "--model", f"ollama_chat/{s.ollama.model}", "--edit-format", "whole",
            "--yes-always", "--no-auto-commits", "--no-show-model-warnings", "--no-suggest-shell-commands",
            # keep aider non-interactive and out of the user's tree (history files go to the job dir)
            "--no-check-update", "--no-show-release-notes", "--analytics-disable", "--no-gitignore",
            "--no-pretty", "--no-fancy-input",
            "--chat-history-file", str(aider_dir / "chat.history.md"),
            "--input-history-file", str(aider_dir / "input.history"),
            "--test-cmd", test_cmd, "--auto-test"]
    for r in reads:
        argv += ["--read", r]
    argv += list(s.aider.extra_args)
    argv += ["--message", message, *editable]
    return argv


def _emit_line(ctx: JobContext, line: str) -> None:
    ctx.events.emit("aider.output", line)


def _is_infra_failure(output: str) -> bool:
    return any(re.search(p, output, re.IGNORECASE) for p in INFRA_PATTERNS)


class Implement(Step):
    name = "implement"

    def _editable(self, ctx: JobContext) -> list[str]:
        p = paths(ctx)
        if self.params.get("mode") == "modify" and ctx.state.get("plan"):
            changes = ctx.state["plan"].get("changes", [])
            return [c["path"] for c in changes if c.get("action") in ("create", "modify")]
        return list(p["impl"])

    def run(self, ctx: JobContext) -> StepResult:
        prof = profile_of(ctx)
        s = ctx.settings
        p = paths(ctx)
        editable = self._editable(ctx)
        tests = list(p["tests"])
        reads = [x for x in (p.get("spec"), p.get("interface")) if x] + tests
        max_rounds = int(ctx.opt("max_rounds", s.aider.max_rounds))
        suspect_after = int(ctx.opt("suspect_after_rounds", s.aider.suspect_after_rounds))
        rounds: list[dict[str, Any]] = list(ctx.state.get("rounds", []))
        streak: dict[str, int] = dict(ctx.state.get("fail_streak", {}))
        reviewed: list[str] = list(ctx.state.get("reviewed_tests", []))
        feedback = ctx.state.pop("implement_feedback", "")
        if rounds and not feedback:
            feedback = rounds[-1].get("digest", "")
        start = len(rounds) + 1
        passed = False
        env = {"OLLAMA_API_BASE": s.ollama.url, "AUTOSPEC_PATHS_DATA_DIR": str(s.data_dir)}

        for rnd in range(start, start + max_rounds):
            ctx.cancel.check()
            fb = ""
            if feedback:
                fb = "\n# 前回のテスト結果（エラー要約）\n```\n" + feedback + "\n```\nこのエラーを解消してください。\n"
            message = PROMPT.format(impl_list=", ".join(editable), spec=p.get("spec"), interface=p.get("interface"),
                                    tests=", ".join(tests), requirements=requirements_text(ctx),
                                    feedback=fb).rstrip() + "\n/no_think"
            ctx.state["aider_round"] = rnd
            ctx.events.log(f"Aider ラウンド {rnd}/{start + max_rounds - 1}", round=rnd)
            argv = _aider_argv(ctx, editable, reads, message)
            try:
                run = ctx.sandbox.run(argv, cwd=ctx.workdir, timeout=int(ctx.opt("aider_timeout_sec", s.aider.timeout_sec)),
                                      on_line=lambda line: _emit_line(ctx, line), env=env)
            except FileNotFoundError as e:
                return StepResult.failed(f"aider を起動できません: {e}", error="infra_error")
            if run.cancelled:
                raise JobCancelled("cancelled during aider")
            if run.returncode != 0 and not run.timed_out and _is_infra_failure(run.output):
                ctx.state["rounds"] = rounds
                return StepResult.failed(
                    f"aider が環境エラーで終了しました（rc={run.returncode}）。ラウンドは消費していません: "
                    + run.output.strip()[-400:], error="infra_error")
            bad = verify_hashes(ctx)
            if bad:
                return StepResult.failed(f"Aider がテストファイルを変更しました: {', '.join(bad)}", error="test_tampered")

            gate = run_gate(ctx, "full")
            post_ok = True
            if gate.passed:
                for pg in prof.post_gates:
                    po = run_gate(ctx, pg)
                    if not po.passed:
                        post_ok = False
                        gate.digest = f"[{pg}] " + po.digest
                        gate.rank = (0, max(1, po.rank[1]))
                        break
            ok = gate.passed and post_ok
            sha = commit_paths(ctx.workdir, editable,
                               f"autospec: job {ctx.job_id} round {rnd} (failures={0 if ok else gate.rank[1]})")
            rec = {"round": rnd, "aider_returncode": run.returncode, "aider_timed_out": run.timed_out,
                   "gate_returncode": gate.returncode, "passed": ok, "rank": [0, 0] if ok else list(gate.rank),
                   "failures": 0 if ok else gate.rank[1], "failed_tests": gate.failed_tests, "commit": sha,
                   "digest": "" if ok else gate.digest}
            rounds.append(rec)
            ctx.state["rounds"] = rounds
            ctx.state["gate_output"] = gate.digest
            ctx.artifacts.write("rounds.json", rounds)
            if ok:
                passed = True
                break
            feedback = gate.digest

            for name in list(streak):
                if name not in gate.failed_tests:
                    streak.pop(name)
            for name in gate.failed_tests:
                streak[name] = streak.get(name, 0) + 1
            ctx.state["fail_streak"] = streak
            suspects = [n for n, c in streak.items() if c >= suspect_after and n not in reviewed]
            if suspects:
                res = self._review(ctx, suspects[0], gate.digest, len(reviewed) + 1)
                reviewed.append(suspects[0])
                ctx.state["reviewed_tests"] = reviewed
                if res is not None:
                    return res

        if passed:
            ctx.state["gate_passed"] = True
            ctx.state["best_round"] = rounds[-1]["round"]
            return StepResult.done(f"ラウンド {rounds[-1]['round']} でゲート通過")
        ctx.state["gate_passed"] = False
        if not rounds:
            return StepResult.failed("Aider ラウンドが実行されませんでした")
        best = min(rounds, key=lambda r: (tuple(r["rank"]), -r["round"]))
        ctx.state["best_round"] = best["round"]
        ctx.state["gate_output"] = best["digest"]
        if best is not rounds[-1]:
            restore_paths(ctx.workdir, best["commit"], editable)
            commit_paths(ctx.workdir, editable, f"autospec: job {ctx.job_id} restore best round {best['round']}")
            ctx.events.warn(f"ゲート未通過。失敗が最少だったラウンド {best['round']}（失敗 {best['failures']}）に戻しました")
        return StepResult.done(f"ゲート未通過（最良ラウンド {best['round']}、失敗 {best['failures']}）")

    def _review(self, ctx: JobContext, test_name: str, digest: str, n: int) -> StepResult | None:
        p = paths(ctx)
        prompt = (f"テスト `{test_name}` が {ctx.opt('suspect_after_rounds', ctx.settings.aider.suspect_after_rounds)} "
                  "ラウンド連続で失敗しています。テスト側が仕様とずれていないかを判定してください。\n\n"
                  f"# 仕様書\n{read_rel(ctx, p['spec'])}\n\n# 要件一覧\n{requirements_text(ctx)}\n\n"
                  f"# テストコード\n{read_many(ctx, p['tests'])}\n\n# 失敗内容\n{digest}\n\n"
                  "仕様に照らしてテストの期待値が誤っている場合のみ suspect=true とし、根拠を reason に書いてください。")
        verdict = ctx.llm.chat([{"role": "user", "content": prompt}], think=True, schema=TestVerdict)
        ctx.artifacts.write(f"test_review_{n}.json", verdict.model_dump())
        if not verdict.suspect:
            ctx.events.log(f"テスト疑義なし: {test_name}（{verdict.reason[:120]}）")
            return None
        ctx.state["test_dispute"] = verdict.model_dump()
        msg = f"テスト {test_name}（{verdict.req_id}）が仕様とずれている疑い: {verdict.reason}"
        if not ctx.interactive:
            return StepResult.failed(msg + "。CLI 実行のため中止します。テストを確認して再実行してください",
                                     error="test_dispute")
        ctx.state.setdefault("approval_content", {})["test_dispute"] = verdict.model_dump()
        ctx.events.emit("approval.required", msg, data={"target": "test_dispute", **verdict.model_dump()})
        return StepResult(StepStatus.WAITING_APPROVAL, msg)


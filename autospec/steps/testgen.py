"""test_gen / coverage_check / red_check / lock_tests (§8.5)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from ..core.context import JobContext
from ..core.llm import ContextBudgetExceeded, compress_for_retry, retry_temperature
from ..core.step import Step, StepResult
from ..core.textutil import ExtractionError, extract_code_block
from .common import (
    chmod_readonly,
    fence_tag,
    make_writable,
    paths,
    profile_of,
    read_many,
    read_rel,
    rel_to_workdir,
    requirements_text,
    run_gate,
    sha256_file,
    write_rel,
)

SYSTEM = ("あなたは熟練したテストエンジニアです。仕様書とインターフェース定義だけを根拠に、"
          "実装に依存しない単体テストを書きます。")

PROMPT = """次の仕様を検証する単体テストを書いてください。

# 仕様書
{spec}

# インターフェース定義（{if_name}）
```{tag}
{interface}
```

# 要件一覧
{requirements}

# 言語ごとの要件
{instructions}

# 共通ルール
- テスト名に要件IDを含めること。命名例: `{example}`（正規表現 `{pattern}` に一致させる）。
- must の要件には必ず1つ以上のテストを書くこと。1つのテストは1つの要件を検証すること。
- 補助ファイル {support} を使い、失敗しても止まらずに全件の結果を出すこと（テストと同じディレクトリに置かれます）。
{previous}{feedback}
出力は ```{tag} のコードブロック1つだけにし、テストファイルの全文を書いてください。説明やビルド例は不要です。
"""


def copy_support_files(ctx: JobContext, test_rel: str) -> list[str]:
    prof = profile_of(ctx)
    copied: list[str] = []
    dest_dir = ctx.wpath(test_rel).parent
    dest_dir.mkdir(parents=True, exist_ok=True)
    for name in prof.support_files:
        src = ctx.settings.test_support_dir / name
        if not src.exists():
            raise FileNotFoundError(f"support file missing: {src}")
        dest = dest_dir / name
        if not dest.exists() or dest.read_bytes() != src.read_bytes():
            make_writable(dest)
            shutil.copyfile(src, dest)
        chmod_readonly(dest)
        copied.append(rel_to_workdir(ctx, dest))
    ctx.state["support_copies"] = sorted(set(ctx.state.get("support_copies", [])) | set(copied))
    return copied


class TestGen(Step):
    __test__ = False
    name = "test_gen"

    def run(self, ctx: JobContext) -> StepResult:
        prof = profile_of(ctx)
        p = paths(ctx)
        test_rel = p["tests"][0]
        support = copy_support_files(ctx, test_rel)
        feedback = ctx.state.pop("test_gen_feedback", None)
        existing = read_rel(ctx, test_rel)
        if ctx.opt("reuse_tests", False) and existing.strip() and not feedback:
            return StepResult.done("既存のテストを再利用します（reuse_tests）")
        if self.params.get("mode") == "modify" and existing.strip() and not feedback:
            return StepResult.failed(f"modify モードでは既存テストを上書きしません: {test_rel}")

        tag = fence_tag(ctx)
        reqs = (ctx.state.get("requirements") or {}).get("requirements") or []
        example_id = reqs[0]["req_id"] if reqs else "REQ-001"
        example = prof.test_name_regex(example_id).replace(r"\w+", "<内容>")
        interface_rel = p.get("interface")
        previous = ""
        if feedback and existing.strip():
            previous = f"\n# 前回生成したテスト\n```{tag}\n{existing}\n```\n"
        prompt = PROMPT.format(
            spec=read_rel(ctx, p["spec"]), if_name=Path(interface_rel).name if interface_rel else "",
            tag=tag, interface=read_rel(ctx, interface_rel), requirements=requirements_text(ctx),
            instructions=prof.render(prof.test_instructions, if_name=Path(interface_rel).name if interface_rel else "",
                                     impl_name=Path(p["impl"][0]).name, impl_stem=Path(p["impl"][0]).stem,
                                     test_name=Path(test_rel).name),
            example=example, pattern=prof.test_name_pattern, support=", ".join(Path(s).name for s in support) or "なし",
            previous=previous, feedback=f"\n# 前回の指摘（必ず反映すること）\n{feedback}\n" if feedback else "")
        messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}]
        retries = int(ctx.opt("test_gen_max_retries", ctx.settings.pipeline.test_gen_max_retries))
        think = bool(ctx.opt("think", False))
        last_problem = ""
        for attempt in range(retries + 1):
            temp = retry_temperature(ctx.settings, None, attempt)
            try:
                resp = ctx.llm.chat(messages, think=think, temperature=temp)
            except ContextBudgetExceeded as e:
                return StepResult.failed(str(e), error="context_budget")
            try:
                code = extract_code_block(resp, prof.code_fence_tags, prof.sanity_patterns)
            except ExtractionError as e:
                last_problem = str(e)
                ctx.events.warn(f"コード抽出に失敗（{attempt + 1}/{retries + 1}）: {e}")
                messages = compress_for_retry(
                    messages, resp, f"{e}\n```{tag} のコードブロック1つでテストファイルの全文を出力してください。")
                continue
            write_rel(ctx, test_rel, code)
            gate = run_gate(ctx, "syntax")
            if gate.passed:
                ctx.state["test_gen_attempts"] = attempt + 1
                return StepResult.done(f"テスト生成完了（{attempt + 1} 回目で構文チェック通過）")
            last_problem = gate.digest
            messages = compress_for_retry(
                messages, resp,
                "生成したテストが構文チェックに失敗しました。エラーの要約:\n```\n" + gate.digest +
                f"\n```\n修正したテストファイルの全文を ```{tag} のコードブロック1つで出力してください。")
        return StepResult.failed(f"テスト生成が {retries + 1} 回とも失敗しました: {last_problem[:300]}",
                                 error="test_gen_failed")


class CoverageCheck(Step):
    name = "coverage_check"

    def run(self, ctx: JobContext) -> StepResult:
        reqs = (ctx.state.get("requirements") or {}).get("requirements") or []
        if not reqs:
            return StepResult.skipped("要件一覧がありません")
        prof = profile_of(ctx)
        tests_text = read_many(ctx, paths(ctx)["tests"])
        rows = []
        for r in reqs:
            pattern = prof.test_name_regex(r["req_id"])
            found = sorted(set(re.findall(pattern, tests_text)))
            rows.append({"req_id": r["req_id"], "priority": r.get("priority", "must"), "pattern": pattern,
                         "tests": found, "covered": bool(found)})
        missing_must = [x["req_id"] for x in rows if x["priority"] == "must" and not x["covered"]]
        missing_other = [x["req_id"] for x in rows if x["priority"] != "must" and not x["covered"]]
        attempts = int(ctx.state.get("coverage_retries", 0))
        report = {"requirements": rows, "missing_must": missing_must, "missing_other": missing_other,
                  "retries": attempts}
        ctx.state["coverage"] = report
        ctx.artifacts.write("coverage.json", report)
        if missing_must:
            limit = int(ctx.opt("coverage_max_retries", ctx.settings.pipeline.coverage_max_retries))
            if attempts < limit:
                ctx.state["coverage_retries"] = attempts + 1
                ids = ", ".join(missing_must)
                ctx.state["test_gen_feedback"] = (
                    f"次の must 要件にテストがありません: {ids}。既存のテストは残したまま、"
                    f"テスト名に要件IDを含めたテストを追加してください。")
                return StepResult.jump("test_gen", f"must 要件のテスト不足: {ids}")
            ctx.events.warn(f"must 要件のテスト不足が解消しませんでした: {', '.join(missing_must)}")
        return StepResult.done(f"網羅 {sum(x['covered'] for x in rows)}/{len(rows)}")


STUB_PROMPT = """次のインターフェースの最小スタブ実装を書いてください。

# インターフェース定義（{if_name}）
```{tag}
{interface}
```

# 指示
{instructions}
- 実装ファイル名は {impl_name} です。ビルドが通ることだけを目的とし、仕様どおりの処理はしないこと。

出力は ```{tag} のコードブロック1つだけにしてください。
"""


class RedCheck(Step):
    name = "red_check"

    def should_skip(self, ctx: JobContext) -> bool:
        return not profile_of(ctx).red_check or bool(ctx.opt("skip_red_check", False))

    def run(self, ctx: JobContext) -> StepResult:
        prof = profile_of(ctx)
        p = paths(ctx)
        impl_rel = p["impl"][0]
        impl_path = ctx.wpath(impl_rel)
        original = impl_path.read_bytes() if impl_path.exists() else None
        tag = fence_tag(ctx)
        if_name = Path(p["interface"]).name if p.get("interface") else ""
        messages = [{"role": "user", "content": STUB_PROMPT.format(
            if_name=if_name, tag=tag, interface=read_rel(ctx, p.get("interface")), impl_name=impl_path.name,
            instructions=prof.render(prof.stub_instructions, if_name=if_name, impl_name=impl_path.name,
                                     impl_stem=impl_path.stem))}]
        outcome = None
        for attempt in range(2):
            resp = ctx.llm.chat(messages, temperature=retry_temperature(ctx.settings, None, attempt))
            try:
                stub = extract_code_block(resp, prof.code_fence_tags)
            except ExtractionError as e:
                messages = compress_for_retry(messages, resp, f"{e}\n```{tag} のコードブロック1つで出力してください。")
                continue
            try:
                write_rel(ctx, impl_rel, stub)
                syntax = run_gate(ctx, "syntax", quiet=True)
                full = run_gate(ctx, "full", quiet=True)
            finally:
                make_writable(impl_path)
                if original is None:
                    impl_path.unlink(missing_ok=True)
                else:
                    impl_path.write_bytes(original)
            if syntax.passed and not full.passed and full.rank[0] == 1:
                messages = compress_for_retry(
                    messages, resp, "スタブがビルドできませんでした:\n```\n" + full.digest +
                    f"\n```\n修正したスタブを ```{tag} のコードブロック1つで出力してください。")
                continue
            outcome = (syntax, full)
            break
        if outcome is None:
            ctx.events.warn("スタブがビルドできなかったため red_check を省略します")
            ctx.state["red_check"] = {"status": "skipped", "reason": "stub_build_failed"}
            return StepResult.skipped("スタブ生成に失敗")
        syntax, full = outcome
        result = {"syntax_passed": syntax.passed, "full_passed": full.passed, "failures": full.rank[1],
                  "digest": full.digest[-800:]}
        if not syntax.passed:
            ctx.state["red_check"] = {**result, "status": "syntax_failed"}
            return StepResult.failed("red_check: スタブで構文チェックが通りません", error="red_check_failed")
        if full.passed:
            attempts = int(ctx.state.get("red_check_retries", 0))
            ctx.state["red_check"] = {**result, "status": "green_with_stub"}
            limit = int(ctx.opt("red_check_max_retries", ctx.settings.pipeline.red_check_max_retries))
            if attempts < limit:
                ctx.state["red_check_retries"] = attempts + 1
                ctx.state["test_gen_feedback"] = (
                    "何もしないスタブ実装でもテストが全件通りました。テストが期待値を検査していません。"
                    "受入基準の具体的な入力と期待値を CHECK 系マクロ（またはアサーション）で必ず検査してください。")
                return StepResult.jump("test_gen", "スタブでもテストが通過（テストが何も検査していない）")
            ctx.events.warn("red_check: 再生成後もスタブで通過しました。監査で確認してください")
            return StepResult.done("警告付きで続行")
        ctx.state["red_check"] = {**result, "status": "red"}
        return StepResult.done(f"スタブで失敗することを確認（失敗 {full.rank[1]} 件）")


class LockTests(Step):
    name = "lock_tests"

    def run(self, ctx: JobContext) -> StepResult:
        rels = [*paths(ctx)["tests"], *ctx.state.get("support_copies", [])]
        hashes: dict[str, str] = {}
        for rel in rels:
            p = ctx.wpath(rel)
            if not p.exists():
                return StepResult.failed(f"テストファイルがありません: {rel}")
            chmod_readonly(p)
            hashes[rel] = sha256_file(p)
        ctx.state["test_hashes"] = hashes
        return StepResult.done(f"{len(hashes)} ファイルをロック")


def verify_hashes(ctx: JobContext) -> list[str]:
    bad = []
    for rel, digest in (ctx.state.get("test_hashes") or {}).items():
        p = ctx.wpath(rel)
        if not p.exists() or sha256_file(p) != digest:
            bad.append(rel)
    return bad

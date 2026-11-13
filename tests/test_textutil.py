from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from autospec.core.textutil import (
    ExtractionError,
    count_failures,
    extract_code_block,
    extract_error_digest,
    failed_test_names,
    md_cell,
    strip_thinking,
)

C_TAGS = ["c", "h"]
CPP_TAGS = ["cpp", "c++", "cxx", "cc", "hpp", "h"]
MAIN = [r"\bmain\s*\("]
TEST_BODY = '#include "x.h"\nint main(void) { return 0; }'


# --- §6.7: the five legacy failure cases (B1) --------------------------------------------------

def test_bash_build_example_after_test_is_not_picked() -> None:
    text = f"テストです。\n```c\n{TEST_BODY}\n```\nビルド例:\n```bash\ngcc -o t x.c test.c\n```\n"
    assert extract_code_block(text, C_TAGS, MAIN) == TEST_BODY + "\n"


def test_cpp_fence_tag_with_plus_signs() -> None:
    text = f"```c++\n{TEST_BODY}\n```"
    out = extract_code_block(text, CPP_TAGS, MAIN)
    assert "```" not in out and out.startswith("#include")


def test_fence_tag_followed_by_spaces_and_crlf() -> None:
    text = f"```c   \r\n{TEST_BODY}\r\n```\r\n"
    out = extract_code_block(text, C_TAGS, MAIN)
    assert "```" not in out and "main" in out


def test_no_code_block_raises() -> None:
    with pytest.raises(ExtractionError):
        extract_code_block("テストは main 関数で書きます。int main(void) { return 0; }", C_TAGS, MAIN)


def test_unclosed_think_raises() -> None:
    text = f"<think>まず考える\n```c\n{TEST_BODY}\n```\n"
    with pytest.raises(ExtractionError, match="think"):
        extract_code_block(text, C_TAGS, MAIN)


# --- additional extraction rules ------------------------------------------------------------------

def test_closed_think_is_removed_and_longest_tagged_block_wins() -> None:
    text = ("<think>```c\nint main(void){}\n```</think>\n```c\nint helper;\n```\n"
            f"```c\n{TEST_BODY}\n/* longer */\n```")
    out = extract_code_block(text, C_TAGS, MAIN)
    assert "longer" in out


def test_untagged_block_is_fallback() -> None:
    assert "main" in extract_code_block(f"```\n{TEST_BODY}\n```", C_TAGS, MAIN)


def test_sanity_pattern_mismatch_raises() -> None:
    with pytest.raises(ExtractionError, match="パターン"):
        extract_code_block("```c\nint x;\n```", C_TAGS, MAIN)


def test_strip_thinking() -> None:
    assert strip_thinking("<think>a</think>答え") == "答え"


# --- §6.8: error digest (B3) ------------------------------------------------------------------------

def test_timeout_marker_in_digest() -> None:
    d = extract_error_digest("running...\n", ["error:"], 2500, timed_out=True, timeout_sec=5)
    assert d.startswith("[TIMEOUT] 5秒で打ち切りました")


def test_first_error_kept_and_tail_kept_with_omission_marker() -> None:
    lines = ["x.c:3:5: error: FIRST ROOT CAUSE"] + [f"noise line {i} " + "y" * 60 for i in range(300)]
    lines += ["x.c:200:1: error: cascade"] * 50 + ["PASSED: 0 FAILED: 3"]
    d = extract_error_digest("\n".join(lines), ["error:"], 1200)
    assert "FIRST ROOT CAUSE" in d
    assert "PASSED: 0 FAILED: 3" in d
    assert "行省略" in d
    assert len(d) <= 1300


@pytest.mark.skipif(not shutil.which("cc"), reason="C compiler required")
def test_real_compiler_first_error_is_in_digest(tmp_path: Path) -> None:
    src = tmp_path / "bad.c"
    body = ["int first_bug(void) { return undefined_symbol_alpha; }"]
    body += [f"int f{i}(void) {{ return missing_{i} + ; }}" for i in range(60)]
    src.write_text("\n".join(body) + "\n")
    proc = subprocess.run(["cc", "-std=c11", "-fsyntax-only", "-ferror-limit=0", str(src)],
                          capture_output=True, text=True)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0 and len(out) > 2000
    d = extract_error_digest(out, ["error:"], 800)
    assert "undefined_symbol_alpha" in d


def test_count_failures_ranks_compile_errors_worse_than_failing_tests() -> None:
    ran = count_failures("FAIL t x.c:1 a\nPASSED: 2 FAILED: 1\n", 1, r"FAILED: (\d+)", ["error:"])
    compile_err = count_failures("x.c:1: error: boom\n", 1, r"FAILED: (\d+)", ["error:"])
    assert ran == (0, 1)
    assert compile_err[0] == 1 and compile_err > ran
    assert count_failures("", 0, None, []) == (0, 0)


def test_failed_test_names() -> None:
    out = "FAIL test_REQ001_a x.c:3 a == b\nFAIL test_REQ001_a x.c:4 c\nFAIL test_REQ002_b x.c:9 d\n"
    assert failed_test_names(out, r"^FAIL (\S+) ") == ["test_REQ001_a", "test_REQ002_b"]


def test_md_cell_escapes_pipe_and_newline() -> None:
    assert md_cell("a|b\nc") == "a\\|b<br>c"

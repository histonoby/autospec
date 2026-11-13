"""Text helpers: thinking removal, code block extraction (§6.7), error digest (§6.8), token estimate."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

_THINK_CLOSED = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_THINK_OPEN = re.compile(r"<think>", re.IGNORECASE)
_FENCE_OPEN = re.compile(r"^[ \t]*```[ \t]*([\w+#.-]*)[^\n]*$")
_FENCE_CLOSE = re.compile(r"^[ \t]*```[ \t]*$")


class ExtractionError(ValueError):
    """The LLM response did not contain a usable code block."""


def strip_thinking(text: str) -> str:
    """Remove closed <think>...</think> blocks. Unclosed blocks are kept (see has_unclosed_think)."""
    return _THINK_CLOSED.sub("", text).strip()


def has_unclosed_think(text: str) -> bool:
    return bool(_THINK_OPEN.search(_THINK_CLOSED.sub("", text)))


@dataclass
class CodeBlock:
    tag: str
    body: str


def parse_code_blocks(text: str) -> list[CodeBlock]:
    """Line based fence parser. Handles `c++`, trailing spaces after the tag and CRLF."""
    blocks: list[CodeBlock] = []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    tag: str | None = None
    buf: list[str] = []
    for line in lines:
        if tag is None:
            m = _FENCE_OPEN.match(line)
            if m:
                tag = m.group(1).lower()
                buf = []
        elif _FENCE_CLOSE.match(line):
            blocks.append(CodeBlock(tag=tag, body="\n".join(buf)))
            tag = None
        else:
            buf.append(line)
    return blocks


def extract_code_block(
    text: str,
    tags: Sequence[str],
    sanity_patterns: Iterable[str] = (),
) -> str:
    """Pick the code block according to §6.7 and return its body (with trailing newline)."""
    if has_unclosed_think(text):
        raise ExtractionError("応答が <think> の途中で切れています（閉じタグがありません）")
    text = strip_thinking(text)
    blocks = parse_code_blocks(text)
    wanted = {t.lower() for t in tags}
    tagged = [b for b in blocks if b.tag in wanted]
    untagged = [b for b in blocks if b.tag == ""]
    chosen: CodeBlock | None = None
    if tagged:
        chosen = max(tagged, key=lambda b: len(b.body))
    elif untagged:
        chosen = max(untagged, key=lambda b: len(b.body))
    if chosen is None or not chosen.body.strip():
        found = ", ".join(sorted({b.tag or "(タグ無し)" for b in blocks})) or "なし"
        raise ExtractionError(f"対象のコードブロックが見つかりません（見つかったタグ: {found}）")
    for pat in sanity_patterns:
        if not re.search(pat, chosen.body, re.MULTILINE):
            raise ExtractionError(f"コードが必要なパターン `{pat}` を満たしていません")
    return chosen.body.rstrip() + "\n"


def estimate_tokens(text: str, chars_per_token: float = 3) -> int:
    return int(len(text) / chars_per_token) + 1


def extract_error_digest(
    output: str,
    patterns: Sequence[str],
    limit: int = 2500,
    *,
    timed_out: bool = False,
    timeout_sec: int | float | None = None,
    context: int = 2,
) -> str:
    """Collect error lines (with context) in order, always keep the first one, fill the rest with the tail."""
    header = f"[TIMEOUT] {timeout_sec}秒で打ち切りました\n" if timed_out else ""
    lines = output.replace("\r\n", "\n").split("\n")
    while lines and not lines[-1].strip():
        lines.pop()
    budget = max(limit - len(header), 200)
    if sum(len(x) + 1 for x in lines) <= budget:
        return header + "\n".join(lines)

    regs = [re.compile(p) for p in patterns]
    hits = [i for i, ln in enumerate(lines) if any(r.search(ln) for r in regs)]
    selected: set[int] = set()
    used = 0

    def take(indices: Iterable[int]) -> bool:
        nonlocal used
        new = [i for i in indices if i not in selected]
        cost = sum(len(lines[i]) + 1 for i in new)
        if used + cost > budget:
            return False
        selected.update(new)
        used += cost
        return True

    for n, h in enumerate(hits):
        window = range(max(0, h - context), min(len(lines), h + context + 1))
        if not take(window) and n == 0:
            # The first error must always be present, even when its context does not fit.
            first = lines[h][: budget - 40]
            lines[h] = first
            take([h])
        if used >= budget * 0.7:
            break
    for i in range(len(lines) - 1, -1, -1):
        if not take([i]):
            break

    out: list[str] = []
    prev = -1
    for i in sorted(selected):
        if i > prev + 1:
            out.append(f"...({i - prev - 1}行省略)...")
        out.append(lines[i])
        prev = i
    if prev < len(lines) - 1:
        out.append(f"...({len(lines) - 1 - prev}行省略)...")
    return header + "\n".join(out)


def count_failures(output: str, returncode: int, failure_count_pattern: str | None,
                   error_patterns: Sequence[str]) -> tuple[int, int]:
    """Return a sortable rank (ran_flag, failures). ran_flag=0 when the test summary was found."""
    if returncode == 0:
        return (0, 0)
    if failure_count_pattern:
        found = re.findall(failure_count_pattern, output)
        if found:
            last = found[-1]
            n = int(last[0] if isinstance(last, tuple) else last)
            return (0, max(n, 1))
    regs = [re.compile(p) for p in error_patterns]
    n = sum(1 for ln in output.splitlines() if any(r.search(ln) for r in regs))
    return (1, max(n, 1))


def failed_test_names(output: str, pattern: str | None) -> list[str]:
    if not pattern:
        return []
    names: list[str] = []
    for m in re.finditer(pattern, output, re.MULTILINE):
        name = m.group(1)
        if name not in names:
            names.append(name)
    return names


def md_cell(value: object) -> str:
    """Escape a value for a Markdown table cell (pipes and newlines)."""
    s = str(value)
    return s.replace("\\", "\\\\").replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")

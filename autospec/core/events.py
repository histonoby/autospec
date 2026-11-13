"""Event model and emitters (§6.5). Sinks: console, JSONL file, memory; the DB sink arrives in M2."""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class Event:
    job_id: str
    type: str
    message: str = ""
    level: str = "info"
    step: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    ts: str = field(default_factory=now_iso)
    id: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


Sink = Callable[[Event], None]


class EventEmitter:
    def __init__(self, job_id: str, sinks: list[Sink] | None = None) -> None:
        self.job_id = job_id
        self.sinks: list[Sink] = list(sinks or [])
        self.current_step: str | None = None
        self._lock = threading.Lock()
        self._seq = 0

    def add_sink(self, sink: Sink) -> None:
        self.sinks.append(sink)

    def emit(self, type: str, message: str = "", *, level: str = "info",
             data: dict[str, Any] | None = None, step: str | None = None) -> Event:
        with self._lock:
            self._seq += 1
            ev = Event(job_id=self.job_id, type=type, message=message, level=level,
                       step=step if step is not None else self.current_step, data=data or {}, id=self._seq)
            for sink in self.sinks:
                sink(ev)
        return ev

    def log(self, message: str, level: str = "info", **data: Any) -> Event:
        return self.emit("log", message, level=level, data=data)

    def warn(self, message: str, **data: Any) -> Event:
        return self.emit("log", message, level="warn", data=data)


class MemorySink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, ev: Event) -> None:
        self.events.append(ev)

    def of_type(self, type: str) -> list[Event]:
        return [e for e in self.events if e.type == type]


class JsonlFileSink:
    """Append every event to DATA_DIR/jobs/<id>/events.log (§18 L4)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def __call__(self, ev: Event) -> None:
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(ev.to_dict(), ensure_ascii=False) + "\n")


_ICONS = {
    "job.status": "🧭", "step.start": "▶️ ", "step.end": "⏹ ", "llm.request": "🧠", "llm.response": "💬",
    "gate.result": "🧪", "aider.output": "  │", "approval.required": "✋", "approval.resolved": "👍",
    "artifact.created": "📄",
}


class ConsoleSink:
    def __init__(self, stream: TextIO | None = None, show_aider: bool = True, verbose: bool = False) -> None:
        self.stream = stream or sys.stdout
        self.show_aider = show_aider
        self.verbose = verbose

    def __call__(self, ev: Event) -> None:
        if ev.type == "aider.output" and not self.show_aider:
            return
        if ev.type in ("llm.request", "llm.response", "artifact.created") and not self.verbose:
            return
        icon = _ICONS.get(ev.type, "•")
        if ev.level == "error":
            icon = "❌"
        elif ev.level == "warn":
            icon = "⚠️ "
        elif ev.type == "step.end" and ev.data.get("status") == "done":
            icon = "✅"
        elif ev.type == "gate.result":
            icon = "✅" if ev.data.get("returncode") == 0 else "🧪"
        step = f"[{ev.step}] " if ev.step and ev.type != "aider.output" else ""
        print(f"{icon} {step}{ev.message}", file=self.stream, flush=True)

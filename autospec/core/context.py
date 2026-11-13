"""JobContext, ArtifactStore and CancelToken (§6.2, §6.3)."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..config import Settings
    from .events import EventEmitter
    from .llm import LLMClient
    from .profiles import Profile
    from .sandbox import Sandbox
    from .workspace import Workspace


class JobCancelled(Exception):
    pass


class CancelToken:
    def __init__(self) -> None:
        self._event = threading.Event()
        self._procs: set[subprocess.Popen[str]] = set()
        self._lock = threading.Lock()

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        self._event.set()
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass

    def check(self) -> None:
        if self.is_set():
            raise JobCancelled("job cancelled")

    def register(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._procs.add(proc)

    def unregister(self, proc: subprocess.Popen[str]) -> None:
        with self._lock:
            self._procs.discard(proc)


class ArtifactStore:
    def __init__(self, root: Path, events: EventEmitter | None = None) -> None:
        self.root = root
        self.events = events
        self._llm_seq = 0
        self._lock = threading.Lock()

    def path(self, name: str) -> Path:
        p = (self.root / name).resolve()
        if self.root.resolve() not in p.parents and p != self.root.resolve():
            raise ValueError(f"artifact path escapes the store: {name}")
        return p

    def write(self, name: str, content: str | bytes | dict | list, *, announce: bool = True) -> Path:
        p = self.path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, (dict, list)):
            p.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
        elif isinstance(content, bytes):
            p.write_bytes(content)
        else:
            p.write_text(content, encoding="utf-8")
        if announce and self.events:
            self.events.emit("artifact.created", name, data={"name": name, "bytes": p.stat().st_size})
        return p

    def read_text(self, name: str) -> str:
        return self.path(name).read_text(encoding="utf-8")

    def exists(self, name: str) -> bool:
        return self.path(name).exists()

    def next_llm_log(self) -> str:
        with self._lock:
            if self._llm_seq == 0 and (self.root / "llm").is_dir():
                self._llm_seq = len(list((self.root / "llm").glob("*.json")))
            self._llm_seq += 1
            return f"llm/{self._llm_seq:04d}.json"

    def list(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*") if p.is_file())


@dataclass
class JobContext:
    job_id: str
    workspace: Workspace
    workdir: Path
    profile: Profile | None
    inputs: dict[str, Any]
    options: dict[str, Any]
    state: dict[str, Any]
    llm: LLMClient
    events: EventEmitter
    sandbox: Sandbox
    artifacts: ArtifactStore
    cancel: CancelToken
    settings: Settings
    job_dir: Path
    interactive: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def opt(self, key: str, default: Any) -> Any:
        v = self.options.get(key)
        return default if v is None else v

    def wpath(self, rel: str) -> Path:
        """Absolute path of a workdir-relative path, refusing anything outside the workdir."""
        p = (self.workdir / rel).resolve()
        root = self.workdir.resolve()
        if p != root and root not in p.parents:
            raise ValueError(f"path escapes the workdir: {rel}")
        return p

    @property
    def build_dir(self) -> Path:
        d = self.job_dir / "build"
        d.mkdir(parents=True, exist_ok=True)
        return d

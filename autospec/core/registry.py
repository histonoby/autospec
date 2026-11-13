"""Name based registration of steps, pipelines, profiles and targets (§6.9), plus resource locks."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import yaml

from .profiles import Profile, load_profiles
from .step import Step

StepSpec = tuple[str, dict[str, Any]]


class Registry:
    def __init__(self) -> None:
        self.steps: dict[str, Callable[..., Step]] = {}
        self.pipelines: dict[str, list[StepSpec]] = {}
        self.profiles: dict[str, Profile] = {}
        self.targets: dict[str, Any] = {}  # filled by part 2 (hw/target.py)

    def register_step(self, cls: Callable[..., Step]) -> Callable[..., Step]:
        name = getattr(cls, "name", "")
        if not name:
            raise ValueError(f"{cls!r} has no step name")
        self.steps[name] = cls
        return cls

    def register_pipeline(self, name: str, steps: Iterable[str | StepSpec]) -> None:
        specs: list[StepSpec] = []
        for s in steps:
            specs.append((s, {}) if isinstance(s, str) else (s[0], dict(s[1])))
        self.pipelines[name] = specs

    def load_pipeline_yaml(self, path: Path) -> None:
        """YAML: {name: str, steps: [step_name | {step: name, params: {...}}]}."""
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        steps: list[str | StepSpec] = []
        for item in data.get("steps", []):
            if isinstance(item, str):
                steps.append(item)
            else:
                steps.append((item["step"], item.get("params", {}) or {}))
        self.register_pipeline(data.get("name") or path.stem, steps)

    def load_pipeline_dir(self, directory: Path) -> None:
        if directory.is_dir():
            for f in sorted(directory.glob("*.yaml")):
                self.load_pipeline_yaml(f)

    def load_profiles(self, directory: Path) -> None:
        self.profiles = load_profiles(directory)

    def build(self, pipeline: str, extra: Iterable[StepSpec] = ()) -> list[Step]:
        if pipeline not in self.pipelines:
            raise KeyError(f"unknown pipeline: {pipeline}")
        steps: list[Step] = []
        for name, params in [*self.pipelines[pipeline], *extra]:
            if name not in self.steps:
                raise KeyError(f"pipeline {pipeline}: unknown step {name}")
            steps.append(self.steps[name](**params))
        return steps


class LockTimeout(TimeoutError):
    pass


class ResourceLocks:
    """acquire(resource_ids, timeout) / release(). Resources are taken in name order to avoid deadlock.

    M1 uses a single "llm" resource; part 2 adds device resources (uart:/dev/ttyUSB0 ...).
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._held: dict[str, str] = {}
        self.on_change: Callable[[str, str | None], None] | None = None

    def acquire(self, resource_ids: Iterable[str], timeout: float, holder: str) -> None:
        ids = sorted(set(resource_ids))
        deadline = time.monotonic() + timeout
        with self._cond:
            while any(self._held.get(r) not in (None, holder) for r in ids):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise LockTimeout(f"resources busy: {ids}")
                self._cond.wait(remaining)
            for r in ids:
                self._held[r] = holder
                if self.on_change:
                    self.on_change(r, holder)

    def release(self, holder: str, resource_ids: Iterable[str] | None = None) -> None:
        with self._cond:
            for r in list(resource_ids or self._held):
                if self._held.get(r) == holder:
                    del self._held[r]
                    if self.on_change:
                        self.on_change(r, None)
            self._cond.notify_all()

    def holder_of(self, resource: str) -> str | None:
        with self._cond:
            return self._held.get(resource)


LOCKS = ResourceLocks()
REGISTRY = Registry()

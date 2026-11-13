"""Step base class and StepResult (§6.2)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from .context import JobContext


class StepStatus(StrEnum):
    DONE = "done"
    FAILED = "failed"
    WAITING_APPROVAL = "waiting_approval"
    SKIPPED = "skipped"


@dataclass
class StepResult:
    status: StepStatus
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    jump_to: str | None = None
    error: str | None = None  # machine readable job error code (e.g. infra_error)

    @classmethod
    def done(cls, message: str = "", **data: Any) -> StepResult:
        return cls(StepStatus.DONE, message, data)

    @classmethod
    def failed(cls, message: str, error: str | None = None, **data: Any) -> StepResult:
        return cls(StepStatus.FAILED, message, data, error=error)

    @classmethod
    def skipped(cls, message: str = "") -> StepResult:
        return cls(StepStatus.SKIPPED, message)

    @classmethod
    def jump(cls, target: str, message: str = "", **data: Any) -> StepResult:
        return cls(StepStatus.DONE, message, data, jump_to=target)


class Step(ABC):
    name: ClassVar[str] = ""
    always_run: ClassVar[bool] = False

    def __init__(self, **params: Any) -> None:
        self.params = params

    @abstractmethod
    def run(self, ctx: JobContext) -> StepResult: ...

    def should_skip(self, ctx: JobContext) -> bool:
        return False

    @property
    def label(self) -> str:
        target = self.params.get("target")
        return f"{self.name}:{target}" if target else self.name

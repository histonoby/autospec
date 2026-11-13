"""Pipeline definitions (§8). Pipelines are lists of registered step names (+params).

Extra pipelines can be added as YAML files in this directory: {name, steps: [name | {step, params}]}.
"""

from __future__ import annotations

from pathlib import Path

from ..config import Settings
from ..core.registry import Registry
from ..steps.approval import Approval
from ..steps.audit import Audit
from ..steps.implement import Implement
from ..steps.integrity import IntegrityCheck
from ..steps.prepare import Finalize, PrepareWorkspace
from ..steps.requirements import RequirementsExtract
from ..steps.testgen import CoverageCheck, LockTests, RedCheck, TestGen

STEP_CLASSES = [PrepareWorkspace, RequirementsExtract, Approval, TestGen, CoverageCheck, RedCheck, LockTests,
                Implement, IntegrityCheck, Audit, Finalize]

PIPELINES: dict[str, list[str]] = {
    "implement": ["prepare_workspace", "requirements_extract", "test_gen", "coverage_check", "red_check",
                  "lock_tests", "implement", "integrity_check", "audit", "finalize"],
    # design / analyze / modify / hw_verify are registered in M4-M8 when their steps exist.
}


def build_registry(settings: Settings) -> Registry:
    reg = Registry()
    for cls in STEP_CLASSES:
        reg.register_step(cls)
    for name, steps in PIPELINES.items():
        reg.register_pipeline(name, steps)
    reg.load_pipeline_dir(Path(__file__).parent)
    reg.load_profiles(settings.profiles_dir)
    return reg

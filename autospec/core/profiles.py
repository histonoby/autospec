"""Language profiles (§7): loading, validation, template expansion and workspace overrides."""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

_PLACEHOLDER = re.compile(r"\{(\w+)\}")


class Profile(BaseModel):
    name: str
    display_name: str
    extensions: list[str]
    sandbox_image: str = "ubuntu:24.04"
    test_instructions: str
    scaffold: str
    stub_instructions: str
    interface_instructions: str = ""
    gates: dict[str, str]
    red_check: bool = True
    signature_patterns: list[str] = Field(default_factory=list)
    code_fence_tags: list[str]
    sanity_patterns: list[str] = Field(default_factory=list)
    error_patterns: list[str]
    failure_count_pattern: str | None = None
    failed_test_pattern: str | None = None
    test_name_pattern: str = r"test_{req_id_compact}_\w+"
    support_files: list[str] = Field(default_factory=list)
    timeouts: dict[str, int] = Field(default_factory=dict)
    post_gates: list[str] = Field(default_factory=list)
    infra_error_patterns: list[str] = Field(default_factory=list)
    ignore: list[str] = Field(default_factory=list)

    @field_validator("gates")
    @classmethod
    def _required_gates(cls, v: dict[str, str]) -> dict[str, str]:
        missing = [g for g in ("syntax", "full") if not v.get(g)]
        if missing:
            raise ValueError(f"gates.{'/'.join(missing)} is required")
        return v

    @field_validator("error_patterns", "sanity_patterns", "signature_patterns")
    @classmethod
    def _valid_regex(cls, v: list[str]) -> list[str]:
        for p in v:
            re.compile(p)
        return v

    def timeout_for(self, gate: str, default: int) -> int:
        return int(self.timeouts.get(gate, default))

    def test_name_regex(self, req_id: str) -> str:
        compact = re.sub(r"[^A-Za-z0-9]", "", req_id)
        return self.test_name_pattern.replace("{req_id_compact}", compact)

    def render(self, template: str, **values: Any) -> str:
        return render_text(template, values)

    def render_gate(self, gate: str, values: Mapping[str, Any]) -> str:
        cmd = self.gates.get(gate)
        if not cmd:
            raise KeyError(f"profile {self.name} has no gate '{gate}'")
        return render_command(cmd, values)


def render_text(template: str, values: Mapping[str, Any]) -> str:
    """Expand only known {placeholders}; other braces (C code, JSON) are left untouched."""
    return _PLACEHOLDER.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


def _quote(value: Any) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(shlex.quote(str(v)) for v in value)
    return shlex.quote(str(value))


def render_command(template: str, values: Mapping[str, Any]) -> str:
    """Expand placeholders in a shell command, shell-quoting every substituted value (§6.6, L1)."""
    return _PLACEHOLDER.sub(lambda m: _quote(values[m.group(1)]) if m.group(1) in values else m.group(0), template)


class ProfileError(ValueError):
    pass


def load_profile_file(path: Path) -> Profile:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    try:
        return Profile.model_validate(data)
    except Exception as e:  # pydantic ValidationError or regex error
        raise ProfileError(f"{path.name}: {e}") from e


def load_profiles(directory: Path) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    errors: list[str] = []
    for f in sorted(directory.glob("*.yaml")):
        try:
            p = load_profile_file(f)
        except ProfileError as e:
            errors.append(str(e))
            continue
        profiles[p.name] = p
    if errors:
        raise ProfileError("invalid profiles:\n" + "\n".join(errors))
    return profiles


def load_workspace_overrides(workdir: Path) -> dict[str, Any]:
    f = workdir / ".autospec" / "workspace.yaml"
    if not f.is_file():
        return {}
    data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ProfileError(f"{f}: must be a mapping")
    return data


def resolve_profile(profiles: Mapping[str, Profile], name: str | None, workdir: Path | None = None,
                    paths: Sequence[str] = ()) -> Profile:
    """Pick the profile (explicit > workspace.yaml > extension guess) and apply workspace overrides."""
    overrides = load_workspace_overrides(workdir) if workdir else {}
    chosen = name or overrides.get("profile") or guess_profile(profiles, paths)
    if not chosen or chosen not in profiles:
        raise ProfileError(f"unknown profile: {chosen!r} (available: {', '.join(sorted(profiles))})")
    prof = profiles[chosen].model_copy(deep=True)
    if isinstance(overrides.get("gates"), dict):
        prof.gates.update({k: str(v) for k, v in overrides["gates"].items()})
    if isinstance(overrides.get("ignore"), list):
        prof.ignore = [str(x) for x in overrides["ignore"]]
    if isinstance(overrides.get("timeouts"), dict):
        prof.timeouts.update({k: int(v) for k, v in overrides["timeouts"].items()})
    return prof


def guess_profile(profiles: Mapping[str, Profile], paths: Sequence[str]) -> str | None:
    for path in paths:
        suffix = Path(path).suffix
        for p in profiles.values():
            if suffix in p.extensions:
                return p.name
    return None

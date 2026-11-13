"""Settings: config.yaml + environment overrides (AUTOSPEC_<SECTION>_<KEY>)."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent


class OllamaCfg(BaseModel):
    url: str = "http://127.0.0.1:11434"
    model: str = "local-coder-64k"
    num_ctx: int = 65536
    temperature: float = 0.2
    timeout_sec: int = 600
    # Some models reject the `think` field (e.g. non-thinking coders). The client also
    # falls back automatically when Ollama answers "does not support thinking".
    send_think: bool = True
    no_think_suffix: bool = True
    connect_retries: int = 3
    backoff_base_sec: float = 1.0


class PathsCfg(BaseModel):
    data_dir: str = "~/autospec/data"
    workspaces_root: str = "~/autospec/workspaces"
    harness_dir: str = "./harness"
    profiles_dir: str = "./profiles"
    test_support_dir: str = "./test_support"
    targets_dir: str = "./targets"


class ServerCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class AuthCfg(BaseModel):
    token: str = ""


class SandboxCfg(BaseModel):
    mode: str = "local"
    gate_timeout_sec: int = 300


class AiderCfg(BaseModel):
    bin: str = "aider"
    max_rounds: int = 5
    timeout_sec: int = 1800
    suspect_after_rounds: int = 3
    extra_args: list[str] = Field(default_factory=list)


class PipelineCfg(BaseModel):
    test_gen_max_retries: int = 4
    approval_max_rejects: int = 3
    analyze_max_relevant_files: int = 8
    max_file_bytes: int = 200_000
    output_reserve_tokens: int = 8192
    chars_per_token: float = 3
    retry_temperature_step: float = 0.15
    retry_temperature_max: float = 0.7
    error_digest_chars: int = 2500
    max_jumps: int = 10
    coverage_max_retries: int = 2
    red_check_max_retries: int = 2


class HwCfg(BaseModel):
    targets_dir: str = "./targets"
    default_mode: str = "mock"
    llm_tool_mode: str = "native"
    max_llm_tool_calls: int = 10
    max_hw_rounds: int = 3
    lock_wait_timeout_sec: int = 1800
    agents: dict[str, Any] = Field(default_factory=dict)


class Settings(BaseModel):
    ollama: OllamaCfg = Field(default_factory=OllamaCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    server: ServerCfg = Field(default_factory=ServerCfg)
    auth: AuthCfg = Field(default_factory=AuthCfg)
    sandbox: SandboxCfg = Field(default_factory=SandboxCfg)
    aider: AiderCfg = Field(default_factory=AiderCfg)
    pipeline: PipelineCfg = Field(default_factory=PipelineCfg)
    hw: HwCfg = Field(default_factory=HwCfg)
    base_dir: str = str(REPO_ROOT)

    def _resolve(self, value: str) -> Path:
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = Path(self.base_dir) / p
        return p.resolve()

    @property
    def data_dir(self) -> Path:
        return self._resolve(self.paths.data_dir)

    @property
    def workspaces_root(self) -> Path:
        return self._resolve(self.paths.workspaces_root)

    @property
    def harness_dir(self) -> Path:
        return self._resolve(self.paths.harness_dir)

    @property
    def profiles_dir(self) -> Path:
        return self._resolve(self.paths.profiles_dir)

    @property
    def test_support_dir(self) -> Path:
        return self._resolve(self.paths.test_support_dir)

    @property
    def targets_dir(self) -> Path:
        return self._resolve(self.paths.targets_dir)

    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"


def _coerce(raw: str, current: Any) -> Any:
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    if isinstance(current, (list, dict)):
        return json.loads(raw)
    return raw


def _apply_env(data: dict[str, Any], env: Mapping[str, str]) -> None:
    defaults = Settings().model_dump()
    for section, fields in defaults.items():
        if not isinstance(fields, dict):
            continue
        for key, default in fields.items():
            name = f"AUTOSPEC_{section.upper()}_{key.upper()}"
            if name in env:
                data.setdefault(section, {})[key] = _coerce(env[name], default)
    if "AIDER_BIN" in env:
        data.setdefault("aider", {})["bin"] = env["AIDER_BIN"]


def load_settings(path: str | Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    data: dict[str, Any] = {}
    base_dir = REPO_ROOT
    cfg_path = Path(path) if path else (Path(env["AUTOSPEC_CONFIG"]) if env.get("AUTOSPEC_CONFIG") else None)
    if cfg_path is None and (Path.cwd() / "config.yaml").is_file():
        cfg_path = Path.cwd() / "config.yaml"
    if cfg_path is not None:
        cfg_path = cfg_path.expanduser().resolve()
        loaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
        if not isinstance(loaded, dict):
            raise ValueError(f"config must be a mapping: {cfg_path}")
        data.update(loaded)
        base_dir = cfg_path.parent
    _apply_env(data, env)
    data["base_dir"] = str(base_dir)
    return Settings.model_validate(data)

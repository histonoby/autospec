"""Command execution with mandatory timeouts and process-group kill (§6.6)."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .context import CancelToken

TIMEOUT_RC = 124


@dataclass
class CompletedRun:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False
    cancelled: bool = False
    duration_sec: float = 0.0

    @property
    def output(self) -> str:
        if self.stderr and self.stdout:
            return self.stdout.rstrip("\n") + "\n" + self.stderr
        return self.stdout or self.stderr

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out and not self.cancelled


Cmd = str | Sequence[str]


class Sandbox(Protocol):
    def run(self, cmd: Cmd, *, cwd: Path, timeout: int,
            on_line: Callable[[str], None] | None = None,
            env: Mapping[str, str] | None = None) -> CompletedRun: ...


def _kill_group(proc: subprocess.Popen[str]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=3)
            return
        except subprocess.TimeoutExpired:
            continue


class LocalSandbox:
    def __init__(self, cancel: CancelToken | None = None) -> None:
        self.cancel = cancel

    def _argv(self, cmd: Cmd) -> list[str]:
        if isinstance(cmd, str):
            return ["bash", "-c", cmd]
        return list(cmd)

    def run(self, cmd: Cmd, *, cwd: Path, timeout: int,
            on_line: Callable[[str], None] | None = None,
            env: Mapping[str, str] | None = None) -> CompletedRun:
        if not timeout or timeout <= 0:
            raise ValueError("timeout is mandatory for every command")
        full_env = dict(os.environ)
        if env:
            full_env.update(env)
        start = time.monotonic()
        proc = subprocess.Popen(
            self._argv(cmd), cwd=str(cwd), env=full_env, text=True, errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        out: list[str] = []
        err: list[str] = []

        def pump(stream, buf: list[str]) -> None:  # type: ignore[no-untyped-def]
            for line in stream:
                buf.append(line)
                if on_line:
                    try:
                        on_line(line.rstrip("\n"))
                    except Exception:  # noqa: BLE001 - a sink must never kill the pump
                        pass
            stream.close()

        threads = [threading.Thread(target=pump, args=(proc.stdout, out), daemon=True),
                   threading.Thread(target=pump, args=(proc.stderr, err), daemon=True)]
        for t in threads:
            t.start()
        if self.cancel:
            self.cancel.register(proc)
        timed_out = cancelled = False
        try:
            while True:
                try:
                    proc.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    pass
                if self.cancel and self.cancel.is_set():
                    cancelled = True
                    _kill_group(proc)
                    break
                if time.monotonic() - start > timeout:
                    timed_out = True
                    _kill_group(proc)
                    break
        finally:
            if self.cancel:
                self.cancel.unregister(proc)
        if self.cancel and self.cancel.is_set() and not timed_out:
            cancelled = True  # CancelToken.set() may have killed the group before the poll loop noticed
        for t in threads:
            t.join(timeout=5)
        rc = proc.returncode if proc.returncode is not None else -1
        if timed_out:
            rc = TIMEOUT_RC
        return CompletedRun(returncode=rc, stdout="".join(out), stderr="".join(err), timed_out=timed_out,
                            cancelled=cancelled, duration_sec=time.monotonic() - start)


class DockerSandbox(LocalSandbox):
    """docker run --rm --network none -v <workdir>:/work -w /work <image> bash -lc <cmd>."""

    def __init__(self, image: str, cancel: CancelToken | None = None, extra_mounts: Sequence[Path] = ()) -> None:
        super().__init__(cancel)
        self.image = image
        self.extra_mounts = list(extra_mounts)
        self._cwd: Path | None = None

    def run(self, cmd: Cmd, *, cwd: Path, timeout: int,
            on_line: Callable[[str], None] | None = None,
            env: Mapping[str, str] | None = None) -> CompletedRun:
        inner = cmd if isinstance(cmd, str) else shlex.join(cmd)
        argv = ["docker", "run", "--rm", "--network", "none", "-v", f"{cwd}:{cwd}", "-w", str(cwd)]
        for m in self.extra_mounts:
            argv += ["-v", f"{m}:{m}"]
        for k, v in (env or {}).items():
            argv += ["-e", f"{k}={v}"]
        argv += [self.image, "bash", "-lc", inner]
        return super().run(argv, cwd=cwd, timeout=timeout, on_line=on_line, env=None)

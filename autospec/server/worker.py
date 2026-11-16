"""Single worker thread over the DB queue (§12): one GPU / one Ollama, so jobs run one at a time."""

from __future__ import annotations

import threading
import traceback
from collections.abc import Callable

from ..config import Settings
from ..core.context import CancelToken
from ..core.events import Event, JsonlFileSink
from ..core.jobs import JobRecord, run_job
from ..core.llm import BaseLLMClient
from ..core.registry import Registry
from ..core.runner import JobStatus
from .db import DbEventSink, Store

LLMFactory = Callable[[JobRecord], BaseLLMClient | None]


def record_event(settings: Settings, store: Store, job_id: str, type: str, message: str, *,
                 level: str = "info", data: dict | None = None) -> None:
    """An event raised outside the pipeline (API action, worker crash, restart): DB + events.log."""
    ev = Event(job_id=job_id, type=type, message=message, level=level, data=data or {})
    ev.id = store.add_event(ev)
    JsonlFileSink(settings.jobs_dir() / job_id / "events.log")(ev)


class Worker:
    def __init__(self, settings: Settings, store: Store, registry: Registry,
                 llm_factory: LLMFactory | None = None, poll_sec: float = 0.5) -> None:
        self.settings = settings
        self.store = store
        self.registry = registry
        self.llm_factory = llm_factory
        self.poll_sec = poll_sec
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._current: tuple[str, CancelToken] | None = None
        self._pending_cancel: set[str] = set()
        self._thread: threading.Thread | None = None

    # --- lifecycle ------------------------------------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="autospec-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 15) -> None:
        """Server shutdown: kill the running job's processes; that job ends up `interrupted` (resumable)."""
        self._stop.set()
        self._wake.set()
        with self._lock:
            cur = self._current
        if cur:
            cur[1].set()
        if self._thread:
            self._thread.join(timeout)

    def wake(self) -> None:
        self._wake.set()

    @property
    def running_job(self) -> str | None:
        with self._lock:
            return self._current[0] if self._current else None

    def cancel(self, job_id: str) -> None:
        with self._lock:
            if self._current and self._current[0] == job_id:
                self._current[1].set()
            else:
                self._pending_cancel.add(job_id)  # claimed but not started yet

    # --- loop -------------------------------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            job_id = self.store.claim_next()
            if job_id is None:
                self._wake.wait(self.poll_sec)
                self._wake.clear()
                continue
            self._run(job_id)

    def _run(self, job_id: str) -> None:
        rec = self.store.job(job_id)
        if rec is None:
            return
        cancel = CancelToken()
        with self._lock:
            self._current = (job_id, cancel)
            if job_id in self._pending_cancel:
                self._pending_cancel.discard(job_id)
                cancel.set()
        try:
            llm = self.llm_factory(rec) if self.llm_factory else None
            run_job(self.settings, rec, self.registry, llm=llm, sinks=[DbEventSink(self.store)],
                    start=rec.current_step, interactive=True, cancel=cancel, on_persist=self.store.save_job)
        except BaseException as e:  # noqa: BLE001 - FakeLLMMismatch is a BaseException; never kill the worker
            if isinstance(e, (KeyboardInterrupt, SystemExit)):
                raise
            rec.status = JobStatus.FAILED.value
            rec.error = "worker_exception"
            rec.state["_error"] = "worker_exception"
            rec.state["_error_message"] = f"{type(e).__name__}: {e}"
            rec.save(self.settings)
            self.store.save_job(rec)
            record_event(self.settings, self.store, job_id, "job.status", f"failed — {type(e).__name__}: {e}",
                         level="error", data={"status": "failed", "error": "worker_exception",
                                              "traceback": traceback.format_exc()[-4000:]})
        finally:
            with self._lock:
                self._current = None
        if self._stop.is_set() and rec.status == JobStatus.CANCELLED.value:
            rec.status = JobStatus.INTERRUPTED.value
            rec.state.pop("_error", None)
            rec.error = None
            rec.save(self.settings)
            self.store.save_job(rec)
            record_event(self.settings, self.store, job_id, "job.status", "interrupted（サーバー停止）",
                         level="warn", data={"status": "interrupted"})

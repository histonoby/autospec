"""`autospec` command: run / resume / gate / approve / profiles / health."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import Settings, load_settings
from .core.events import ConsoleSink
from .core.jobs import JobRecord, build_context, new_job_id, run_job
from .core.llm import OllamaClient
from .core.registry import Registry
from .core.runner import JobStatus
from .pipelines import build_registry
from .steps.approval import record_decision
from .steps.common import run_gate

EXIT_OK, EXIT_FAILED, EXIT_AUDIT_WARN, EXIT_WAITING = 0, 1, 2, 4


def _exit_code(status: JobStatus, state: dict[str, Any]) -> int:
    if status == JobStatus.WAITING_APPROVAL:
        return EXIT_WAITING
    if status != JobStatus.SUCCEEDED or state.get("gate_passed") is False:
        return EXIT_FAILED
    if (state.get("audit") or {}).get("overall_status") == "WARN":
        return EXIT_AUDIT_WARN
    return EXIT_OK


def _summary(rec: JobRecord, status: JobStatus, settings: Settings) -> None:
    st = rec.state
    print("")
    print(f"=== job {rec.id}: {status.value} ===")
    if st.get("_error"):
        print(f"error: {st.get('_error')} — {str(st.get('_error_message', ''))[:500]}")
    if "gate_passed" in st:
        print(f"gate: {'PASS' if st['gate_passed'] else 'FAILED'} (rounds={len(st.get('rounds', []))}, "
              f"best={st.get('best_round')})")
    if st.get("audit"):
        print(f"audit: {st['audit']['overall_status']} score={st['audit']['score']}")
        for r in st["audit"].get("reasons", []):
            print(f"  - {r}")
    print(f"artifacts: {rec.job_dir(settings) / 'artifacts'}")


def _registry(settings: Settings) -> Registry:
    return build_registry(settings)


def cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    if args.model:
        settings.ollama.model = args.model
    reg = _registry(settings)
    ws_root = Path(args.workspace or Path.cwd()).expanduser().resolve()
    inputs: dict[str, Any] = {"spec_path": args.spec, "interface_path": args.interface,
                              "impl_path": args.impl, "test_path": args.test}
    if args.requirements:
        inputs["requirements_path"] = args.requirements
    options: dict[str, Any] = {"workdir_mode": "worktree" if args.worktree else "inplace"}
    for key in ("max_rounds", "reuse_tests", "think", "skip_red_check", "aider_timeout_sec"):
        val = getattr(args, key, None)
        if val not in (None, False):
            options[key] = val
    rec = JobRecord(id=new_job_id(settings), workspace=ws_root.name or "workspace", workspace_root=str(ws_root),
                    pipeline=args.pipeline, profile=args.lang, inputs=inputs, options=options)
    rec.save(settings)
    print(f"🚀 job {rec.id} ({args.pipeline}, profile={args.lang}, model={settings.ollama.model})")
    status, _ = run_job(settings, rec, reg, sinks=[ConsoleSink(show_aider=not args.quiet, verbose=args.verbose)])
    _summary(rec, status, settings)
    return _exit_code(status, rec.state)


def cmd_resume(args: argparse.Namespace, settings: Settings) -> int:
    reg = _registry(settings)
    rec = JobRecord.load(settings, args.job)
    if rec.status in (JobStatus.SUCCEEDED.value,) and args.from_step is None:
        print("job already succeeded")
        return EXIT_OK
    start = args.from_step if args.from_step is not None else rec.current_step
    status, _ = run_job(settings, rec, reg, sinks=[ConsoleSink(verbose=args.verbose)], start=start)
    _summary(rec, status, settings)
    return _exit_code(status, rec.state)


def cmd_approve(args: argparse.Namespace, settings: Settings) -> int:
    rec = JobRecord.load(settings, args.job)
    edited = Path(args.edited_file).read_text(encoding="utf-8") if args.edited_file else None
    record_decision(rec.state, args.target, "reject" if args.reject else "approve", args.comment or "", edited)
    rec.save(settings)
    print(f"{args.target}: {'reject' if args.reject else 'approve'} を記録しました。`autospec resume {rec.id}` で続行します")
    return EXIT_OK


def cmd_gate(args: argparse.Namespace, settings: Settings) -> int:
    if args.data_dir:
        settings.paths.data_dir = args.data_dir
    reg = _registry(settings)
    if args.job:
        rec = JobRecord.load(settings, args.job)
    else:
        if not (args.lang and args.test):
            print("--job か、--lang/--impl/--test を指定してください", file=sys.stderr)
            return 2
        cwd = Path.cwd().resolve()
        rec = JobRecord(id="adhoc", workspace=cwd.name, workspace_root=str(cwd), pipeline="implement",
                        profile=args.lang, inputs={}, options={},
                        state={"workdir": str(cwd), "paths": {
                            "interface": args.interface, "spec": None,
                            "impl": [args.impl] if args.impl else [], "tests": [args.test]}})
        settings.paths.data_dir = str(Path(settings.paths.data_dir).expanduser())
    ctx = build_context(settings, rec, reg)
    outcome = run_gate(ctx, args.gate, quiet=args.job is None)
    sys.stdout.write(outcome.output)
    if outcome.timed_out:
        print(f"\n[TIMEOUT] gate '{args.gate}' timed out")
    return 0 if outcome.passed else (outcome.returncode or 1)


def cmd_profiles(args: argparse.Namespace, settings: Settings) -> int:
    reg = _registry(settings)
    for p in reg.profiles.values():
        print(f"{p.name:8s} {p.display_name}  gates={','.join(p.gates)}  post={','.join(p.post_gates) or '-'}")
    return EXIT_OK


def cmd_health(args: argparse.Namespace, settings: Settings) -> int:
    info: dict[str, Any] = OllamaClient(settings).health()
    info["aider"] = shutil.which(settings.aider.bin)
    info["docker"] = shutil.which("docker")
    info["git"] = shutil.which("git")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return EXIT_OK if info.get("ollama") and info.get("model_available") and info["aider"] else EXIT_FAILED


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="autospec")
    ap.add_argument("--config", help="config.yaml path (default: $AUTOSPEC_CONFIG or ./config.yaml)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run a pipeline")
    r.add_argument("pipeline", choices=["implement"])
    r.add_argument("--lang", "--profile", dest="lang", required=True)
    r.add_argument("--spec", required=True)
    r.add_argument("--interface")
    r.add_argument("--impl", required=True)
    r.add_argument("--test", required=True)
    r.add_argument("--requirements", help="use an existing requirements.json instead of extracting")
    r.add_argument("--workspace", help="workspace (git repo) directory; default: current directory")
    r.add_argument("--worktree", action="store_true", help="work in DATA_DIR/worktrees/job-<id> instead of in place")
    r.add_argument("--max-rounds", dest="max_rounds", type=int)
    r.add_argument("--aider-timeout", dest="aider_timeout_sec", type=int)
    r.add_argument("--reuse-tests", action="store_true")
    r.add_argument("--skip-red-check", action="store_true")
    r.add_argument("--think", action="store_true", help="think=on for test generation")
    r.add_argument("--model", help="override ollama.model")
    r.add_argument("--quiet", action="store_true", help="hide aider output")
    r.add_argument("--verbose", action="store_true")
    r.set_defaults(func=cmd_run)

    s = sub.add_parser("resume", help="resume an interrupted / failed / waiting job")
    s.add_argument("job")
    s.add_argument("--from-step", type=int)
    s.add_argument("--verbose", action="store_true")
    s.set_defaults(func=cmd_resume)

    a = sub.add_parser("approve", help="record an approval decision")
    a.add_argument("job")
    a.add_argument("target")
    a.add_argument("--reject", action="store_true")
    a.add_argument("--comment")
    a.add_argument("--edited-file")
    a.set_defaults(func=cmd_approve)

    g = sub.add_parser("gate", help="run a gate through the sandbox and return its exit code")
    g.add_argument("gate")
    g.add_argument("--job")
    g.add_argument("--data-dir")
    g.add_argument("--lang")
    g.add_argument("--interface")
    g.add_argument("--impl")
    g.add_argument("--test")
    g.set_defaults(func=cmd_gate)

    p = sub.add_parser("profiles", help="list language profiles")
    p.set_defaults(func=cmd_profiles)
    h = sub.add_parser("health", help="check Ollama / model / aider / docker")
    h.set_defaults(func=cmd_health)
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = load_settings(args.config)
    return int(args.func(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())

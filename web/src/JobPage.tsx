import { useCallback, useEffect, useRef, useState } from "react";
import type { JSX } from "react";
import { api, TERMINAL } from "./api";
import type { Approval, JobDetail, JobEvent, StepRow } from "./api";
import { ArtifactsView } from "./ArtifactsView";
import { LogView } from "./LogView";
import { ErrorBox, StatusPill, duration, fmtTime, useLoad } from "./ui";

const REFRESH_TYPES = new Set(["job.status", "step.start", "step.end", "approval.required", "approval.resolved", "gate.result"]);
const RESUMABLE = new Set(["interrupted", "failed", "cancelled"]);

export function JobPage({ id }: { id: string }): JSX.Element {
  const job = useLoad(() => api.job(id), [id]);
  const [tab, setTab] = useState<"log" | "artifacts">("log");
  const [generation, setGeneration] = useState(0);
  const [actionError, setActionError] = useState("");
  const timer = useRef<number | undefined>(undefined);
  const { reload } = job;

  const onEvent = useCallback((ev: JobEvent): void => {
    if (!REFRESH_TYPES.has(ev.type)) return;
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => void reload(), 250);
  }, [reload]);
  useEffect(() => () => window.clearTimeout(timer.current), []);

  const act = async (fn: () => Promise<unknown>): Promise<void> => {
    setActionError("");
    try {
      await fn();
      await reload();
      setGeneration((g) => g + 1); // reopen the event stream after resume / approve
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  };

  const d = job.data;
  return (
    <>
      <p className="crumbs">
        <a href="#/">ワークスペース</a> / {d && <a href={`#/ws/${encodeURIComponent(d.workspace)}`}>{d.workspace}</a>} /
      </p>
      <h1 className="job-title">{id} {d && <StatusPill status={d.status} />}</h1>
      <ErrorBox error={job.error || actionError} />
      {d && (
        <>
          <JobSummary job={d} onCancel={() => void act(() => api.cancel(id))}
            onResume={(from) => void act(() => api.resume(id, from))} />
          {d.status === "waiting_approval" && (
            <ApprovalPanel job={d} onDecide={(target, decision, comment) =>
              void act(() => api.approve(id, { target, decision, comment }))} />
          )}
          <Stepper job={d} />
        </>
      )}
      <div className="tabs">
        <button className={tab === "log" ? "on" : ""} onClick={() => setTab("log")}>ライブログ</button>
        <button className={tab === "artifacts" ? "on" : ""} onClick={() => setTab("artifacts")}>成果物</button>
      </div>
      <div hidden={tab !== "log"}>
        <LogView jobId={id} generation={generation} onEvent={onEvent} />
      </div>
      {tab === "artifacts" && <ArtifactsView jobId={id} refreshKey={d?.updated_at ?? ""} />}
    </>
  );
}

function JobSummary({ job, onCancel, onResume }: {
  job: JobDetail; onCancel: () => void; onResume: (from?: number) => void;
}): JSX.Element {
  const st = job.state;
  const [from, setFrom] = useState<string>("");
  const rounds = st.rounds ?? [];
  const audit = st.audit;
  return (
    <section className="card">
      <dl className="kv">
        <dt>プロファイル</dt><dd>{job.profile ?? "自動"}</dd>
        <dt>モデル</dt><dd>{job.model}</dd>
        <dt>作成</dt><dd>{fmtTime(job.created_at)}</dd>
        <dt>入力</dt>
        <dd className="path small">
          {Object.entries(job.inputs).filter(([k]) => k !== "spec_text").map(([k, v]) => `${k}=${String(v)}`).join("  ")}
          {"spec_text" in job.inputs && " spec_text=（直接入力）"}
        </dd>
        {rounds.length > 0 && (<><dt>aider</dt><dd>
          {rounds.map((r) => (
            <span key={r.round} className={`round ${r.passed ? "pass" : "fail"}`} title={r.digest}>
              R{r.round} {r.passed ? "✓" : `✗${r.failures}`}
            </span>
          ))}
          {job.status === "running" && st.aider_round && st.aider_round > rounds.length &&
            <span className="round now">R{st.aider_round} 実行中…</span>}
        </dd></>)}
        {st.gate_passed !== undefined && (<><dt>ゲート</dt>
          <dd className={st.gate_passed ? "good" : "bad"}>{st.gate_passed ? "PASS" : "未通過"}
            {st.best_round ? `（ラウンド ${st.best_round}）` : ""}</dd></>)}
        {audit && (<><dt>監査</dt><dd className={audit.overall_status === "PASS" ? "good" : "bad"}>
          {audit.overall_status} · スコア {audit.score}
          {audit.reasons?.length > 0 && <ul className="reasons">{audit.reasons.map((r) => <li key={r}>{r}</li>)}</ul>}
        </dd></>)}
      </dl>
      {st._error && st._error !== "cancelled" && (
        <div className="error-box"><strong>{st._error}</strong> {st._error_message}</div>
      )}
      <div className="actions">
        {!TERMINAL.has(job.status) && (
          <button className="danger" onClick={() => {
            if (window.confirm("このジョブをキャンセルしますか？実行中の aider も停止します。")) onCancel();
          }}>キャンセル</button>
        )}
        {RESUMABLE.has(job.status) && (
          <span className="resume">
            <select value={from} onChange={(e) => setFrom(e.target.value)} aria-label="再開するステップ">
              <option value="">続きから（{job.steps[job.current_step]?.name ?? job.current_step}）</option>
              {job.steps.map((s) => <option key={s.idx} value={s.idx}>{s.idx}: {s.name} から</option>)}
            </select>
            <button className="primary" onClick={() => onResume(from === "" ? undefined : Number(from))}>再開</button>
          </span>
        )}
      </div>
    </section>
  );
}

const STEP_ICON: Record<string, string> = {
  done: "✓", failed: "✗", running: "●", skipped: "–", cancelled: "■", interrupted: "‖",
  waiting_approval: "✋", pending: "○",
};

function Stepper({ job }: { job: JobDetail }): JSX.Element {
  const [, tick] = useState(0);
  useEffect(() => {
    if (job.status !== "running") return;
    const t = window.setInterval(() => tick((n) => n + 1), 1000);
    return () => window.clearInterval(t);
  }, [job.status]);
  return (
    <section className="card">
      <h2>ステップ</h2>
      <ol className="stepper">
        {job.steps.map((s: StepRow) => {
          const current = s.status === "running" || (s.idx === job.current_step && !TERMINAL.has(job.status) && s.status !== "done");
          return (
            <li key={s.idx} className={`step st-${s.status} ${current ? "current" : ""}`}>
              <span className="icon">{STEP_ICON[s.status] ?? "○"}</span>
              <span className="name">{s.name}
                {s.name === "implement" && job.state.aider_round ? <span className="muted small"> ラウンド {job.state.aider_round}</span> : null}
              </span>
              <span className="muted small time">{duration(s.started_at, s.ended_at)}</span>
              {s.message && s.status !== "running" && <span className="msg small">{s.message}</span>}
            </li>
          );
        })}
      </ol>
    </section>
  );
}

const TARGET_TEXT: Record<string, { title: string; approve: string; reject: string }> = {
  test_dispute: {
    title: "テストが仕様とずれている疑いがあります",
    approve: "テストは正しい（このまま続行）",
    reject: "中止してテストを見直す",
  },
};

function ApprovalPanel({ job, onDecide }: {
  job: JobDetail; onDecide: (target: string, decision: "approve" | "reject", comment: string) => void;
}): JSX.Element {
  const pending: Approval[] = job.approvals.filter((a) => a.decision === null);
  const [comment, setComment] = useState("");
  if (pending.length === 0) return <section className="card warn"><p>承認待ちです（対象を読み込み中）</p></section>;
  return (
    <>
      {pending.map((a) => {
        const text = TARGET_TEXT[a.target] ?? { title: `${a.target} の承認`, approve: "承認", reject: "却下" };
        const content = job.state.approval_content?.[a.target] as Record<string, unknown> | undefined;
        return (
          <section key={a.target} className="card warn">
            <h2>✋ {text.title}</h2>
            {content && a.target === "test_dispute" ? (
              <dl className="kv">
                <dt>テスト</dt><dd className="path">{String(content.test_name)}</dd>
                <dt>要件</dt><dd>{String(content.req_id)}</dd>
                <dt>理由</dt><dd>{String(content.reason)}</dd>
              </dl>
            ) : content ? <pre className="code">{JSON.stringify(content, null, 2)}</pre> : null}
            <label className="form">コメント（任意）
              <textarea rows={2} value={comment} onChange={(e) => setComment(e.target.value)} />
            </label>
            <div className="actions">
              <button className="primary" onClick={() => onDecide(a.target, "approve", comment)}>{text.approve}</button>
              <button className="danger" onClick={() => onDecide(a.target, "reject", comment)}>{text.reject}</button>
            </div>
          </section>
        );
      })}
    </>
  );
}

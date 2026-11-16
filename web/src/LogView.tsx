import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { JSX } from "react";
import { api } from "./api";
import type { JobEvent } from "./api";

const MAX_EVENTS = 5000;

const GROUPS: { key: string; label: string; match: (t: string) => boolean }[] = [
  { key: "step", label: "ステップ", match: (t) => t.startsWith("step.") || t.startsWith("job.") || t.startsWith("approval.") },
  { key: "log", label: "ログ", match: (t) => t === "log" },
  { key: "gate", label: "ゲート", match: (t) => t === "gate.result" },
  { key: "aider", label: "aider", match: (t) => t === "aider.output" },
  { key: "llm", label: "LLM", match: (t) => t.startsWith("llm.") },
  { key: "artifact", label: "成果物", match: (t) => t === "artifact.created" },
];

function groupOf(type: string): string {
  return GROUPS.find((g) => g.match(type))?.key ?? "log";
}

/** Live log over SSE. `generation` bumps (resume / approve) reopen the stream after the last seen id. */
export function LogView({ jobId, generation, onEvent }: {
  jobId: string; generation: number; onEvent: (ev: JobEvent) => void;
}): JSX.Element {
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [shown, setShown] = useState<Record<string, boolean>>(
    () => Object.fromEntries(GROUPS.map((g) => [g.key, true])));
  const [follow, setFollow] = useState(true);
  const [state, setState] = useState<"connecting" | "live" | "ended" | "retrying">("connecting");
  const lastId = useRef(0);
  const box = useRef<HTMLDivElement>(null);
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    const es = new EventSource(api.eventsUrl(jobId, lastId.current));
    setState("connecting");
    es.onopen = () => setState("live");
    es.onmessage = (m: MessageEvent<string>) => {
      const ev = JSON.parse(m.data) as JobEvent;
      if (ev.id <= lastId.current) return;
      lastId.current = ev.id;
      setEvents((cur) => {
        const next = cur.length >= MAX_EVENTS ? cur.slice(cur.length - MAX_EVENTS + 1) : cur.slice();
        next.push(ev);
        return next;
      });
      onEventRef.current(ev);
    };
    es.addEventListener("end", () => {
      setState("ended");
      es.close();
    });
    es.onerror = () => setState(es.readyState === EventSource.CLOSED ? "ended" : "retrying");
    return () => es.close();
  }, [jobId, generation]);

  useLayoutEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [events, follow, shown]);

  const visible = events.filter((e) => shown[groupOf(e.type)]);
  return (
    <section className="card log-card">
      <div className="log-tools">
        {GROUPS.map((g) => (
          <label key={g.key} className="check small">
            <input type="checkbox" checked={shown[g.key]} onChange={(e) => setShown({ ...shown, [g.key]: e.target.checked })} />
            {g.label}
          </label>
        ))}
        <label className="check small follow">
          <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> 自動スクロール
        </label>
        <span className={`stream st-${state}`}>
          {{ connecting: "接続中", live: "● ライブ", ended: "終了", retrying: "再接続中" }[state]}
        </span>
      </div>
      <div className="log" ref={box} onWheel={() => {
        const el = box.current;
        if (el && follow && el.scrollHeight - el.scrollTop - el.clientHeight > 80) setFollow(false);
      }}>
        {visible.length === 0 && <div className="muted">（まだイベントがありません）</div>}
        {visible.map((e) => <LogLine key={e.id} ev={e} />)}
      </div>
    </section>
  );
}

function LogLine({ ev }: { ev: JobEvent }): JSX.Element {
  const time = ev.ts.slice(11, 19);
  const g = groupOf(ev.type);
  let extra: JSX.Element | null = null;
  if (ev.type === "gate.result") {
    const tail = (ev.data.digest ?? ev.data.output_tail) as string | undefined;
    if (tail && ev.data.returncode !== 0) extra = <details><summary>エラー要約</summary><pre>{tail}</pre></details>;
  } else if (ev.data.traceback) {
    extra = <details><summary>traceback</summary><pre>{String(ev.data.traceback)}</pre></details>;
  }
  return (
    <div className={`line g-${g} lv-${ev.level}`}>
      <span className="t">{time}</span>
      {ev.step && g !== "aider" && <span className="s">[{ev.step}]</span>}
      <span className="m">{ev.message}</span>
      {extra}
    </div>
  );
}

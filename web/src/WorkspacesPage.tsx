import { useState } from "react";
import type { FormEvent, JSX } from "react";
import { go } from "./App";
import { api } from "./api";
import type { Sample } from "./api";
import { ErrorBox, StatusPill, fmtTime, useLoad } from "./ui";

export function WorkspacesPage(): JSX.Element {
  const list = useLoad(() => api.workspaces(), []);
  const samples = useLoad(() => api.samples(), []);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const trySample = async (s: Sample): Promise<void> => {
    setBusy(true);
    setError("");
    try {
      const taken = new Set((list.data ?? []).map((w) => w.name));
      let name = s.name;
      for (let i = 2; taken.has(name); i++) name = `${s.name}-${i}`;
      await api.createWorkspace({ name, mode: "import", source_path: s.source_path });
      const { id } = await api.createJob({ workspace: name, pipeline: "implement", profile: s.profile, inputs: s.inputs, options: {} });
      go(`/jobs/${id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  };

  return (
    <>
      <h1>ワークスペース</h1>
      {samples.data && samples.data.length > 0 && (
        <section className="card accent">
          <h2>すぐ試す</h2>
          <p className="muted">サンプルを新しいワークスペースに取り込み、implement ジョブを開始します。</p>
          {samples.data.map((s) => (
            <button key={s.name} className="primary" disabled={busy} onClick={() => void trySample(s)}>
              ▶ {s.title} で実行
            </button>
          ))}
        </section>
      )}
      <ErrorBox error={error || list.error} />
      <section className="card">
        {list.data?.length === 0 && <p className="muted">まだありません。下のフォームで作成してください。</p>}
        <ul className="rows">
          {list.data?.map((w) => (
            <li key={w.name}>
              <a href={`#/ws/${encodeURIComponent(w.name)}`} className="row-main">
                <strong>{w.name}</strong>
                <span className="muted small path">{w.path}</span>
              </a>
              <span className="row-side">
                {w.last_status ? (
                  <a href={`#/jobs/${w.last_job}`}><StatusPill status={w.last_status} /></a>
                ) : <span className="muted small">ジョブなし</span>}
                <span className="muted small">{fmtTime(w.created_at)}</span>
              </span>
            </li>
          ))}
        </ul>
      </section>
      <CreateWorkspace onCreated={(name) => go(`/ws/${encodeURIComponent(name)}`)} />
    </>
  );
}

function CreateWorkspace({ onCreated }: { onCreated: (name: string) => void }): JSX.Element {
  const [name, setName] = useState("");
  const [mode, setMode] = useState<"empty" | "import">("import");
  const [source, setSource] = useState("");
  const [error, setError] = useState("");
  const submit = async (e: FormEvent): Promise<void> => {
    e.preventDefault();
    try {
      const r = await api.createWorkspace({ name, mode, source_path: mode === "import" ? source : undefined });
      onCreated(r.name);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };
  return (
    <section className="card">
      <h2>新規ワークスペース</h2>
      <form onSubmit={(e) => void submit(e)} className="form">
        <label>名前（英数字・_・-）
          <input value={name} onChange={(e) => setName(e.target.value)} required pattern="[A-Za-z0-9_-]{1,64}" />
        </label>
        <label>作り方
          <select value={mode} onChange={(e) => setMode(e.target.value as "empty" | "import")}>
            <option value="import">既存のディレクトリを取り込む（git なら clone、それ以外はコピー）</option>
            <option value="empty">空のリポジトリ</option>
          </select>
        </label>
        {mode === "import" && (
          <label>取り込むディレクトリ（このマシン上の絶対パス）
            <input value={source} onChange={(e) => setSource(e.target.value)} required placeholder="/home/me/project" />
          </label>
        )}
        <ErrorBox error={error} />
        <button type="submit" className="primary">作成</button>
      </form>
    </section>
  );
}

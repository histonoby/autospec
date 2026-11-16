import { useState } from "react";
import type { FormEvent, JSX } from "react";
import { go } from "./App";
import { api } from "./api";
import { ErrorBox, StatusPill, fmtTime, useLoad } from "./ui";

type Field = "spec_path" | "interface_path" | "impl_path" | "test_path";
const FIELD_LABEL: Record<Field, string> = {
  spec_path: "仕様書", interface_path: "インターフェース", impl_path: "実装", test_path: "テスト",
};

export function WorkspacePage({ name }: { name: string }): JSX.Element {
  const ws = useLoad(() => api.workspace(name), [name]);
  const [paths, setPaths] = useState<Record<Field, string>>({ spec_path: "", interface_path: "", impl_path: "", test_path: "" });
  const assign = (f: Field, p: string): void => setPaths((cur) => ({ ...cur, [f]: p }));

  return (
    <>
      <p className="crumbs"><a href="#/">ワークスペース</a> /</p>
      <h1>{name}</h1>
      <ErrorBox error={ws.error} />
      {ws.data && (
        <section className="card">
          <dl className="kv">
            <dt>場所</dt><dd className="path">{ws.data.path}</dd>
            <dt>ブランチ</dt><dd>{ws.data.branch ?? "-"}</dd>
            <dt>最新コミット</dt><dd>{ws.data.head || "-"}</dd>
          </dl>
        </section>
      )}
      <JobForm workspace={name} paths={paths} setPath={assign} />
      <section className="card">
        <h2>ジョブ履歴</h2>
        {ws.data?.jobs.length === 0 && <p className="muted">まだありません。</p>}
        <ul className="rows">
          {ws.data?.jobs.map((j) => (
            <li key={j.id}>
              <a href={`#/jobs/${j.id}`} className="row-main">
                <strong>{j.id}</strong>
                <span className="muted small">{j.pipeline} · {j.profile ?? "auto"} · {fmtTime(j.created_at)}</span>
              </a>
              <span className="row-side"><StatusPill status={j.status} /></span>
            </li>
          ))}
        </ul>
      </section>
      <FileBrowser workspace={name} onAssign={assign} />
    </>
  );
}

function JobForm({ workspace, paths, setPath }: {
  workspace: string; paths: Record<Field, string>; setPath: (f: Field, p: string) => void;
}): JSX.Element {
  const profiles = useLoad(() => api.profiles(), []);
  const [profile, setProfile] = useState("");
  const [specMode, setSpecMode] = useState<"path" | "text">("path");
  const [specText, setSpecText] = useState("");
  const [maxRounds, setMaxRounds] = useState("");
  const [timeout, setTimeoutSec] = useState("");
  const [think, setThink] = useState(false);
  const [skipRed, setSkipRed] = useState(false);
  const [reuse, setReuse] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent): Promise<void> => {
    e.preventDefault();
    setBusy(true);
    setError("");
    const inputs: Record<string, unknown> = {
      interface_path: paths.interface_path, impl_path: paths.impl_path, test_path: paths.test_path,
      ...(specMode === "path" ? { spec_path: paths.spec_path } : { spec_text: specText }),
    };
    const options: Record<string, unknown> = {};
    if (maxRounds) options.max_rounds = Number(maxRounds);
    if (timeout) options.aider_timeout_sec = Number(timeout);
    if (think) options.think = true;
    if (skipRed) options.skip_red_check = true;
    if (reuse) options.reuse_tests = true;
    try {
      const { id } = await api.createJob({ workspace, pipeline: "implement", profile: profile || undefined, inputs, options });
      go(`/jobs/${id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  const pathInput = (f: Field, required: boolean, placeholder: string): JSX.Element => (
    <label>{FIELD_LABEL[f]}のパス
      <input value={paths[f]} onChange={(e) => setPath(f, e.target.value)} required={required} placeholder={placeholder} />
    </label>
  );

  return (
    <section className="card">
      <h2>新規ジョブ（implement）</h2>
      <p className="muted small">仕様書からテストを作り、aider に実装させてゲートを通します。パスはワークスペース内の相対パスです。下のファイル一覧から割り当てることもできます。</p>
      <form onSubmit={(e) => void submit(e)} className="form">
        <label>言語プロファイル
          <select value={profile} onChange={(e) => setProfile(e.target.value)}>
            <option value="">自動判定（拡張子から）</option>
            {profiles.data?.map((p) => <option key={p.name} value={p.name}>{p.display_name}（{p.name}）</option>)}
          </select>
        </label>
        <div className="seg">
          <button type="button" className={specMode === "path" ? "on" : ""} onClick={() => setSpecMode("path")}>仕様書をファイルで</button>
          <button type="button" className={specMode === "text" ? "on" : ""} onClick={() => setSpecMode("text")}>仕様書を直接入力</button>
        </div>
        {specMode === "path" ? pathInput("spec_path", true, "spec.md") : (
          <label>仕様書
            <textarea value={specText} onChange={(e) => setSpecText(e.target.value)} rows={8} required />
          </label>
        )}
        {pathInput("interface_path", true, "include/foo.h")}
        {pathInput("impl_path", true, "src/foo.c（無ければ雛形を作ります）")}
        {pathInput("test_path", true, "tests/test_foo.c（生成されます）")}
        <details>
          <summary>詳細オプション</summary>
          <label>aider の最大ラウンド数
            <input type="number" min={1} max={20} value={maxRounds} onChange={(e) => setMaxRounds(e.target.value)} placeholder="設定値（既定 5）" />
          </label>
          <label>aider 1回のタイムアウト（秒）
            <input type="number" min={60} value={timeout} onChange={(e) => setTimeoutSec(e.target.value)} placeholder="設定値（既定 1800）" />
          </label>
          <label className="check"><input type="checkbox" checked={think} onChange={(e) => setThink(e.target.checked)} /> テスト生成で think を使う</label>
          <label className="check"><input type="checkbox" checked={skipRed} onChange={(e) => setSkipRed(e.target.checked)} /> red_check（スタブでテストが落ちるかの確認）を省く</label>
          <label className="check"><input type="checkbox" checked={reuse} onChange={(e) => setReuse(e.target.checked)} /> 既存のテストファイルを使う（生成しない）</label>
        </details>
        <ErrorBox error={error} />
        <button type="submit" className="primary" disabled={busy}>ジョブを開始</button>
      </form>
    </section>
  );
}

function FileBrowser({ workspace, onAssign }: { workspace: string; onAssign: (f: Field, p: string) => void }): JSX.Element {
  const [dir, setDir] = useState("");
  const [file, setFile] = useState<{ path: string; text: string } | null>(null);
  const tree = useLoad(() => api.tree(workspace, dir), [workspace, dir]);
  const [error, setError] = useState("");
  const open = async (path: string): Promise<void> => {
    try {
      setFile({ path, text: await api.file(workspace, path) });
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };
  const up = dir.split("/").slice(0, -1).join("/");
  return (
    <section className="card">
      <h2>ファイル</h2>
      <p className="muted small path">/{dir}</p>
      <ErrorBox error={error || tree.error} />
      <ul className="tree">
        {dir && <li><button className="link" onClick={() => setDir(up)}>📁 ..</button></li>}
        {tree.data?.map((e) => (
          <li key={e.path}>
            {e.type === "dir"
              ? <button className="link" onClick={() => setDir(e.path)}>📁 {e.name}</button>
              : <button className="link" onClick={() => void open(e.path)}>📄 {e.name} <span className="muted small">{e.size} B</span></button>}
          </li>
        ))}
      </ul>
      {file && (
        <div className="file-view">
          <div className="file-head">
            <strong className="path">{file.path}</strong>
            <span className="assign">
              {(Object.keys(FIELD_LABEL) as Field[]).map((f) => (
                <button key={f} className="small-btn" onClick={() => onAssign(f, file.path)}>{FIELD_LABEL[f]}に使う</button>
              ))}
            </span>
          </div>
          <pre className="code">{file.text}</pre>
        </div>
      )}
    </section>
  );
}

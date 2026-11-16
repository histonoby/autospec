import { useEffect, useMemo, useState } from "react";
import type { JSX } from "react";
import { Marked } from "marked";
import { api } from "./api";
import { ErrorBox, useLoad } from "./ui";

const ORDER = ["audit_report.md", "diff.patch", "requirements.json", "coverage.json", "rounds.json", "audit_result.json"];

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c] ?? c);
}

// LLM-written Markdown: raw HTML is shown as text, never rendered.
const md = new Marked({ gfm: true, breaks: false });
md.use({ renderer: { html: (token) => escapeHtml(token.text) } });

export function ArtifactsView({ jobId, refreshKey }: { jobId: string; refreshKey: string }): JSX.Element {
  const list = useLoad(() => api.artifacts(jobId), [jobId, refreshKey]);
  const [name, setName] = useState<string>("");
  const items = useMemo(() => {
    const all = list.data ?? [];
    const rank = (n: string): number => (ORDER.includes(n) ? ORDER.indexOf(n) : n.startsWith("llm/") ? 1000 : 100);
    return [...all].sort((a, b) => rank(a.name) - rank(b.name) || a.name.localeCompare(b.name));
  }, [list.data]);
  useEffect(() => {
    if (!name && items.length) setName(items[0].name);
  }, [items, name]);

  return (
    <section className="card">
      <ErrorBox error={list.error} />
      {list.data === null && !list.error ? <p className="muted">読み込み中…</p> : items.length === 0 ? <p className="muted">まだ成果物がありません。</p> : (
        <select className="artifact-select" value={name} onChange={(e) => setName(e.target.value)}>
          {items.map((a) => <option key={a.name} value={a.name}>{a.name}（{a.size} B）</option>)}
        </select>
      )}
      {name && <ArtifactBody jobId={jobId} name={name} refreshKey={refreshKey} />}
    </section>
  );
}

function ArtifactBody({ jobId, name, refreshKey }: { jobId: string; name: string; refreshKey: string }): JSX.Element {
  const body = useLoad(() => api.artifact(jobId, name), [jobId, name, refreshKey]);
  if (body.error) return <ErrorBox error={body.error} />;
  if (body.data === null) return <p className="muted">読み込み中…</p>;
  const text = typeof body.data === "string" ? body.data : JSON.stringify(body.data, null, 2);
  if (name.endsWith(".md")) {
    return <div className="markdown" dangerouslySetInnerHTML={{ __html: md.parse(text, { async: false }) }} />;
  }
  if (name.endsWith(".json")) {
    let pretty = text;
    try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch { /* keep raw */ }
    return <pre className="code">{pretty}</pre>;
  }
  if (name.endsWith(".patch") || name.endsWith(".diff")) {
    if (!text.trim()) return <p className="muted">差分はありません。</p>;
    return (
      <pre className="code diff">
        {text.split("\n").map((l, i) => {
          const cls = l.startsWith("+++") || l.startsWith("---") ? "meta" : l.startsWith("+") ? "add"
            : l.startsWith("-") ? "del" : l.startsWith("@@") ? "hunk" : l.startsWith("diff ") ? "meta" : "";
          return <span key={i} className={cls}>{l}{"\n"}</span>;
        })}
      </pre>
    );
  }
  return <pre className="code">{text}</pre>;
}

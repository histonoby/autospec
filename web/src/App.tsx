import { useEffect, useState } from "react";
import type { JSX } from "react";
import { api } from "./api";
import type { Health } from "./api";
import { JobPage } from "./JobPage";
import { WorkspacePage } from "./WorkspacePage";
import { WorkspacesPage } from "./WorkspacesPage";

type Route = { page: "home" } | { page: "ws"; name: string } | { page: "job"; id: string };

function parseHash(hash: string): Route {
  const parts = hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent);
  if (parts[0] === "ws" && parts[1]) return { page: "ws", name: parts[1] };
  if (parts[0] === "jobs" && parts[1]) return { page: "job", id: parts[1] };
  return { page: "home" };
}

export function go(path: string): void {
  window.location.hash = path;
}

export function App(): JSX.Element {
  const [route, setRoute] = useState<Route>(() => parseHash(window.location.hash));
  useEffect(() => {
    const onHash = (): void => setRoute(parseHash(window.location.hash));
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  return (
    <>
      <header className="topbar">
        <a href="#/" className="brand">AutoSpec</a>
        <HealthBadge />
      </header>
      <main className="container">
        {route.page === "home" && <WorkspacesPage />}
        {route.page === "ws" && <WorkspacePage key={route.name} name={route.name} />}
        {route.page === "job" && <JobPage key={route.id} id={route.id} />}
      </main>
    </>
  );
}

function HealthBadge(): JSX.Element {
  const [h, setH] = useState<Health | null>(null);
  const [err, setErr] = useState("");
  useEffect(() => {
    let alive = true;
    const load = async (): Promise<void> => {
      try {
        const v = await api.health();
        if (alive) { setH(v); setErr(""); }
      } catch (e) {
        if (alive) setErr(String(e));
      }
    };
    void load();
    const t = window.setInterval(() => void load(), 5000);
    return () => { alive = false; window.clearInterval(t); };
  }, []);
  if (err) return <span className="health bad" title={err}>サーバー応答なし</span>;
  if (!h) return <span className="health">…</span>;
  const ok = h.ollama && h.model_available && h.aider;
  const info = h.model_info;
  const detail = info ? [info.family, info.parameter_size, info.quantization].filter(Boolean).join(" · ") : "";
  const ctx = info?.modelfile_num_ctx ?? info?.request_num_ctx;
  const title = [
    h.ollama ? "Ollama: 接続OK" : `Ollama: ${h.error ?? "未接続"}`,
    `モデル: ${h.model ?? "-"}${h.model_available ? "" : "（見つかりません）"}`,
    `aider: ${h.aider ?? "見つかりません"}`,
  ].join("\n");
  return (
    <span className={`health ${ok ? "ok" : "bad"}`} title={title}>
      <span className="dot" />
      <span className="model">{h.model ?? "model?"}</span>
      {detail && <span className="muted"> {detail}{ctx ? ` · ctx ${Math.round(ctx / 1024)}k` : ""}</span>}
      {h.running_job && <a href={`#/jobs/${h.running_job}`} className="running-link">実行中</a>}
    </span>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import type { JSX } from "react";
import { STATUS_LABEL } from "./api";

export function StatusPill({ status }: { status: string }): JSX.Element {
  return <span className={`pill s-${status}`}>{STATUS_LABEL[status] ?? status}</span>;
}

export function ErrorBox({ error }: { error: string }): JSX.Element | null {
  return error ? <div className="error-box">{error}</div> : null;
}

export function fmtTime(iso: string | null | undefined): string {
  if (!iso) return "";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString("ja-JP", { hour12: false });
}

export function duration(start: string | null, end: string | null): string {
  if (!start) return "";
  const ms = (end ? new Date(end) : new Date()).getTime() - new Date(start).getTime();
  if (!(ms >= 0)) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}秒` : `${Math.floor(s / 60)}分${s % 60}秒`;
}

/** Load async data with error state; `reload` re-fetches. */
export function useLoad<T>(fn: () => Promise<T>, deps: unknown[]): {
  data: T | null; error: string; reload: () => Promise<void>;
} {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const key = JSON.stringify(deps);
  const reload = useCallback(async (): Promise<void> => {
    try {
      setData(await fnRef.current());
      setError("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  useEffect(() => { void reload(); }, [reload, key]);
  return { data, error, reload };
}

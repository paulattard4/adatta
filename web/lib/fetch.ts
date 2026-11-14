"use client";
import { useEffect, useState } from "react";

/** GET JSON from the API; with refreshMs, fetch again on that interval (keeping the last data). */
export function useJson<T>(url: string | null, refreshMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!refreshMs) return;
    const t = setInterval(() => setTick((n) => n + 1), refreshMs);
    return () => clearInterval(t);
  }, [refreshMs]);
  useEffect(() => {
    if (!url) return;
    let live = true;
    setLoading(true);
    setError(null);
    fetch(url)
      .then(async (r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return (await r.json()) as T;
      })
      .then((j) => live && setData(j))
      .catch((e: Error) => live && setError(e.message))
      .finally(() => live && setLoading(false));
    return () => { live = false; };
  }, [url, tick]);
  return { data, error, loading };
}

export function useNow(intervalMs = 1000) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), intervalMs);
    return () => clearInterval(t);
  }, [intervalMs]);
  return now;
}

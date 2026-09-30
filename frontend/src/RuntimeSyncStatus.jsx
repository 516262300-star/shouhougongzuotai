import { useEffect, useState } from "react";
import { syncStatusView } from "./sync-status.mjs";

export function RuntimeSyncStatus({ freshness }) {
  const [monitor, setMonitor] = useState(null);
  useEffect(() => {
    let controller;
    let timer;
    let active = true;
    const load = async () => {
      controller = new AbortController();
      const timeout = setTimeout(() => controller.abort(), 10_000);
      try {
        const response = await fetch("/api/v1/monitor/status", {
          cache: "no-store", signal: controller.signal,
        });
        if (!response.ok) throw new Error("status unavailable");
        const value = await response.json();
        if (active) setMonitor(value);
      } catch {
        if (active) setMonitor(null);
      } finally {
        clearTimeout(timeout);
        if (active) timer = setTimeout(load, 30_000);
      }
    };
    load();
    return () => { active = false; clearTimeout(timer); controller?.abort(); };
  }, []);
  const view = syncStatusView(monitor, freshness);
  const timestamp = view.oldest ? new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(new Date(view.oldest)) : "";
  return <div className={`sync-status monitor-sync monitor-${view.state}`}>
    <span />{view.label} · {view.detail} {timestamp}
  </div>;
}

export function syncStatusView(monitor, freshness, now = Date.now()) {
  const checked = Date.parse(monitor?.checked_at);
  const current = Number.isFinite(checked) && now - checked <= 90_000 && checked <= now + 5_000;
  const missing = freshness?.missing_shop_count;
  const errors = freshness?.error_shop_count ?? 0;
  const oldest = Date.parse(freshness?.oldest_success_at);
  const overdue = Number.isFinite(oldest) && now - oldest > 15 * 60_000;
  const uncertain = !freshness || !freshness.shop_count || missing > 0;
  const syncWarning = uncertain || errors > 0 || overdue;
  return {
    state: !current ? "unknown" : syncWarning && monitor.state === "healthy" ? "warning" : monitor.state,
    label: !current ? "运行状态未核实" : syncWarning && monitor.state === "healthy" ? "同步状态待核实" : monitor.state_label,
    detail: uncertain ? `同步水位待核实${missing ? `（${missing}店缺失）` : ""}`
      : errors ? `${errors}店同步有错误` : overdue ? "部分店铺同步水位超过15分钟未更新" : "范围内最旧同步水位",
    oldest: uncertain ? null : freshness.oldest_success_at,
  };
}

const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const count = (value) => Number.isSafeInteger(value) && value >= 0;
const states = ["OPEN", "RESOLVED", "STOPPED", "ACKNOWLEDGED"];

export function issuesRequestParams(filters, page, focus) {
  const params = new URLSearchParams({ page: String(page), page_size: "15" });
  if (focus) {
    params.set("state", "OPEN");
    params.set("stage_id", focus.stageId);
    if (focus.cycleFinishedAt) params.set("cycle_finished_at", focus.cycleFinishedAt);
  } else {
    Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
  }
  return params;
}

export function validateIssuesResponse(value, expectedStageId) {
  const valid = object(value) && typeof value.checked_at === "string"
    && Number.isFinite(Date.parse(value.checked_at))
    && object(value.counts) && states.every((key) => count(value.counts[key] ?? (key === "ACKNOWLEDGED" ? 0 : undefined)))
    && object(value.category_counts)
    && Array.isArray(value.categories) && value.categories.every((c) => object(c)
      && typeof c.id === "string" && typeof c.label === "string" && count(value.category_counts[c.id]))
    && Array.isArray(value.shops) && value.shops.every((s) => object(s) && count(s.shop_id) && s.shop_id > 0)
    && object(value.pagination) && count(value.pagination.total)
    && count(value.pagination.pages) && value.pagination.pages > 0
    && count(value.pagination.page) && value.pagination.page > 0
    && count(value.pagination.page_size) && value.pagination.page_size > 0
    && Array.isArray(value.items) && value.items.every((i) => object(i)
      && typeof i.key === "string" && states.includes(i.state)
      && typeof i.reason === "string" && typeof i.category_label === "string"
      && typeof i.can_open_order === "boolean"
      && (!i.can_open_order || (typeof i.after_sales_sn === "string" && i.after_sales_sn.length > 0
        && count(i.shop_id) && i.shop_id > 0 && typeof i.platform === "string"))
      && Array.isArray(i.events) && i.events.every((e) => object(e) && states.includes(e.state)
        && typeof e.reason === "string" && typeof e.at === "string"))
    && new Set(value.items.map((i) => i.key)).size === value.items.length;
  if (!valid) throw new Error("异常明细响应格式不完整，不能据此确认没有异常");
  if (expectedStageId && (!object(value.focus) || value.focus.stage_id !== expectedStageId
    || !Array.isArray(value.focus.issue_keys) || typeof value.focus.message !== "string"
    || !value.items.every((item) => value.focus.issue_keys.includes(item.key)))) {
    throw new Error("未取得这条告警的精准明细，已阻止显示全量异常，请稍后刷新");
  }
  return value;
}

export async function loadIssuesSnapshot(url, { signal, timeoutMs = 12000, fetchImpl = fetch, expectedStageId } = {}) {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  let timedOut = false;
  if (signal?.aborted) cancel();
  signal?.addEventListener("abort", cancel, { once: true });
  const timer = setTimeout(() => { timedOut = true; cancel(); }, timeoutMs);
  try {
    const response = await fetchImpl(url, { cache: "no-store", signal: controller.signal });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result?.detail === "string" ? result.detail : `读取失败（${response.status}）`);
    return validateIssuesResponse(result, expectedStageId);
  } catch (error) {
    if (timedOut) throw new Error("异常明细读取超时，等待下次刷新；不能据此确认没有异常");
    throw error;
  } finally {
    clearTimeout(timer);
    signal?.removeEventListener("abort", cancel);
  }
}

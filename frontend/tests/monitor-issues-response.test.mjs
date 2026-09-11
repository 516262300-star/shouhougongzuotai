import { test } from "node:test";
import assert from "node:assert/strict";
import { issuesRequestParams, loadIssuesSnapshot, validateIssuesResponse } from "../src/monitor-issues-response.mjs";

const snapshot = () => ({
  checked_at: "2026-09-11T03:00:00Z", counts: { OPEN: 0, RESOLVED: 0, STOPPED: 0 },
  categories: [{ id: "ERP", label: "ERP" }], category_counts: { ERP: 0 }, shops: [], items: [],
  pagination: { page: 1, page_size: 15, pages: 1, total: 0 },
});

test("告警定位不带入历史筛选且保留精确阶段及周期", () => {
  const params = issuesRequestParams({ state: "ALL", platform: "TMALL", keyword: "old", shop_id: "9" }, 1,
    { stageId: "sync", cycleFinishedAt: "2026-09-11T11:00:00+08:00" });
  assert.equal(params.get("stage_id"), "sync");
  assert.equal(params.get("state"), "OPEN");
  assert.equal(params.get("cycle_finished_at"), "2026-09-11T11:00:00+08:00");
  assert.equal(params.has("keyword"), false);
  assert.equal(params.has("shop_id"), false);
  assert.equal(params.has("platform"), false);
});

test("旧后端返回总表或不同阶段数据时拒绝展示成精准结果", () => {
  assert.throws(() => validateIssuesResponse(snapshot(), "sync"), /精准明细/);
  const focused = { ...snapshot(), focus: { stage_id: "sync", issue_keys: [], message: "已恢复" } };
  assert.doesNotThrow(() => validateIssuesResponse(focused, "sync"));
  assert.throws(() => validateIssuesResponse(focused, "module3_erp_refunds"), /精准明细/);
  const item = { key: "unrelated", state: "OPEN", reason: "timeout", category_label: "ERP", can_open_order: false, events: [] };
  assert.throws(() => validateIssuesResponse({ ...focused, items: [item] }, "sync"), /精准明细/);
});

test("HTTP error, broken JSON and timeout reject rather than returning an empty snapshot", async () => {
  await assert.rejects(loadIssuesSnapshot("/issues", { fetchImpl: async () => new Response('{"detail":"permission denied"}', { status: 503 }) }), /permission denied/);
  await assert.rejects(loadIssuesSnapshot("/issues", { fetchImpl: async () => new Response("broken") }));
  const stalled = (_, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
  });
  await assert.rejects(loadIssuesSnapshot("/issues", { fetchImpl: stalled, timeoutMs: 10 }), /读取超时/);
});

test("only a complete successful empty result means no records", () => {
  assert.deepEqual(validateIssuesResponse(snapshot()), snapshot());
  for (const data of [null, {}, [], { items: [] }, { ...snapshot(), counts: {} }, { ...snapshot(), items: null }]) {
    assert.throws(() => validateIssuesResponse(data), /不能据此确认没有异常/);
  }
});

test("unknown state and incomplete order identity cannot appear as valid results", () => {
  const item = { key: "task:1", state: "OPEN", reason: "timeout", category_label: "ERP", can_open_order: false, events: [] };
  assert.doesNotThrow(() => validateIssuesResponse({ ...snapshot(), items: [item] }));
  for (const invalid of [{ ...item, state: "SUCCESS" }, { ...item, can_open_order: true }, { ...item, events: null }]) {
    assert.throws(() => validateIssuesResponse({ ...snapshot(), items: [invalid] }));
  }
  assert.throws(() => validateIssuesResponse({ ...snapshot(), items: [item, item] }));
});

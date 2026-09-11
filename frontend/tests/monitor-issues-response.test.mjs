import { test } from "node:test";
import assert from "node:assert/strict";
import { loadIssuesSnapshot, validateIssuesResponse } from "../src/monitor-issues-response.mjs";

const snapshot = () => ({
  checked_at: "2026-09-11T03:00:00Z", counts: { OPEN: 0, RESOLVED: 0, STOPPED: 0 },
  categories: [{ id: "ERP", label: "ERP" }], category_counts: { ERP: 0 }, shops: [], items: [],
  pagination: { page: 1, page_size: 15, pages: 1, total: 0 },
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

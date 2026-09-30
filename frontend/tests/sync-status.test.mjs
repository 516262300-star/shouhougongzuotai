import test from "node:test";
import assert from "node:assert/strict";
import { syncStatusView } from "../src/sync-status.mjs";

const now = Date.parse("2026-09-30T03:00:00Z");
const monitor = { state: "healthy", state_label: "当前周期完成", checked_at: new Date(now).toISOString() };
const freshness = { shop_count: 2, missing_shop_count: 0, error_shop_count: 0, oldest_success_at: new Date(now - 60_000).toISOString() };
test("missing/failed/stale monitor never retains green status", () => {
  assert.equal(syncStatusView(null, freshness, now).state, "unknown");
  assert.equal(syncStatusView({ ...monitor, state: "stopped" }, freshness, now).state, "stopped");
  assert.equal(syncStatusView(monitor, freshness, now + 91_000).state, "unknown");
});
test("missing or outdated shop watermarks are visible despite healthy worker", () => {
  assert.equal(syncStatusView(monitor, { ...freshness, missing_shop_count: 1 }, now).label, "同步状态待核实");
  assert.equal(syncStatusView(monitor, { ...freshness, missing_shop_count: 1 }, now).state, "warning");
  assert.equal(syncStatusView(monitor, { ...freshness, error_shop_count: 1 }, now).state, "warning");
  assert.equal(syncStatusView(monitor, { ...freshness, oldest_success_at: new Date(now - 3600_000).toISOString() }, now).state, "warning");
  assert.equal(syncStatusView(monitor, freshness, now).state, "healthy");
});

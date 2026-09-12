import { test } from "node:test";
import assert from "node:assert/strict";
import { runtimeStage, stageHasFailure } from "../src/monitor-stage-view.mjs";

test("同步正常推进的隔离订单和人工跟进不作为模块运行失败", () => {
  for (const status of ["warning", "acknowledged"]) {
    const source = { id: "sync", status, shops_failed: 0, error: "一笔订单隔离", scanned: 20 };
    const view = runtimeStage(source);
    assert.equal(view.status, "completed");
    assert.equal(stageHasFailure(view), false);
    assert.equal(view.error, null);
    assert.equal(view.scanned, 20);
    assert.equal(source.error, "一笔订单隔离");
    assert.equal(source.status, status);
  }
});

test("整店失败、发送阻断、跳过和未知状态不伪装成完成", () => {
  for (const source of [
    { id: "sync", status: "failed", shops_failed: 1, error: "授权失效" },
    { id: "notification", status: "failed", error: "发送结果未确认" },
    { id: "sync", status: "warning" },
    { id: "sync", status: "warning", shops_failed: 1 },
    { id: "sync", status: "skipped" },
    { id: "sync", status: "missing" },
  ]) {
    assert.equal(runtimeStage(source), source);
    assert.equal(stageHasFailure(source), source.status === "failed");
  }
});

test("退款结果待确认保留独立状态，不提供失败入口；混合真实失败仍提供入口", () => {
  const pending = { id: "pdd_refund", status: "awaiting_confirmation", failed: 0, pending_confirmation: 1 };
  assert.equal(runtimeStage(pending).status, "awaiting_confirmation");
  assert.equal(stageHasFailure(pending), false);
  assert.equal(stageHasFailure({ ...pending, status: "failed", failed: 1 }), true);
});

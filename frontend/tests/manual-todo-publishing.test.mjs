import test from "node:test";
import assert from "node:assert/strict";
import { readPublishControl, savePublishControl } from "../src/manualTodoPublishing.mjs";

test("read does not write and rejects incomplete state", async () => {
  await assert.rejects(readPublishControl(undefined, async (url, options) => {
    assert.equal(options.method, undefined);
    assert.equal(options.cache, "no-store");
    return { ok: true, json: async () => ({ enabled: false }) };
  }), /不完整/);
});

test("save carries explicit value, previous version and operation header", async () => {
  const result = await savePublishControl(false, 3, undefined, async (url, options) => {
    assert.equal(url, "/api/v1/aftersales/manual-todos/publishing");
    assert.equal(options.method, "PUT");
    assert.deepEqual(JSON.parse(options.body), { enabled: false, expected_version: 3 });
    assert.equal(options.headers["X-Workbench-Action"], "manual-todo-publish-switch");
    return { ok: true, json: async () => ({ enabled: false, effective_enabled: false, version: 4 }) };
  });
  assert.equal(result.enabled, false);
});

test("conflict or network error is not retried or reported as saved", async () => {
  let requests = 0;
  await assert.rejects(savePublishControl(true, 0, undefined, async () => {
    requests += 1;
    return { ok: false, status: 409, json: async () => ({ detail: "开关已变化" }) };
  }), /开关已变化/);
  assert.equal(requests, 1);
  await assert.rejects(savePublishControl(true, 0, undefined, async () => {
    throw new Error("network unavailable");
  }), /network unavailable/);
});

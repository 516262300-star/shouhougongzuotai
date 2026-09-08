const endpoint = "/api/v1/aftersales/manual-todos/publishing";

async function responseState(response) {
  const payload = await response.json();
  if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : `服务返回 ${response.status}`);
  if (typeof payload.enabled !== "boolean" || typeof payload.effective_enabled !== "boolean" || !Number.isInteger(payload.version)) {
    throw new Error("开关状态不完整，请刷新核实");
  }
  return payload;
}

export async function readPublishControl(signal, request = fetch) {
  return responseState(await request(endpoint, { signal, cache: "no-store" }));
}

export async function savePublishControl(enabled, version, signal, request = fetch) {
  return responseState(await request(endpoint, {
    method: "PUT", signal,
    headers: { "Content-Type": "application/json", "X-Workbench-Action": "manual-todo-publish-switch" },
    body: JSON.stringify({ enabled, expected_version: version }),
  }));
}
